"""Run real Claude against two stdio MCP servers and a synthetic model.

No model account, OAuth, project key, user config or external model calls.
Tests variadic --mcp-config coexistence, real PDF and image tool-result content.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

CALLS = [
    ("mcp__codex_native__image_models", {}),
    ("mcp__codex_native__image_generate", {"prompt": "Synthetic blue square", "size": "64x64"}),
    ("mcp__codex_native__document_extract", {"path": "fixture.pdf"}),
    ("mcp__codex_native__realtime_voice", {"prompt": "Fixture"}),
    ("mcp__extra__fixture_ping", {}),
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_GET(self): self.send_response(404); self.end_headers()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":1000}'); return
        self.server.requests.append(body)
        results = [b for m in body.get("messages", []) for b in m.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        content = ([{"type": "tool_use", "id": "fixture_" + str(i), "name": name, "input": args}
                    for i, (name, args) in enumerate(CALLS)] if not results else
                   [{"type": "text", "text": "CAPABILITIES_FIXTURE_READY"}])
        stop = "tool_use" if not results else "end_turn"
        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "model": body.get("model"),
                   "content": content, "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 1000, "output_tokens": 100}}
        self.send_response(200)
        if not body.get("stream"):
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(message).encode()); return
        self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        events = [{"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}]
        for i, block in enumerate(content):
            start = {**block, "input": {}} if block["type"] == "tool_use" else {**block, "text": ""}
            delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if block["type"] == "tool_use"
                     else {"type": "text_delta", "text": block["text"]})
            events += [{"type": "content_block_start", "index": i, "content_block": start},
                       {"type": "content_block_delta", "index": i, "delta": delta},
                       {"type": "content_block_stop", "index": i}]
        events += [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 100}}, {"type": "message_stop"}]
        for event in events:
            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()


def main():
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude: raise RuntimeError("A pinned real Claude binary is required")
    with tempfile.TemporaryDirectory(prefix="bridge-mcp-client-") as temporary:
        root = Path(temporary); work = root / "workspace"; work.mkdir()
        private = root / "private"; private.mkdir(mode=0o700)
        config = root / "claude"; config.mkdir(mode=0o700)
        writer = PdfWriter(); page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject(); stream.set_data(b"BT /F1 12 Tf 10 100 Td (PDF_VIOLET_73) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream); writer.write(work / "fixture.pdf")
        fixture = Path(__file__).with_name("mcp_capability_fixture.py")
        paths = []
        for name, kind in (("codex_native", "native"), ("extra", "extra")):
            path = root / (name + ".json")
            path.write_text(json.dumps({"mcpServers": {name: {"command": sys.executable,
                            "args": [str(fixture), kind, str(private), str(work)]}}}), encoding="utf-8")
            paths.append(path)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
        env.update({"CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_AUTH_TOKEN": "fixture-only",
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-6.1-sol",
                    "ENABLE_TOOL_SEARCH": "false", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"})
        try:
            result = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(paths[0]),
                "--model", "gpt-6.1-sol", "--mcp-config", str(paths[1]), "--permission-mode", "dontAsk", "--tools", "",
                "--allowedTools", *(name for name, _ in CALLS), "--output-format", "json", "-p", "Use the fixture tools."],
                cwd=work, env=env, capture_output=True, text=True, timeout=90)
            if result.returncode:
                raise RuntimeError("Claude MCP fixture failed; raw output withheld")
            if json.loads(result.stdout).get("is_error"): raise RuntimeError("Claude returned an error; raw output withheld")
            offered = {t.get("name") for r in server.requests for t in r.get("tools", [])}
            assert all(name in offered for name, _ in CALLS), "Repeated MCP config flags discarded a server"
            results = {b["tool_use_id"]: b for r in server.requests for m in r.get("messages", []) for b in m.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "tool_result"}
            assert set(results) == {"fixture_"+str(i) for i in range(len(CALLS))}, "A tool result was lost"
            assert not any(r.get("is_error") for r in results.values()), "Tool permission/protocol failure"
            image = results["fixture_1"]["content"]
            assert any(b.get("type") == "image" and b.get("source", {}).get("media_type") == "image/png" for b in image)
            assert "64x64" in json.dumps(image) and "84x84" in json.dumps(image)
            assert "PDF_VIOLET_73" in json.dumps(results["fixture_2"])
            assert "VOICE_FIXTURE_READY" in json.dumps(results["fixture_3"])
            assert "EXTRA_PLUGIN_READY" in json.dumps(results["fixture_4"])
            print(json.dumps({"mock_only": True, "real_claude_mcp": "pass", "repeated_mcp_config": "pass",
                              "image_content": "pass", "pdf_text": "pass", "realtime_result": "pass", "plugin_coexistence": "pass"}))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__": main()
