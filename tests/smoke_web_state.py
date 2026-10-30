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
import urllib.request
import uuid

from live_support import temporary_translator
from live_compare_translators import stream

MODEL = "gpt-6.1-sol"
TEXT = "Fixture source retained."
CITATION = {"type": "url_citation", "url": "https://fixture.invalid/docs", "title": "Fixture source",
            "start_index": 0, "end_index": 7}
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
                   "content": [{"type": "output_text", "text": TEXT, "annotations": [CITATION]}]}]
        if len(self.requests) == 1:
            output.insert(0, ITEM)
        response = {"id": "resp_fixture", "model": MODEL, "status": "completed", "output": output,
                    "usage": {"input_tokens": 1000, "output_tokens": 20}}
        events = [{"type": "response.created", "response": {"id": "resp_fixture", "model": MODEL}}]
        for index, item in enumerate(output):
            if item["type"] == "message":
                events.extend([
                    {"type": "response.content_part.added", "output_index": index, "content_index": 0,
                     "part": {"type": "output_text", "text": "", "annotations": []}},
                    {"type": "response.output_text.delta", "output_index": index, "content_index": 0, "delta": TEXT},
                    {"type": "response.output_text.annotation.added", "output_index": index, "content_index": 0,
                     "annotation_index": 0, "annotation": CITATION},
                    {"type": "response.content_part.done", "output_index": index, "content_index": 0, "part": item["content"][0]}])
            events.append({"type": "response.output_item.done", "output_index": index, "item": item})
        events.append({"type": "response.completed", "response": response})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
        self.end_headers()
        if not body.get("stream"):
            self.wfile.write(json.dumps(response).encode())
            return
        for event in events:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()


def assert_replayed():
    assert len(Upstream.requests) == 2
    items = [i for i in Upstream.requests[-1]["input"] if i.get("type") == "web_search_call"]
    assert items == [ITEM], "Native search state was lost, changed, or duplicated on replay"
    parts = [part for item in Upstream.requests[-1]["input"] if item.get("role") == "assistant"
             for part in item.get("content", []) if isinstance(part, dict) and part.get("type") == "output_text"]
    assert any(part.get("text") == TEXT and part.get("annotations") == [CITATION] for part in parts), "Native URL annotation was lost or changed on replay"


def assert_citation(content):
    parts = [block for block in content if block.get("type") == "text"]
    assert "".join(part.get("text", "") for part in parts) == TEXT, "Answer text was rewritten"
    citations = [citation for part in parts for citation in part.get("citations", [])]
    assert len(citations) == 1 and citations[0]["url"] == CITATION["url"] and citations[0]["title"] == CITATION["title"], "Citation missing, duplicated or mismatched"
    assert citations[0]["cited_text"] == "", "Generated answer must not masquerade as a source quotation"


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
        assert_citation(content)
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            stream(port, key, {"model": MODEL, "max_tokens": 100, "messages": [*messages,
                {"role": "assistant", "content": content}, {"role": "user", "content": "Recall its source."}]})
        assert_replayed()
        result = {"mock_only": True, "streamed_search_replay_after_proxy_replacement": "pass", "streamed_url_citation_replay": "pass"}
        Upstream.requests = []
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for turn in range(2):
                payload = {"model": MODEL, "max_tokens": 100, "stream": False, "messages": messages if turn == 0 else [*messages,
                           {"role": "assistant", "content": content}, {"role": "user", "content": "Recall its source."}]}
                request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=json.dumps(payload).encode(),
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "anthropic-version": "2023-06-01"})
                with opener.open(request, timeout=30) as response:
                    content = json.load(response)["content"]
                assert_citation(content)
        assert_replayed()
        result["nonstream_url_citation_replay"] = "pass"
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
            result["real_claude_resume_preserves_url_annotations"] = "pass"
        print(json.dumps(result))
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    main()
