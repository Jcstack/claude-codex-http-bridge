"""Native compaction replay against an isolated credential-free upstream.

Checks process/model replacement, repeated compaction, fresh images and tool
results, current instructions, normal max effort, SSE, and fail-closed trust.
The opaque fixture is synthetic; semantic retention needs the live fixture.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request

from live_support import temporary_translator


SOL, TERRA = "gpt-6.1-sol", "gpt-6.1-terra"
OPT_IN = {"X-Codex-Bridge-Native-Compaction": "1", "X-Claude-Code-Request-Class": "compaction"}


class Upstream(BaseHTTPRequestHandler):
    requests = []
    reject_compaction = False

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        compact = any(item.get("type") == "compaction_trigger" for item in body.get("input", []))
        if compact and self.reject_compaction:
            self.send_response(400); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'{"error":{"message":"Synthetic native operation unavailable"}}')
            return
        output = [{"type": "compaction", "encrypted_content": "synthetic-capsule-" + str(len(self.requests))}] if compact else [
            {"id": "msg_fixture", "type": "message", "role": "assistant", "phase": "final_answer",
             "content": [{"type": "output_text", "text": "READY", "annotations": []}]}]
        response = {"id": "resp_fixture", "object": "response", "status": "completed", "model": body["model"],
                    "output": output, "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 75}, "output_tokens": 10}}
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for event in [{"type": "response.created", "response": {"id": "resp_fixture", "model": body["model"]}},
                      {"type": "response.completed", "response": response}]:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()


def request(port, key, body, *, headers=None, count=False, stream=False):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages" + ("/count_tokens" if count else ""),
        data=json.dumps({**body, "stream": stream}).encode(), headers={"Authorization": "Bearer " + key,
        "Content-Type": "application/json", "anthropic-version": "2023-06-01", **(headers or {})})
    with opener.open(req, timeout=15) as response:
        if not stream:
            return json.load(response)
        return [json.loads(line[6:]) for line in response if line.startswith(b"data: ")]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_native_compaction.py /path/to/reviewed-proxy")
    binary = Path(sys.argv[1]).resolve()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    spec = {"context_window": 872000, "supports_search_tool": True, "input_modalities": ["text", "image"],
            "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]}
    upstream = {"state": {"port": server.server_port}, "key": "synthetic-bridge-signing-key", "models": {SOL: spec, TERRA: spec}}
    base = {"model": SOL, "max_tokens": 1000, "output_config": {"effort": "max"},
            "messages": [{"role": "user", "content": "Retain the fixture."}]}
    try:
        with temporary_translator(binary, upstream, SOL) as (port, key):
            compact = request(port, key, base, headers=OPT_IN)
            summary = compact["content"][0]["text"]
            assert "codex-native-context-v1:" in summary
            assert compact["usage"]["input_tokens"] == 25 and compact["usage"]["cache_read_input_tokens"] == 75
            assert "reasoning" not in Upstream.requests[-1]
        # A new translator with another model has no state from the old process.
        with temporary_translator(binary, upstream, TERRA) as (port, key):
            request(port, key, {**base, "model": TERRA,
                "messages": [{"role": "user", "content": "Independent subagent without the parent's summary"}]})
            messages = [{"role": "user", "content": summary}, {"role": "user", "content": [
                {"type": "text", "text": "Current request"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "call_fixture", "name": "lookup", "input": {"key": "violet"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_fixture", "content": "VIOLET_73"}]}]
            body = {**base, "model": TERRA, "system": "Current authoritative instructions", "messages": messages,
                    "tools": [{"name": "lookup", "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}}}]}
            request(port, key, body)
            sent = Upstream.requests[-1]
            assert sent["model"] == TERRA and sent["reasoning"]["effort"] == "max"
            assert "Current authoritative instructions" in json.dumps(sent)
            assert sent["input"][0]["type"] == "compaction" and sent["input"][0]["encrypted_content"].startswith("synthetic-capsule-")
            assert any(i.get("type") == "function_call_output" and i.get("output") == "VIOLET_73" for i in sent["input"])
            assert any(p.get("type") == "input_image" for i in sent["input"] for p in i.get("content", []) if isinstance(p, dict))
            assert "codex-native-context-v1:" not in json.dumps(sent)
            assert request(port, key, body, count=True)["input_tokens"] > 0
            compact_events = request(port, key, body, headers=OPT_IN, stream=True)
            assert compact_events[-1]["type"] == "message_stop"
            newer_summary = "".join(e.get("delta", {}).get("text", "") for e in compact_events)
            assert "codex-native-context-v1:" in newer_summary and newer_summary != summary
            assert sum(i.get("type") == "compaction" for i in Upstream.requests[-1]["input"]) == 1
            request(port, key, {**body, "messages": [{"role": "user", "content": newer_summary}, {"role": "user", "content": "Continue"}]})
            assert Upstream.requests[-1]["reasoning"]["effort"] == "max"
            corrupt = newer_summary.replace("codex-native-context-v1:", "codex-native-context-v1:!")
            before = len(Upstream.requests)
            for count in (False, True):
                try:
                    request(port, key, {**body, "messages": [{"role": "user", "content": corrupt}]}, count=count)
                except urllib.error.HTTPError as error:
                    assert error.code == 400
                    error.close()
                else:
                    raise AssertionError("untrusted context was accepted")
            assert len(Upstream.requests) == before
            Upstream.reject_compaction = True
            fallback = request(port, key, {**body, "messages": [{"role": "user", "content": "Summarize"}]}, headers=OPT_IN)
            assert fallback["content"][0]["text"] == "READY"
            assert Upstream.requests[-1]["reasoning"]["effort"] == "max"
            assert not any(i.get("type") == "compaction_trigger" for i in Upstream.requests[-1]["input"])
        print(json.dumps({"native_v2_compaction": True, "sse": True, "repeated_compaction": True,
            "process_and_model_handoff": True, "fresh_images_and_tool_results": True, "max_effort_preserved": True,
            "unrelated_request_does_not_clear_parent_state": True,
            "reported_cache_accounting": True, "untrusted_capsule_rejected": True, "original_summary_fallback": True}))
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__": main()
