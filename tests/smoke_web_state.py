"""Exercise native search replay through a real proxy and optional Claude client.

All upstream data is synthetic. No account, user project, credential, or external
model call is used. Replaces only disposable proxy processes between requests.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import uuid

from live_support import temporary_translator
from live_compare_translators import stream

MODEL = "gpt-6.1-sol"
ITEM = {"type": "web_search_call", "id": "ws_fixture", "status": "completed",
        "encrypted_content": "synthetic-search-state",
        "action": {"type": "search", "query": "fixture", "sources": [
            {"type": "url", "url": "https://fixture.invalid/docs", "title": "Fixture source"}]}}


class Upstream(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        output = [{"type": "message", "id": "msg_fixture", "role": "assistant", "phase": "final_answer",
                   "content": [{"type": "output_text", "text": "Fixture source retained.", "annotations": []}]}]
        if len(self.requests) == 1:
            output.insert(0, ITEM)
        response = {"id": "resp_fixture", "model": MODEL, "status": "completed", "output": output,
                    "usage": {"input_tokens": 1000, "output_tokens": 20}}
        events = [{"type": "response.created", "response": {"id": "resp_fixture", "model": MODEL}}]
        events.extend({"type": "response.output_item.done", "output_index": n, "item": item}
                      for n, item in enumerate(output))
        events.append({"type": "response.completed", "response": response})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for event in events:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()


def assert_replayed():
    assert len(Upstream.requests) == 2
    items = [i for i in Upstream.requests[-1]["input"] if i.get("type") == "web_search_call"]
    assert items == [ITEM], "Native search state was lost, changed, or duplicated on replay"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path); parser.add_argument("--claude-client", action="store_true")
    args = parser.parse_args(); binary = args.binary.resolve()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    upstream = {"state": {"port": server.server_port}, "key": "synthetic-upstream-key",
                "models": {MODEL: {"context_window": 872000, "supports_search_tool": True,
                                   "input_modalities": ["text", "image"],
                                   "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]}}}
    try:
        Upstream.requests = []
        messages = [{"role": "user", "content": "Find the synthetic fixture."}]
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            content, _ = stream(port, key, {"model": MODEL, "max_tokens": 100, "messages": messages})
        assert any(b.get("type") == "web_search_tool_result" for b in content)
        assert sum(b.get("signature", "").startswith("codex_web_search:v1:") for b in content) == 1
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            stream(port, key, {"model": MODEL, "max_tokens": 100, "messages": [*messages,
                {"role": "assistant", "content": content}, {"role": "user", "content": "Recall its source."}]})
        assert_replayed()
        result = {"mock_only": True, "streamed_search_replay_after_proxy_replacement": "pass"}
        if args.claude_client:
            claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
            if not claude: raise RuntimeError("Pinned Claude fixture binary is required")
            Upstream.requests = []
            with tempfile.TemporaryDirectory(prefix="bridge-search-client-") as temporary:
                root = Path(temporary); work = root / "workspace"; work.mkdir()
                config = root / "claude"; config.mkdir(mode=0o700)
                env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
                env.update({"CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL, "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
                            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"})
                sid = str(uuid.uuid4())
                for session_flag in ("--session-id", "--resume"):
                    with temporary_translator(binary, upstream, MODEL) as (port, key):
                        env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}", "ANTHROPIC_AUTH_TOKEN": key})
                        run = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config",
                            '{"mcpServers":{}}', "--model", MODEL, "--tools", "", "--permission-mode", "dontAsk",
                            "--output-format", "json", session_flag, sid, "-p", "Recall the synthetic source."],
                            cwd=work, env=env, capture_output=True, text=True, timeout=45)
                        if run.returncode or json.loads(run.stdout).get("is_error"):
                            raise RuntimeError("Isolated Claude search fixture failed; raw output withheld")
                assert_replayed()
            result["real_claude_resume_preserves_native_search_state"] = "pass"
        print(json.dumps(result))
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    main()
