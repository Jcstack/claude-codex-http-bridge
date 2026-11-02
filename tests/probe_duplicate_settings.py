"""Observe repeated --settings routing with a disposable real Claude client.

Both endpoints, all credentials, settings, and prompts are synthetic and local.
Environment isolation is not an operating-system sandbox.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from claude_fixture import isolated_environment


MODEL = "gpt-6.1-sol"
FAKE_KEY = "fixture-local-token"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        token = self.headers.get("Authorization") == "Bearer " + FAKE_KEY or self.headers.get("x-api-key") == FAKE_KEY
        if "count_tokens" in self.path:
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":100}'); return
        with self.server.lock:
            self.server.messages += 1
            self.server.fake_key_received |= token
        block = {"type": "text", "text": "DONE"}
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [block], "stop_reason": "end_turn", "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        self.send_response(200)
        if not body.get("stream"):
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(message).encode()); return
        self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        events = [{"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
                  {"type": "content_block_start", "index": 0, "content_block": {**block, "text": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "DONE"}},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
                  {"type": "message_stop"}]
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def run_case(claude: str, duplicate: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="bridge-duplicate-settings-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve(); work = root / "work"; config = root / "config"
        work.mkdir(); config.mkdir(mode=0o700)
        servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        threads = []
        for server in servers:
            server.lock = threading.Lock(); server.messages = 0; server.fake_key_received = False
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); threads.append(thread)
        urls = [f"http://127.0.0.1:{server.server_port}" for server in servers]
        generated, user = root / "generated.json", root / "user.json"
        generated.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": urls[0], "ANTHROPIC_AUTH_TOKEN": FAKE_KEY},
            "modelPicker": {"options": [{"model": MODEL, "label": "Fixture", "behavesAs": "claude-sonnet-5-5"}],
                            "replaceBuiltInOptions": True}}), encoding="utf-8")
        user.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": urls[1]}}), encoding="utf-8")
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=urls[0], ANTHROPIC_AUTH_TOKEN=FAKE_KEY,
                   ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
        command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--settings", str(generated)]
        if duplicate: command += ["--settings", str(user)]
        command += ["--model", MODEL, "--tools", "", "--permission-mode", "dontAsk", "--output-format", "json",
                    "-p", "Return the synthetic response."]
        try:
            result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=45)
            try:
                value = json.loads(result.stdout)
                completed = result.returncode == 0 and not value.get("is_error") and "DONE" in value.get("result", "")
            except (ValueError, AttributeError, TypeError):
                completed = False
            return {"client_completed": completed, "first_endpoint_messages": servers[0].messages,
                    "second_endpoint_messages": servers[1].messages,
                    "first_received_fake_key": servers[0].fake_key_received,
                    "second_received_fake_key": servers[1].fake_key_received}
        finally:
            for server in servers: server.shutdown(); server.server_close()
            for thread in threads: thread.join(timeout=2)


def main() -> None:
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude: raise RuntimeError("The locked Claude fixture is required")
    control, duplicate = run_case(claude, False), run_case(claude, True)
    print(json.dumps({"synthetic_only": True, "control": control, "duplicate_settings": duplicate}, sort_keys=True))
    if not (control["client_completed"] and control["first_endpoint_messages"] and not control["second_endpoint_messages"]
            and control["first_received_fake_key"] and duplicate["client_completed"]):
        raise RuntimeError("Synthetic duplicate-settings control did not complete")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic duplicate-settings observation failed; raw output withheld") from None
