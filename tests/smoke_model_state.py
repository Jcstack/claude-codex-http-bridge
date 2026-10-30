"""Verify reasoning/phase/tool replay across isolated proxy replacement.

Only synthetic opaque envelopes and a fake upstream are used. This is a state
translation test, not proof that a live in-flight HTTP stream survives restart.
"""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

from live_support import temporary_translator
from smoke_api_contract import post


MODEL = "gpt-6.1-sol"
raw_signature = bytearray(1 + 8 + 16 + 16 + 32)
raw_signature[0] = 0x80; raw_signature[8] = 1
SIGNATURE = base64.urlsafe_b64encode(raw_signature).decode()


class Upstream(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body)
        output = [{"id": "msg_final", "type": "message", "role": "assistant", "phase": "final_answer",
                   "content": [{"type": "output_text", "text": "DONE", "annotations": []}]}]
        if len(self.requests) == 1:
            output = [
                {"type": "reasoning", "id": "rs_fixture", "encrypted_content": SIGNATURE,
                 "summary": [{"type": "summary_text", "text": "Synthetic reasoning summary"}]},
                {"type": "message", "id": "msg_commentary", "role": "assistant", "phase": "commentary",
                 "content": [{"type": "output_text", "text": "Checking fixture", "annotations": []}]},
                {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture", "name": "lookup",
                 "arguments": '{"key":"violet"}', "status": "completed"},
            ]
        response = {"id": "resp_fixture", "object": "response", "model": MODEL, "status": "completed",
                    "output": output, "usage": {"input_tokens": 1000, "output_tokens": 30,
                                               "input_tokens_details": {"cached_tokens": 800}}}
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for event in [{"type": "response.created", "response": {"id": "resp_fixture", "model": MODEL, "status": "in_progress"}},
                      {"type": "response.completed", "response": response}]:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_model_state.py /path/to/proxy")
    binary = Path(sys.argv[1]).resolve()
    Upstream.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    spec = {"context_window": 872000, "supports_search_tool": True, "input_modalities": ["text", "image"],
            "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]}
    upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
    metadata = {"user_id": json.dumps({"session_id": "aabbccdd-1111-4222-8333-001122334455"})}
    tools = [{"name": "lookup", "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}}}]
    base = {"model": MODEL, "max_tokens": 200, "stream": False, "metadata": metadata, "tools": tools,
            "thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}}
    first_messages = [{"role": "user", "content": "Look up the synthetic fixture."}]
    try:
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            first = post(port, key, {**base, "messages": first_messages})
        assert any(b.get("type") == "thinking" and b.get("signature") == SIGNATURE for b in first["content"])
        assert first["usage"]["input_tokens"] == 200
        assert first["usage"]["cache_read_input_tokens"] == 800
        # This second, new process cannot rely on the first process's memory.
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            second = post(port, key, {**base, "messages": [*first_messages,
                {"role": "assistant", "content": first["content"]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_fixture", "content": "VIOLET_73"}]}]})
            body = Upstream.requests[1]; items = body["input"]
            assert any(i.get("type") == "reasoning" and i.get("encrypted_content") == SIGNATURE for i in items)
            assert any(i.get("type") == "message" and i.get("phase") == "commentary" for i in items)
            assert any(i.get("type") == "function_call" and i.get("call_id") == "call_fixture" for i in items)
            assert any(i.get("type") == "function_call_output" and i.get("output") == "VIOLET_73" for i in items)
            assert body["reasoning"]["effort"] == "max" and body.get("store") is False
            assert Upstream.requests[0].get("prompt_cache_key") and Upstream.requests[0]["prompt_cache_key"] == body.get("prompt_cache_key")
            schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}
            post(port, key, {**base, "output_config": {"effort": "high", "format": {"type": "json_schema", "schema": schema}},
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Describe the fixture image"},
                   {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}]}]})
            rich = Upstream.requests[2]
            assert rich["text"]["format"]["schema"] == schema
            assert any(part.get("type") == "input_image" and part.get("image_url", "").startswith("data:image/png;base64,")
                       for item in rich["input"] for part in item.get("content", []) if isinstance(part, dict))
        print(json.dumps({"encrypted_reasoning_replay_after_new_process": True,
                          "commentary_phase_replay": True, "tool_call_result_pairing": True,
                          "stable_session_cache_key_after_new_process": True,
                          "cached_input_not_double_counted": True, "max_effort_preserved": True,
                          "image_input_translation": True, "structured_schema_translation": True}))
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    main()
