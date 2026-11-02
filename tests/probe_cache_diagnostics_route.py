"""Observe synthetic cache-diagnostic translation with a disposable API proxy.

The configured upstream is a loopback fake. No real credential, user prompt,
model call or running service is used; the provided translator binary is read
as executable code. The input extension is synthetic and is not a claim that
Claude Code sends it. Run with --translator PATH.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from claude_fixture import isolated_environment
from smoke_api_contract import MOCK_UPSTREAM_KEY, MODEL, post, post_stream

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile  # noqa: E402


DIAGNOSTIC = {"type": "cache_miss", "reason": "tools_changed",
              "comparison_reusable_tokens": 2048, "cache_missed_tokens": 1024}


def contains_diagnostic(value):
    if isinstance(value, dict):
        return "prompt_cache_diagnostics" in value or any(contains_diagnostic(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_diagnostic(v) for v in value)
    return value == "tools_changed"


class SyntheticUpstream(BaseHTTPRequestHandler):
    requests = []
    synthetic_authorization = []
    expected_paths = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            size = int(self.headers["Content-Length"])
            request = json.loads(self.rfile.read(size))
            self.requests.append(request)
            self.synthetic_authorization.append(self.headers.get("Authorization") == "Bearer " + MOCK_UPSTREAM_KEY)
            self.expected_paths.append(self.path.endswith("/v1/responses"))
            message = {"id": "msg_fixture", "type": "message", "status": "completed",
                       "role": "assistant", "content": [{"type": "output_text", "text": "ok", "annotations": []}]}
            response = {"id": "resp_fixture", "object": "response", "created_at": 1,
                        "status": "completed", "model": MODEL, "output": [message],
                        "prompt_cache_diagnostics": DIAGNOSTIC,
                        "usage": {"input_tokens": 20, "output_tokens": 1, "total_tokens": 21,
                                  "input_tokens_details": {"cached_tokens": 10}}}
            events = [
                {"type": "response.created", "response": {"id": response["id"], "model": MODEL, "status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {"id": "msg_fixture", "type": "message", "role": "assistant", "content": []}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "ok"},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": response},
            ]
            encoded = b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (OSError, ValueError, KeyError):
            return


def observe(binary):
    if not binary.is_file():
        raise RuntimeError("Disposable proxy binary is missing")
    if not contains_diagnostic({"response": {"prompt_cache_diagnostics": DIAGNOSTIC}}) or contains_diagnostic({"text": "ok"}):
        raise RuntimeError("Synthetic diagnostic detector control failed")
    SyntheticUpstream.requests = []
    SyntheticUpstream.synthetic_authorization = []
    SyntheticUpstream.expected_paths = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-cache-diagnostics-") as temporary:
            root = Path(temporary)
            runtime = root / "runtime-api"
            runtime.mkdir(mode=0o700)
            api_profile.set_key(root, MOCK_UPSTREAM_KEY)
            spec = {"context_window": 1_050_000,
                    "supported_reasoning_levels": [{"effort": "low"}],
                    "supports_search_tool": False, "input_modalities": ["text"],
                    "source": api_profile.DOCS_MODEL_BASE + MODEL + ".md"}
            with patch.object(api_profile, "discover_models", return_value={MODEL: spec}):
                port, _, config_path, key_path, _ = api_profile.make_session(root, runtime)
            local_key = key_path.read_text().strip()
            config = config_path.read_text()
            public_base = 'base-url: "https://api.openai.com/v1"'
            if config.count(public_base) != 1:
                raise RuntimeError("Disposable proxy configuration is not the expected API profile")
            config = config.replace(public_base, f'base-url: "http://127.0.0.1:{upstream.server_port}/v1"')
            config_path.write_text(config)
            environment = isolated_environment(root)
            environment.update({"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                                "NO_PROXY": "localhost,127.0.0.1", "http_proxy": "http://127.0.0.1:9",
                                "https_proxy": "http://127.0.0.1:9", "no_proxy": "localhost,127.0.0.1"})
            process = subprocess.Popen([str(binary), "-config", str(config_path)], cwd=root, env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        raise RuntimeError("Disposable proxy exited before becoming ready")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.2):
                            break
                    except OSError:
                        time.sleep(.2)
                else:
                    raise RuntimeError("Disposable proxy startup timed out")
                request = {"model": MODEL, "max_tokens": 128,
                           "messages": [{"role": "user", "content": "synthetic fixture"}],
                           "prompt_cache_options": {"comparison_response_id": "resp_fixture"}}
                nonstream = post(port, local_key, {**request, "stream": False})
                stream = [event for _, event in post_stream(port, local_key, {**request, "stream": True})]
                if len(SyntheticUpstream.requests) != 2 or not all(SyntheticUpstream.synthetic_authorization) or not all(
                        SyntheticUpstream.expected_paths) or not any(
                        part.get("text") == "ok" for part in nonstream.get("content", [])) or not any(
                        event.get("type") == "message_stop" for event in stream):
                    raise RuntimeError("Synthetic control did not complete")
                expected_options = {"comparison_response_id": "resp_fixture"}
                return {"synthetic_only": True, "fake_upstream_only": True,
                        "model_inference": False, "claude_code_client_tested": False,
                        "nonstream_request_options_forwarded": SyntheticUpstream.requests[0].get("prompt_cache_options") == expected_options,
                        "stream_request_options_forwarded": SyntheticUpstream.requests[1].get("prompt_cache_options") == expected_options,
                        "nonstream_diagnostics_forwarded": contains_diagnostic(nonstream),
                        "stream_diagnostics_forwarded": contains_diagnostic(stream),
                        "nonstream_cache_read_reported": nonstream.get("usage", {}).get("cache_read_input_tokens") == 10,
                        "stream_cache_read_reported": any(event.get("usage", {}).get("cache_read_input_tokens") == 10 or
                            event.get("message", {}).get("usage", {}).get("cache_read_input_tokens") == 10 for event in stream)}
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--translator", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(observe(args.translator.resolve()), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic cache-diagnostics observation failed; raw output withheld") from None
