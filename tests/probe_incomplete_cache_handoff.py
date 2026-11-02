"""Compare synthetic prompt/cache bytes before and after the safety patch."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

from live_support import temporary_translator
from probe_proxy_features import raw_json_field
from smoke_api_contract import post

MODEL = "gpt-6.1-sol"
LONG = "mcp__gateway__mcp_github-12345678-9abc-def0-1234-56789abcdef0-12345678_create-branch"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.server.requests.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        event = {"type": "response.completed", "response": {"id": "resp_fixture", "status": "completed", "model": MODEL,
                 "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [
                     {"type": "output_text", "text": "DONE"}]}], "usage": {"input_tokens": 10, "output_tokens": 1}}}
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()


def main() -> int:
    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != "--direct-policy"):
        raise RuntimeError("Usage: probe_incomplete_cache_handoff.py /path/to/previous /path/to/candidate [--direct-policy]")
    direct_policy = len(sys.argv) == 4
    binaries = [Path(value).resolve() for value in sys.argv[1:3]]
    if any(not value.is_file() for value in binaries):
        raise RuntimeError("Both candidate binaries are required")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    spec = {"context_window": 500000, "input_modalities": ["text"], "supports_search_tool": True,
            "supported_reasoning_levels": [{"effort": level} for level in ("low", "medium", "high", "xhigh", "max")]}
    upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
    tool = lambda name: {"name": name, "input_schema": {"type": "object", "properties": {}},
                         **({"allowed_callers": ["direct"]} if direct_policy else {})}
    tools = [tool("ToolSearch"), tool("Read"), {**tool(LONG), "defer_loading": True}]
    initial = [{"role": "user", "content": "Synthetic fixture"}]
    revealed = initial + [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "search_fixture", "name": "ToolSearch", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "search_fixture",
                                     "content": [{"type": "tool_reference", "tool_name": LONG}]}]},
    ]
    removed = revealed + [{"role": "system", "content": [{"type": "tool_removal",
                           "tool": {"type": "tool_reference", "name": LONG}}]}]
    base = {"model": MODEL, "max_tokens": 100, "stream": False, "system": "Synthetic stable instructions",
            "metadata": {"user_id": json.dumps({"session_id": "00000000-0000-4000-8000-000000000001"})},
            "tools": tools}
    try:
        snapshots = []
        for binary in binaries:
            start = len(server.requests)
            with temporary_translator(binary, upstream, MODEL) as (port, key):
                for messages in (initial, revealed, removed):
                    post(port, key, {**base, "messages": messages})
            snapshots.append(server.requests[start:])
        fields = ("model", "instructions", "tools", "input", "reasoning", "prompt_cache_key", "tool_choice")
        if any(len(snapshot) != 3 for snapshot in snapshots):
            raise RuntimeError("Fixture did not receive all synthetic requests")
        for previous, candidate in zip(*snapshots):
            if any(raw_json_field(previous, field) != raw_json_field(candidate, field) for field in fields):
                raise RuntimeError("Safety candidate changed a synthetic prompt/cache field")
            if any(raw_json_field(previous, field) is None for field in ("model", "tools", "input", "prompt_cache_key")):
                raise RuntimeError("Fixture lacked a required prompt/cache field")
        print(json.dumps({"mock_only": True, "three_dynamic_tool_stages": True,
                          "long_gateway_name_unchanged": True, "prompt_bytes_unchanged": True,
                          "session_cache_key_unchanged": True, "provider_cache_not_tested": True,
                          "explicit_direct_policy": direct_policy}, sort_keys=True))
        return 0
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError):
        raise SystemExit("Synthetic safety-patch cache handoff gate failed") from None
