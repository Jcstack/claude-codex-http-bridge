"""Exercise installed Claude Code against a credential-free fake Messages server.

No real model calls. Verifies client behavior, not model quality or translator
behavior. Uses an isolated configuration and synthetic disposable workspace.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid


class FixtureServer(ThreadingHTTPServer):
    requests = None
    mode = "text"
    work = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(404); self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        body["_fixture_headers"] = {name: self.headers.get(name) for name in (
            "x-claude-code-request-class", "x-claude-code-compaction", "x-claude-code-context-compacted")}
        if "count_tokens" in self.path:
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(b'{"input_tokens":1000}'); return
        self.server.requests.append(body)
        results = [b for m in body.get("messages", []) for b in m.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        block = {"type": "text", "text": "<summary>FIXTURE_KEY=violet-73. Keep the key for the next turn.</summary>"}
        stop = "end_turn"
        mode = self.server.mode
        if mode in ("read", "deny") and not results:
            name = "Read" if mode == "read" else "Write"
            tool_input = {"file_path": str(self.server.work / ("fixture.txt" if mode == "read" else "blocked.txt"))}
            if mode == "deny":
                tool_input["content"] = "MUST_NOT_WRITE"
            block = {"type": "tool_use", "id": "tool_fixture", "name": name, "input": tool_input}
            stop = "tool_use"
        message = {"id": "msg_fixture_" + str(len(self.server.requests)), "type": "message",
                   "role": "assistant", "model": body.get("model"), "content": [block],
                   "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 1000, "output_tokens": 40}}
        self.send_response(200)
        if not body.get("stream"):
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(message).encode()); return
        self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        start_block = {**block, "text": ""} if block["type"] == "text" else {**block, "input": {}}
        delta = ({"type": "text_delta", "text": block["text"]} if block["type"] == "text" else
                 {"type": "input_json_delta", "partial_json": json.dumps(block["input"])})
        events = [
            {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0, "content_block": start_block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 40}},
            {"type": "message_stop"},
        ]
        for event in events:
            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()


def main():
    claude = shutil.which("claude")
    if claude is None:
        raise RuntimeError("Install Claude Code before running this optional probe")
    with tempfile.TemporaryDirectory(prefix="bridge-client-probe-") as temporary:
        root = Path(temporary); work = root / "workspace"; work.mkdir()
        config = root / "claude-config"; config.mkdir(mode=0o700)
        (work / "fixture.txt").write_text("FIXTURE_KEY=violet-73", encoding="utf-8")
        hook = root / "hook.py"
        hook.write_text("import json, pathlib, sys\nevent=json.load(sys.stdin)\n"
                        "pathlib.Path(sys.argv[1]).write_text(event.get('hook_event_name',''), encoding='utf-8')\n", encoding="utf-8")
        marker = root / "hook-marker"
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [
            {"model": model, "label": "Fixture", "behavesAs": "claude-sonnet-5-5"}
            for model in ("gpt-6.1-sol", "gpt-6.1-terra")], "replaceBuiltInOptions": True},
            "hooks": {"PostToolUse": [{"matcher": "Read", "hooks": [{"type": "command",
                "command": sys.executable, "args": [str(hook), str(marker)], "timeout": 5}]}]}}), encoding="utf-8")
        server = FixtureServer(("127.0.0.1", 0), Handler)
        server.requests = []; server.work = work
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
        env.update({"CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_AUTH_TOKEN": "fixture-only",
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-6.1-sol",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-6.1-sol",
                    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"})
        common = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                  "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "json"]

        def run(prompt, extra):
            before = len(server.requests)
            result = subprocess.run([*common, *extra, "-p", prompt], cwd=work, env=env,
                                    capture_output=True, text=True, timeout=45)
            if result.returncode:
                raise RuntimeError("Isolated Claude client exited unsuccessfully; raw output withheld")
            final = json.loads(result.stdout)
            if final.get("is_error"):
                raise RuntimeError("Isolated Claude client reported an error; raw output withheld")
            return server.requests[before:]

        try:
            sid = str(uuid.uuid4())
            seed = run("Remember FIXTURE_KEY=violet-73.", ["--session-id", sid, "--model", "gpt-6.1-sol", "--effort", "max", "--tools", ""])
            assert seed and seed[0]["_fixture_headers"]["x-claude-code-request-class"] == "main"
            assert not any("x-anthropic-billing-header:" in json.dumps(r.get("system")) for r in seed)
            compact = run("/compact Preserve the fixture key.", ["--resume", sid, "--model", "gpt-6.1-sol", "--effort", "max", "--tools", ""])
            assert compact and all(r.get("output_config", {}).get("effort") == "max" for r in compact)
            assert all(r["_fixture_headers"]["x-claude-code-request-class"] == "compaction" and
                       r["_fixture_headers"]["x-claude-code-compaction"] == "manual" for r in compact)
            transcripts = list((config / "projects").glob(f"*/{sid}.jsonl"))
            assert len(transcripts) == 1 and '"compact_boundary"' in transcripts[0].read_text(encoding="utf-8")
            resumed = run("Report the fixture key.", ["--resume", sid, "--model", "gpt-6.1-terra", "--effort", "high", "--tools", ""])
            assert resumed and all(r.get("model") == "gpt-6.1-terra" and r.get("output_config", {}).get("effort") == "high" for r in resumed)
            assert "violet-73" in json.dumps(resumed[0].get("messages"))
            server.mode = "read"
            read = run("Read fixture.txt.", ["--model", "gpt-6.1-sol", "--tools", "Read", "--allowedTools", "Read"])
            assert marker.read_text(encoding="utf-8") == "PostToolUse"
            assert any(b.get("type") == "tool_result" and not b.get("is_error")
                       for r in read for m in r.get("messages", []) for b in m.get("content", []) if isinstance(b, dict))
            server.mode = "deny"
            denied = run("Attempt to write blocked.txt.", ["--model", "gpt-6.1-sol", "--tools", "Write", "--disallowedTools", "Write"])
            assert not (work / "blocked.txt").exists()
            assert any(b.get("type") == "tool_result" and b.get("is_error")
                       for r in denied for m in r.get("messages", []) for b in m.get("content", []) if isinstance(b, dict))
            print(json.dumps({"claude_version": subprocess.check_output([claude, "--version"], text=True).strip(),
                              "mock_only": True, "resume_after_compact": "pass", "model_effort_after_resume": "pass",
                              "explicit_read_hook": "pass", "disallowed_write": "pass",
                              "compaction_hint_headers": "pass", "attribution_omitted": "pass",
                              "compaction_requested_effort": "max", "compaction_requests": len(compact)}))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    main()
