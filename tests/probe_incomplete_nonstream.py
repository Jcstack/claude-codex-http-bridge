"""Check request-scoped incomplete-tool failures through a disposable proxy."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request

from live_support import temporary_translator
from smoke_api_contract import post

MODEL = "gpt-6.1-sol"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.calls += 1
        count = self.server.calls
        if count == 2:
            terminal = "response.completed"
            output = [{"type": "message", "role": "assistant", "status": "completed", "content": [
                {"type": "output_text", "text": "DONE"}]}]
        else:
            terminal = "response.incomplete"
            output = [{"type": "function_call", "id": "item_fixture", "call_id": "call_fixture", "name": "Read",
                       "arguments": '{"file_path":"synthetic"}', "status": "in_progress" if count == 1 else "completed"}]
        response = {"id": "resp_fixture", "model": MODEL, "status": "completed" if count == 2 else "incomplete",
                    "output": output, "usage": {"input_tokens": 10, "output_tokens": 5}}
        if count != 2:
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        event = {"type": terminal, "response": response}
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()


def main() -> int:
    if len(sys.argv) != 2:
        raise RuntimeError("Usage: probe_incomplete_nonstream.py /path/to/proxy")
    binary = Path(sys.argv[1]).resolve()
    if not binary.is_file():
        raise RuntimeError("Candidate proxy is missing")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    spec = {"context_window": 500000, "input_modalities": ["text"], "supports_search_tool": True,
            "supported_reasoning_levels": [{"effort": level} for level in ("low", "medium", "high", "xhigh", "max")]}
    upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
    body = {"model": MODEL, "max_tokens": 100, "stream": False,
            "messages": [{"role": "user", "content": "Synthetic fixture"}],
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
    try:
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=json.dumps(body).encode(),
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                             "anthropic-version": "2023-06-01"})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15):
                    pass
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
            else:
                raise RuntimeError("Unconfirmed non-streaming tool was accepted")
            if status != 502 or server.calls != 1:
                raise RuntimeError("Unconfirmed tool error was retried or not surfaced as 502")
            healthy = post(port, key, body)
            if server.calls != 2 or healthy.get("stop_reason") != "end_turn":
                raise RuntimeError("Account did not remain available after the request-scoped error")
            confirmed = post(port, key, body)
            calls = [part for part in confirmed.get("content", []) if part.get("type") == "tool_use"]
            if server.calls != 3 or confirmed.get("stop_reason") != "tool_use" or len(calls) != 1:
                raise RuntimeError("A completed tool in an incomplete response was not preserved")
        print(json.dumps({"mock_only": True, "unconfirmed_http_502": True, "no_cross_request_replay": True,
                          "next_request_available": True, "confirmed_tool_preserved": True}, sort_keys=True))
        return 0
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError):
        raise SystemExit("Synthetic non-streaming incomplete-tool gate failed") from None
