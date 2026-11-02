"""Observe whether a synthetic effort rejection causes Claude to retry without it.

Only a temporary Claude configuration and an in-memory loopback fake Messages
server are used. No production proxy, model account, or user settings are used.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading

from claude_fixture import isolated_environment


MODEL = "gpt-6.1-sol"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        effort = (body.get("output_config") or {}).get("effort")
        self.server.efforts.append(effort)
        if effort is not None:
            message = ("output_config.effort: Extra inputs are not permitted" if self.server.recognizable else
                       "The requested reasoning level is unavailable. Choose another level using /effort.")
            if self.server.stream_error:
                start = {"type": "message_start", "message": {"id": "msg_fixture", "type": "message",
                         "role": "assistant", "model": MODEL, "content": [], "stop_reason": None,
                         "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 0}}}
                failure = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in (start, failure):
                    self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
                return
            payload = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": message}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            if not self.server.recognizable:
                self.send_header("x-should-retry", "false")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "DONE"}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
                  {"type": "message_stop"}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def run_case(claude, recognizable, *, followup=False, stream_error=False):
    with tempfile.TemporaryDirectory(prefix="bridge-effort-rejection-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.efforts, server.recognizable, server.stream_error = [], recognizable, stream_error
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_AUTH_TOKEN="fixture-only",
                   ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1", ENABLE_TOOL_SEARCH="false")
        try:
            common = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", str(settings), "--permission-mode", "dontAsk", "--model", MODEL, "--effort", "max",
                "--tools", ""]
            if followup:
                process = subprocess.Popen([*common, "--input-format", "stream-json", "--output-format", "stream-json",
                                            "--verbose", "-p"], cwd=work, env=env, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
                events = queue.Queue()
                def read_events():
                    for line in process.stdout:
                        try:
                            events.put(json.loads(line))
                        except ValueError:
                            pass
                    events.put({"type": "fixture_eof"})
                reader = threading.Thread(target=read_events, daemon=True)
                reader.start()
                def send_prompt(text):
                    process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
                    process.stdin.flush()
                def get_result():
                    while True:
                        event = events.get(timeout=60)
                        if event.get("type") == "fixture_eof":
                            raise RuntimeError("Synthetic client exited before its result")
                        if event.get("type") == "result":
                            return event
                try:
                    send_prompt("Return a first synthetic response.")
                    first = get_result()
                    boundary = len(server.efforts)
                    send_prompt("Return a second synthetic response.")
                    second = get_result()
                    return {"first_turn_efforts": server.efforts[:boundary], "second_turn_efforts": server.efforts[boundary:],
                            "both_turns_completed": not first.get("is_error") and not second.get("is_error")}
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            result = subprocess.run([*common, "--output-format", "json", "-p", "Return a synthetic response."],
                                    cwd=work, env=env, capture_output=True, text=True, timeout=60)
            try:
                message = json.loads(result.stdout)
                completed = result.returncode == 0 and not message.get("is_error")
                reported_error = bool(message.get("is_error"))
            except (ValueError, AttributeError, TypeError):
                completed = False
                reported_error = False
            return {"request_efforts": server.efforts, "client_completed": completed,
                    "client_reported_error": reported_error}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main():
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    recognized = run_case(claude, True)
    alternative = run_case(claude, False)
    continuation = run_case(claude, True, followup=True)
    streamed = run_case(claude, True, stream_error=True)
    if not recognized["request_efforts"] or recognized["request_efforts"][0] != "max":
        raise RuntimeError("The synthetic initial-effort control failed")
    if not alternative["request_efforts"] or alternative["request_efforts"][0] != "max":
        raise RuntimeError("The synthetic alternative-error control failed")
    print(json.dumps({"synthetic_only": True, "direct_messages_without_bridge": True,
                      "recognized_effort_rejection": recognized, "alternative_error": alternative,
                      "same_process_followup": continuation, "recognized_sse_error": streamed}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, queue.Empty, subprocess.SubprocessError):
        raise SystemExit("Synthetic effort-rejection probe failed; raw output withheld") from None
