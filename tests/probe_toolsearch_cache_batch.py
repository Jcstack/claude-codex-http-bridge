"""Check real Claude ToolSearch batches through a fake Responses origin.

One and nine synthetic MCP tools are revealed in isolated sessions. Captured
requests stay in memory; no account, user configuration or network model is used.
This checks structural cache prefixes, not provider cache hits.
"""

from claude_fixture import isolated_environment

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

from live_support import temporary_translator
from probe_proxy_features import names_in_additional_tools, raw_json_array_items, raw_json_field


MODEL = "gpt-6.1-sol"
TOOLS = [f"lookup_synthetic_fixture_item_{index:02}" for index in range(9)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)
        self.server.requests.append((raw, body))
        first = len(self.server.requests) == 1
        response = {"id": "resp_fixture", "model": MODEL, "status": "in_progress"}
        events = [{"type": "response.created", "response": response}]
        if first:
            arguments = json.dumps({"query": "select:" + ",".join(self.server.selected)})
            item = {"id": "item_search", "type": "function_call", "call_id": "call_search",
                    "name": "ToolSearch", "arguments": arguments, "status": "completed"}
            events.extend([
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.delta", "output_index": 0,
                 "item_id": item["id"], "delta": arguments},
                {"type": "response.function_call_arguments.done", "output_index": 0,
                 "item_id": item["id"], "arguments": arguments},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ])
        else:
            item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "DONE", "annotations": []}]}
            events.extend([
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "content": [], "status": "in_progress"}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "DONE"},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ])
        events.append({"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
                      "usage": {"input_tokens": 1000, "output_tokens": 20}}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def run_case(claude: str, binary: Path, count: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="bridge-toolsearch-batch-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work = root / "work"
        work.mkdir()
        config = root / "claude"
        config.mkdir(mode=0o700)
        script = root / "fixture_mcp.py"
        script.write_text('''import json,sys
names = ''' + repr(TOOLS) + '''
for line in sys.stdin:
    try:
        request=json.loads(line); method=request.get("method"); ident=request.get("id")
        if ident is None: continue
        if method=="initialize":
            result={"protocolVersion":request.get("params",{}).get("protocolVersion","2025-11-25"),
                    "capabilities":{"tools":{}},"serverInfo":{"name":"synthetic","version":"1.0"}}
        elif method=="tools/list":
            result={"tools":[{"name":name,"description":"Synthetic lookup fixture",
                    "inputSchema":{"type":"object","properties":{"value":{"type":"string"}}}} for name in names]}
        elif method=="tools/call":
            result={"content":[{"type":"text","text":"SYNTHETIC"}]}
        else:
            print(json.dumps({"jsonrpc":"2.0","id":ident,"error":{"code":-32601,"message":"Unknown method"}}),flush=True)
            continue
        print(json.dumps({"jsonrpc":"2.0","id":ident,"result":result}),flush=True)
    except (ValueError,TypeError):
        pass
''', encoding="utf-8")
        mcp = root / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"batch": {"command": sys.executable, "args": [str(script)]}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        server.selected = ["mcp__batch__" + name for name in TOOLS[:count]]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   CLAUDE_CODE_ATTRIBUTION_HEADER="0", ENABLE_TOOL_SEARCH="true", DISABLE_TELEMETRY="1")
        spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                "supported_reasoning_levels": [{"effort": effort} for effort in ("low", "medium", "high", "xhigh", "max")]}
        upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
        try:
            with temporary_translator(binary, upstream, MODEL) as (port, key):
                env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key,
                           ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL)
                result = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp),
                    "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "json",
                    "--model", MODEL, "--tools", "ToolSearch", "--allowedTools", "ToolSearch",
                    "--max-turns", "5", "-p", "Search the synthetic tool fixtures."],
                    cwd=work, env=env, capture_output=True, text=True, timeout=90)
                if result.returncode or json.loads(result.stdout).get("is_error"):
                    raise RuntimeError("Real Claude ToolSearch fixture did not complete")
            if len(server.requests) < 2:
                raise RuntimeError("Synthetic origin did not receive the follow-up request")
            before_raw, before = server.requests[0]
            after_raw, after = server.requests[1]
            before_input = raw_json_array_items(raw_json_field(before_raw, "input"))
            after_input = raw_json_array_items(raw_json_field(after_raw, "input"))
            key = before.get("prompt_cache_key")
            before_tools = raw_json_field(before_raw, "tools")
            before_instructions = raw_json_field(before_raw, "instructions")
            selected = set(server.selected)
            initial_names = {item.get("name") for item in before.get("tools", []) if isinstance(item, dict)}
            return {"batch_size": count, "query_bytes": len(("select:" + ",".join(server.selected)).encode()),
                    "toolsearch_available_and_selected_tools_deferred":
                        "ToolSearch" in initial_names and not selected.intersection(initial_names),
                    "all_selected_tools_revealed": selected.issubset(names_in_additional_tools(after)),
                    "tool_definition_bytes_stable": before_tools is not None and before_tools == raw_json_field(after_raw, "tools"),
                    "instruction_bytes_stable": before_instructions is not None and before_instructions == raw_json_field(after_raw, "instructions"),
                    "previous_input_items_stable": bool(before_input) and after_input is not None and after_input[:len(before_input)] == before_input,
                    "session_cache_key_stable": isinstance(key, str) and bool(key) and key == after.get("prompt_cache_key")}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main() -> int:
    if len(sys.argv) != 2:
        raise RuntimeError("Usage: probe_toolsearch_cache_batch.py /path/to/proxy")
    binary = Path(sys.argv[1]).resolve()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude or not binary.is_file():
        raise RuntimeError("The locked Claude fixture and candidate proxy are required")
    results = [run_case(claude, binary, count) for count in (1, 9)]
    print(json.dumps({"mock_only": True, "provider_cache_not_tested": True, "cases": results}, sort_keys=True))
    return 0 if all(all(value for name, value in case.items() if name not in ("batch_size", "query_bytes"))
                    for case in results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic ToolSearch batch gate failed; raw output withheld") from None
