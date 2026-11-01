"""Exercise pinned proxy SSE keep-alives against a disposable local upstream.

The fixture never connects to a model account or changes the installed runtime.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile
import launcher


MODEL = "gpt-6.1-sol"
SPEC = {"context_window": 872000, "input_modalities": ["text"], "supports_search_tool": True,
        "supported_reasoning_levels": [{"effort": level} for level in ("low", "medium", "high", "xhigh", "max")]}


class Origin(BaseHTTPRequestHandler):
    mode = "stream"

    def log_message(self, *_args): pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.attempts += 1
        if self.server.mode == "late-error":
            time.sleep(2.5)
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"type":"server_error","message":"synthetic"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        item = {"type": "message", "role": "assistant", "id": "msg_fixture", "phase": "final_answer",
                "content": [{"type": "output_text", "text": "fixture", "annotations": []}]}
        events = [
            {"type": "response.created", "response": {"id": "resp_fixture", "model": MODEL}},
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**item, "content": []}},
            {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
             "part": {"type": "output_text", "text": ""}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "fixture"},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": "resp_fixture", "model": MODEL,
                "output": [item], "status": "completed", "usage": {"input_tokens": 10, "output_tokens": 1}}},
        ]
        try:
            for index, event in enumerate(events):
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
                if index == 3:
                    if self.server.mode == "cancel":
                        for _ in range(40):
                            time.sleep(.2)
                            self.wfile.write(b'data: {"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"."}\n\n')
                            self.wfile.flush()
                        return
                    time.sleep(2.5)
                    if self.server.mode == "truncated":
                        return
        except OSError:
            self.server.disconnected.set()


@contextmanager
def translator(binary: Path, upstream_port: int, *, before_first: bool):
    with tempfile.TemporaryDirectory(prefix="bridge-keepalive-") as temporary:
        root = Path(temporary)
        runtime = root / "runtime-api"
        runtime.mkdir(mode=0o700)
        api_profile.set_key(root, "synthetic-upstream-" + "x" * 40)
        with patch.object(api_profile, "discover_models", return_value={MODEL: SPEC}):
            port, _, config, key, _ = api_profile.make_session(root, runtime)
        text = config.read_text()
        text = text.replace('base-url: "https://api.openai.com/v1"',
                            f'base-url: "http://127.0.0.1:{upstream_port}/v1"')
        text = text.replace("request-retry: 2", "request-retry: 0")
        text += "\nrequests:\n  streaming:\n    keepalive-seconds: 1\n    bootstrap-retries: 0\n"
        text += "    send-keepalive-before-ft: " + ("true" if before_first else "false") + "\n"
        api_profile.write_private(config, text)
        process = subprocess.Popen([str(binary), "-config", str(config)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=root)
        try:
            launcher.wait_for_private_listener(process, port)
            environment = os.environ.copy()
            environment.update({"CODEX_BRIDGE_PORT": str(port), "CODEX_BRIDGE_KEY_FILE": str(key),
                                "CODEX_BRIDGE_ROUTE": "api",
                                "CODEX_BRIDGE_API_METADATA_FILE": str(runtime / "catalog.json")})
            launcher.wait_for_catalog(process, environment, True, "api")
            yield port, key.read_text().strip()
        finally:
            launcher.stop_process(process)


def request(port: int, key: str):
    payload = {"model": MODEL, "max_tokens": 64, "stream": True,
               "messages": [{"role": "user", "content": "synthetic fixture"}]}
    item = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(item, timeout=15)


def read_stream(response) -> tuple[int, list[str]]:
    comments, events = 0, []
    for line in response:
        if line.startswith(b":"):
            comments += 1
            events.append("fixture_keepalive")
        elif line.startswith(b"data:"):
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                events.append(event["type"])
    return comments, events


def claude_stream(port: int, key: str) -> None:
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("Claude client fixture is unavailable")
    with tempfile.TemporaryDirectory(prefix="bridge-keepalive-client-") as temporary:
        root = Path(temporary)
        work = root / "work"
        work.mkdir()
        config = root / "config"
        config.mkdir(mode=0o700)
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL,
            "label": "Synthetic fixture", "behavesAs": "claude-sonnet-5-5"}],
            "replaceBuiltInOptions": True}}), encoding="utf-8")
        env = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC",
               "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if name in os.environ}
        env.update({"CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_AUTH_TOKEN": key,
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL, "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"})
        result = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "json",
            "--model", MODEL, "--tools", "", "-p", "Return the synthetic fixture response."],
            cwd=work, env=env, capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise RuntimeError("Isolated Claude keep-alive fixture exited unsuccessfully")
        value = json.loads(result.stdout)
        if value.get("is_error") or "fixture" not in value.get("result", ""):
            raise RuntimeError("Isolated Claude did not finish the keep-alive fixture")


def main() -> None:
    if len(sys.argv) != 2:
        raise RuntimeError("Usage: probe_stream_keepalive.py /path/to/reviewed-proxy")
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    origin.attempts = 0
    origin.disconnected = threading.Event()
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()
    binary = Path(sys.argv[1]).resolve()
    try:
        with translator(binary, origin.server_port, before_first=False) as (port, key):
            origin.mode = "stream"
            with request(port, key) as response:
                comments, events = read_stream(response)
            if (comments < 1 or not events or events[-1] != "message_stop"
                    or "content_block_delta" not in events or events.index("content_block_delta") > events.index("fixture_keepalive")):
                raise RuntimeError("Post-first-token keep-alives or final event were not preserved")
            claude_stream(port, key)
            origin.mode = "truncated"
            origin.attempts = 0
            with request(port, key) as response:
                comments, events = read_stream(response)
            if comments < 1 or "message_stop" in events or origin.attempts != 1:
                raise RuntimeError("Incomplete stream was replayed or appeared successful")
            origin.mode = "cancel"
            origin.attempts = 0
            origin.disconnected.clear()
            with request(port, key) as response:
                for line in response:
                    if line.startswith(b":"):
                        break
            if not origin.disconnected.wait(8) or origin.attempts != 1:
                raise RuntimeError("Client cancellation did not release the synthetic upstream")
            origin.mode = "late-error"
            try:
                with request(port, key):
                    raise RuntimeError("A delayed pre-stream error was reported as success")
            except urllib.error.HTTPError as error:
                if error.code != 503:
                    raise RuntimeError("Unexpected delayed pre-stream error status") from None
        with translator(binary, origin.server_port, before_first=True) as (port, key):
            origin.mode = "late-error"
            with request(port, key) as response:
                status = response.status
                prefirst_comments, error_events = read_stream(response)
            if status != 200 or prefirst_comments < 1 or "error" not in error_events:
                raise RuntimeError("Pre-first-token error framing did not match the fixture expectation")
        print(json.dumps({"mock_only": True, "post_first_token_keepalive_and_completion": True,
            "claude_client_completed_with_keepalive": True,
            "incomplete_stream_not_replayed_or_completed": True, "cancel_releases_origin": True,
            "pre_first_disabled_preserves_http_503": True,
            "pre_first_enabled_commits_200_then_inband_error": True,
            "active_runtime_changed": False}, sort_keys=True))
    finally:
        origin.shutdown()
        origin.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
