"""Test candidate mapping with real Claude and only synthetic local servers."""

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

from claude_fixture import isolated_environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_alias_policy import alias_for  # noqa: E402


MODEL = "gpt-6.1-sol"
ORIGINALS = ("lookup.fixture", "lookup_fixture")
ALIASES = ["mcp__gateway__" + alias_for(name, "gateway") for name in ORIGINALS]
DYNAMIC_ALIAS = "mcp__gateway__" + alias_for("lookup.extra", "gateway")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        self.server.bodies.append(body)
        step = len(self.server.bodies) - 1
        if self.server.dynamic and step == 1:
            deadline = time.monotonic() + 8
            while not self.server.refresh_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        names = [ALIASES[0], DYNAMIC_ALIAS] if self.server.dynamic else ALIASES
        calling = step < len(names)
        block = ({"type": "tool_use", "id": f"fixture_call_{step}", "name": names[step], "input": {}}
                 if calling else {"type": "text", "text": "DONE"})
        stop = "tool_use" if calling else "end_turn"
        message = {"id": f"msg_fixture_{step}", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        events = [
            {"type": "message_start", "message": message},
            {"type": "content_block_start", "index": 0,
             "content_block": {**block, "input": {}} if calling else {**block, "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": "{}"} if calling
                      else {"type": "text_delta", "text": "DONE"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def run_case(claude, deny, *, resume=False, dynamic=False):
    with tempfile.TemporaryDirectory(prefix="bridge-mcp-alias-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        marker = root / "calls"
        script = Path(__file__).with_name("mcp_alias_fixture.py").resolve()
        mcp = root / "mcp.json"
        mcp_args = [str(script), "--fake-adapter", str(marker)]
        if dynamic:
            mcp_args.append("dynamic")
        mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
            "args": mcp_args}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.bodies = []
        server.dynamic = dynamic
        server.refresh_marker = Path(str(marker) + ".refreshed")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                   ENABLE_TOOL_SEARCH="false", MCP_CONNECTION_NONBLOCKING="0",
                   CLAUDE_CODE_MCP_STARTUP_WAIT_MS="5000", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1")
        command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp), "--settings", str(settings),
                   "--permission-mode", "dontAsk", "--model", MODEL, "--tools", "", "--allowedTools",
                   ",".join([*ALIASES, DYNAMIC_ALIAS] if dynamic else ALIASES),
                   "--output-format", "json", "--max-turns", "5"]
        if deny:
            command += ["--disallowedTools", deny]
        try:
            result = subprocess.run([*command, "-p", "Use the synthetic fixture tools."], cwd=work,
                                    env=env, capture_output=True, text=True, timeout=60)
            try:
                message = json.loads(result.stdout)
                completed = result.returncode == 0 and not message.get("is_error") and "DONE" in message.get("result", "")
            except (ValueError, AttributeError, TypeError):
                message = {}
                completed = False
            offered = {item.get("name") for body in server.bodies for item in body.get("tools", [])
                       if isinstance(item, dict)}
            if resume and completed:
                session_id = message.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    raise RuntimeError("Synthetic session could not be resumed")
                server.bodies.clear()
                result = subprocess.run([*command, "--resume", session_id, "-p", "Use both synthetic tools again."],
                                        cwd=work, env=env, capture_output=True, text=True, timeout=60)
                try:
                    message = json.loads(result.stdout)
                    completed = (result.returncode == 0 and not message.get("is_error")
                                 and "DONE" in message.get("result", ""))
                except (ValueError, AttributeError, TypeError):
                    completed = False
                resumed_offered = {item.get("name") for body in server.bodies for item in body.get("tools", [])
                                   if isinstance(item, dict)}
                completed = completed and set(ALIASES).issubset(resumed_offered)
            calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
            return completed, offered, calls, server.refresh_marker.exists()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main():
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    ok, offered, calls, _ = run_case(claude, None, resume=True)
    denied_ok, denied_offered, denied_calls, _ = run_case(claude, ALIASES[0])
    dynamic_ok, dynamic_offered, dynamic_calls, refreshed = run_case(claude, None, dynamic=True)
    if not ok or not set(ALIASES).issubset(offered) or calls != list(ORIGINALS) * 2:
        raise RuntimeError("Distinct synthetic original tools were not each called once per session turn")
    if not denied_ok or ALIASES[1] not in denied_offered or denied_calls != [ORIGINALS[1]]:
        raise RuntimeError("Synthetic scoped deny control failed")
    if not dynamic_ok or not refreshed or DYNAMIC_ALIAS not in dynamic_offered or dynamic_calls != [ORIGINALS[0], "lookup.extra"]:
        raise RuntimeError("Synthetic dynamic tool refresh did not complete")
    print(json.dumps({"synthetic_only": True, "test_only_transport": True,
                      "distinct_originals_called_once_per_run_including_resume": True,
                      "dynamic_list_changed_refreshed_and_called_new_original": True,
                      "scoped_encoded_alias_deny_blocked_only_that_tool": True}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic MCP alias candidate probe failed; raw output withheld") from None
