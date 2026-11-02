"""Observe strict tool and caller-policy translation with a fake API upstream.

This is not a Claude Code permission test or evidence of real model behavior.
Only a supplied translator binary, synthetic key and loopback fake are used.
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
import urllib.error
import urllib.request
from unittest.mock import patch

from claude_fixture import isolated_environment
from smoke_api_contract import MOCK_UPSTREAM_KEY, MODEL, post, post_stream

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile  # noqa: E402


EXAMPLE = "SYNTHETIC_EXAMPLE_ONLY"
NAME = "fixture_tool"


def post_observed(port, local_key, payload):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + local_key, "Content-Type": "application/json",
                 "anthropic-version": "2023-06-01"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, None


class SyntheticUpstream(BaseHTTPRequestHandler):
    requests = []
    authorization_ok = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.requests.append(request)
            self.authorization_ok.append(self.path.endswith("/v1/responses") and
                self.headers.get("Authorization") == "Bearer " + MOCK_UPSTREAM_KEY)
            invalid = "synthetic_bad_arguments" in json.dumps(request.get("input", []))
            call = {"id": "fc_fixture", "type": "function_call", "status": "completed",
                    "call_id": "call_fixture", "name": NAME,
                    "arguments": '{"value":123}' if invalid else '{"value":"fixture"}'}
            response = {"id": "resp_fixture", "object": "response", "created_at": 1,
                        "status": "completed", "model": MODEL, "output": [call],
                        "usage": {"input_tokens": 20, "output_tokens": 1, "total_tokens": 21}}
            events = [{"type": "response.created", "response": {"id": response["id"],
                        "model": MODEL, "status": "in_progress"}},
                      {"type": "response.completed", "response": response}]
            if "synthetic_stream_case" in json.dumps(request.get("input", [])):
                events = [events[0],
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {"id": "fc_fixture", "type": "function_call", "call_id": "call_fixture",
                              "name": NAME, "arguments": ""}},
                    {"type": "response.function_call_arguments.delta", "output_index": 0,
                     "delta": call["arguments"]},
                    {"type": "response.function_call_arguments.done", "output_index": 0,
                     "arguments": call["arguments"]},
                    {"type": "response.output_item.done", "output_index": 0, "item": call}, events[1]]
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
    SyntheticUpstream.requests = []
    SyntheticUpstream.authorization_ok = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-tool-contract-") as temporary:
            root = Path(temporary)
            runtime = root / "runtime-api"
            runtime.mkdir(mode=0o700)
            api_profile.set_key(root, MOCK_UPSTREAM_KEY)
            spec = {"context_window": 1_050_000, "supported_reasoning_levels": [{"effort": "low"}],
                    "supports_search_tool": False, "input_modalities": ["text"],
                    "source": api_profile.DOCS_MODEL_BASE + MODEL + ".md"}
            with patch.object(api_profile, "discover_models", return_value={MODEL: spec}):
                port, _, config_path, key_path, _ = api_profile.make_session(root, runtime)
            local_key = key_path.read_text().strip()
            config = config_path.read_text()
            public_base = 'base-url: "https://api.openai.com/v1"'
            if config.count(public_base) != 1:
                raise RuntimeError("Disposable proxy configuration is not the expected API profile")
            config_path.write_text(config.replace(public_base,
                f'base-url: "http://127.0.0.1:{upstream.server_port}/v1"'))
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
                schema = {"type": "object", "properties": {"value": {"type": "string"}},
                          "required": ["value"], "additionalProperties": False}
                base = {"model": MODEL, "max_tokens": 128, "stream": False,
                        "messages": [{"role": "user", "content": "synthetic fixture"}]}
                strict_tool = {"name": NAME, "description": "Synthetic fixture", "input_schema": schema,
                               "strict": True, "input_examples": [{"value": EXAMPLE}],
                               "allowed_callers": ["direct"]}
                direct = post(port, local_key, {**base, "tools": [strict_tool]})
                programmatic_tool = {**strict_tool, "strict": False,
                                     "allowed_callers": ["code_execution_20260120"]}
                count_before_restricted = len(SyntheticUpstream.requests)
                restricted_status, programmatic = post_observed(port, local_key, {**base, "tools": [programmatic_tool]})
                restricted_contacted = len(SyntheticUpstream.requests) > count_before_restricted
                second = SyntheticUpstream.requests[count_before_restricted] if restricted_contacted else {}
                weakened_schema = {**schema, "properties": {"value": {"type": "string", "pattern": r"\p{L}+"}}}
                weakened_tool = {**strict_tool, "input_schema": weakened_schema}
                count_before_weakened = len(SyntheticUpstream.requests)
                weakened_status, _ = post_observed(port, local_key, {**base, "tools": [weakened_tool]})
                weakened_contacted = len(SyntheticUpstream.requests) > count_before_weakened
                count_before_stream = len(SyntheticUpstream.requests)
                try:
                    streamed = post_stream(port, local_key, {**base, "stream": True,
                        "messages": [{"role": "user", "content": "synthetic_stream_case"}],
                        "tools": [programmatic_tool]})
                    stream_status = 200
                except urllib.error.HTTPError as error:
                    stream_status = error.code
                    error.close()
                    streamed = []
                stream_contacted = len(SyntheticUpstream.requests) > count_before_stream
                bad_status, bad_response = post_observed(port, local_key, {**base,
                    "messages": [{"role": "user", "content": "synthetic_bad_arguments"}],
                    "tools": [strict_tool]})
                if not 1 <= len(SyntheticUpstream.requests) <= 5 or not all(SyntheticUpstream.authorization_ok):
                    raise RuntimeError("Synthetic upstream control did not complete")
                first = SyntheticUpstream.requests[0]
                first_tools = [tool for tool in first.get("tools", []) if tool.get("name") == NAME]
                second_tools = [tool for tool in second.get("tools", []) if tool.get("name") == NAME]
                if len(first_tools) != 1:
                    raise RuntimeError("Synthetic direct tool was not offered")
                direct_calls = [part for part in direct.get("content", []) if part.get("type") == "tool_use"]
                restricted_calls = [part for part in (programmatic or {}).get("content", []) if part.get("type") == "tool_use"]
                bad_calls = [part for part in (bad_response or {}).get("content", []) if part.get("type") == "tool_use"]
                if len(direct_calls) != 1 or direct_calls[0].get("name") != NAME:
                    raise RuntimeError("Synthetic direct tool-call control failed")
                return {"synthetic_only": True, "fake_upstream_only": True,
                        "claude_code_client_tested": False, "model_inference": False,
                        "explicit_strict_true_preserved": first_tools[0].get("strict") is True,
                        "input_example_visible_to_fake_upstream": EXAMPLE in json.dumps(first),
                        "programmatic_only_status": restricted_status,
                        "programmatic_only_contacted_fake_upstream": restricted_contacted,
                        "programmatic_only_tool_offered_to_fake_upstream": bool(second_tools),
                        "programmatic_only_direct_tool_use_returned": any(call.get("name") == NAME for call in restricted_calls),
                        "weakened_strict_schema_status": weakened_status,
                        "weakened_strict_schema_contacted_fake_upstream": weakened_contacted,
                        "programmatic_only_stream_status": stream_status,
                        "programmatic_only_stream_contacted_fake_upstream": stream_contacted,
                        "programmatic_only_stream_returned_tool_use": any(event.get("content_block", {}).get("type") == "tool_use"
                                                                         for _, event in streamed),
                        "invalid_strict_arguments_status": bad_status,
                        "invalid_strict_arguments_returned_as_tool_use": any(call.get("input", {}).get("value") == 123
                                                                              for call in bad_calls)}
            finally:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--translator", required=True, type=Path)
    parser.add_argument("--require-preflight-guard", action="store_true",
                        help="Require the experimental request guard, not complete strict output validation")
    parser.add_argument("--require-caller-only-guard", action="store_true",
                        help="Require only caller-policy rejection and unchanged unsupported strict behavior")
    options = parser.parse_args()
    if options.require_preflight_guard and options.require_caller_only_guard:
        raise RuntimeError("Choose one synthetic guard candidate")
    result = observe(options.translator.resolve())
    print(json.dumps(result, sort_keys=True))
    if options.require_preflight_guard and not (
            result["explicit_strict_true_preserved"] and
            result["programmatic_only_status"] == 400 and not result["programmatic_only_contacted_fake_upstream"] and
            result["programmatic_only_stream_status"] == 400 and not result["programmatic_only_stream_contacted_fake_upstream"] and
            result["weakened_strict_schema_status"] == 400 and not result["weakened_strict_schema_contacted_fake_upstream"]):
        raise RuntimeError("Synthetic request guard did not meet its limited acceptance criteria")
    if options.require_caller_only_guard and not (
            not result["explicit_strict_true_preserved"] and
            result["programmatic_only_status"] == 400 and not result["programmatic_only_contacted_fake_upstream"] and
            result["programmatic_only_stream_status"] == 400 and not result["programmatic_only_stream_contacted_fake_upstream"] and
            result["weakened_strict_schema_status"] == 200 and result["weakened_strict_schema_contacted_fake_upstream"] and
            result["invalid_strict_arguments_returned_as_tool_use"]):
        raise RuntimeError("Synthetic caller-only guard or its strict-mode negative control did not match")


if __name__ == "__main__":
    try: main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic tool-contract observation failed; raw output withheld") from None
