"""Exercise colliding long gateway MCP names through a real isolated Claude client.

The model origin and tools are synthetic. Requests remain in memory, no user
configuration or account is used, and only pass/fail categories are printed.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import ExitStack
import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

from live_support import temporary_translator

MODEL = "gpt-6.1-sol"
FIRST = "mcp_github-12345678-9abc-def0-1234-56789abcdef0-12345678_create-branch"
SECOND = "mcp_github-0b9e7c1d-1111-2222-3333-444455556666-89abcdef_create-branch"
FULL = ["mcp__gateway__" + FIRST, "mcp__gateway__" + SECOND]


class SwitchingIngress(BaseHTTPRequestHandler):
    """Synthetic per-request handoff; never connects outside loopback."""
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.split("?", 1)[0] == "/v1/messages":
            self.server.messages += 1
        port, key = self.server.before if self.server.messages <= 2 else self.server.after
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() not in ("authorization", "x-api-key", "host", "connection", "content-length")}
        headers["Authorization"] = "Bearer " + key
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
        try:
            connection.request("POST", self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in ("connection", "content-length", "transfer-encoding"):
                    self.send_header(name, value)
            self.end_headers()
            while True:
                chunk = response.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk); self.wfile.flush()
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.server.requests.append(body)
        step = len(self.server.requests)
        response = {"id": "resp_fixture", "model": MODEL, "status": "in_progress"}
        events = [{"type": "response.created", "response": response}]
        if step <= 2:
            if step == 1:
                name, args = "ToolSearch", {"query": "select:" + ",".join(FULL)}
            else:
                tools = [tool for item in body.get("input", []) if item.get("type") == "additional_tools"
                         for tool in item.get("tools", [])]
                self.server.revealed = [(tool.get("name"), tool.get("description")) for tool in tools]
                selected = [tool.get("name") for tool in tools if "0b9e7c1d" in str(tool.get("name", ""))]
                name, args = (selected[0] if len(selected) == 1 else "unknown_fixture"), {}
            arguments = json.dumps(args)
            item = {"id": "item_fixture", "type": "function_call", "call_id": f"call_{step}",
                    "name": name, "arguments": arguments, "status": "completed"}
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
            item = {"id": "message_fixture", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "DONE", "annotations": []}]}
            events.extend([
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "content": [], "status": "in_progress"}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "DONE"},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ])
        events.append({"type": "response.completed", "response": {**response, "status": "completed",
                      "output": [item], "usage": {"input_tokens": 1000, "output_tokens": 20}}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--previous", type=Path, help="Switch from this old binary after the tool call")
    args = parser.parse_args()
    binary = args.binary.resolve()
    previous = args.previous.resolve() if args.previous else None
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude or not binary.is_file() or (previous and not previous.is_file()):
        raise RuntimeError("The locked Claude fixture and candidate proxy are required")
    with tempfile.TemporaryDirectory(prefix="bridge-gateway-names-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "claude"
        work.mkdir(); config.mkdir(mode=0o700)
        calls = root / "calls.jsonl"
        script = root / "mcp_fixture.py"
        script.write_text('''import json, sys
first, second, call_path = sys.argv[1:]
for line in sys.stdin:
    try:
        request = json.loads(line)
        method, ident = request.get("method"), request.get("id")
        if ident is None:
            continue
        if method == "initialize":
            result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2025-11-25"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": "Synthetic gateway tool",
                      "inputSchema": {"type": "object", "properties": {}}}
                      for name in (first, second)]}
        elif method == "tools/call":
            name = request.get("params", {}).get("name", "")
            with open(call_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(name) + "\\n")
            result = {"content": [{"type": "text", "text": "SECOND_OK" if name == second else "WRONG_TOOL"}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": ident,
                              "error": {"code": -32601, "message": "Unknown method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
    except (ValueError, TypeError):
        pass
''', encoding="utf-8")
        mcp = root / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
                       "args": [str(script), FIRST, SECOND, str(calls)]}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests, server.revealed = [], []
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        allowed = ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT",
                   "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP")
        env = {name: os.environ[name] for name in allowed if name in os.environ}
        env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   CLAUDE_CODE_ATTRIBUTION_HEADER="0", ENABLE_TOOL_SEARCH="true", DISABLE_TELEMETRY="1")
        spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                "supported_reasoning_levels": [{"effort": effort} for effort in ("low", "medium", "high", "xhigh", "max")]}
        upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
        try:
            with ExitStack() as stack:
                port, key = stack.enter_context(temporary_translator(binary, upstream, MODEL))
                if previous:
                    old = stack.enter_context(temporary_translator(previous, upstream, MODEL))
                    switch = ThreadingHTTPServer(("127.0.0.1", 0), SwitchingIngress)
                    switch.before, switch.after, switch.messages = old, (port, key), 0
                    switch_thread = threading.Thread(target=switch.serve_forever, daemon=True); switch_thread.start()
                    def close_switch():
                        switch.shutdown(); switch.server_close(); switch_thread.join(timeout=2)
                    stack.callback(close_switch)
                    port = switch.server_port
                env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key,
                           ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL)
                result = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp),
                    "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "json",
                    "--model", MODEL, "--tools", "ToolSearch", "--allowedTools", ",".join(["ToolSearch", *FULL]),
                    "--max-turns", "5", "-p", "Search and call the second synthetic gateway tool."],
                    cwd=work, env=env, capture_output=True, text=True, timeout=90)
                if result.returncode or json.loads(result.stdout).get("is_error"):
                    raise RuntimeError("Claude gateway fixture did not complete")
            names = [name for name, desc in server.revealed if desc == "Synthetic gateway tool"]
            if previous and len(server.requests) >= 3:
                later_tools = [tool for item in server.requests[2].get("input", []) if item.get("type") == "additional_tools"
                               for tool in item.get("tools", [])]
                newer = [tool.get("name") for tool in later_tools if tool.get("description") == "Synthetic gateway tool"]
                if names == newer:
                    raise RuntimeError("Cross-version fixture did not observe the expected cache-prefix change")
                names = newer
            if len(names) != 2 or len(set(names)) != 2 or any(not isinstance(name, str) or
                    len(name) > 64 or "create-branch" not in name for name in names):
                raise RuntimeError("Revealed gateway names were not readable and distinct")
            if "12345678" not in names[0] or "0b9e7c1d" not in names[1]:
                raise RuntimeError("Revealed names lost recognizable gateway instances")
            if not calls.is_file() or [json.loads(line) for line in calls.read_text().splitlines()] != [SECOND]:
                raise RuntimeError("Claude did not call the selected original MCP tool exactly once")
            if len(server.requests) < 3 or not any(item.get("type") == "function_call_output" and
                    "SECOND_OK" in str(item.get("output", "")) for item in server.requests[2].get("input", [])):
                raise RuntimeError("Selected MCP result did not reach the next model request")
            print(json.dumps({"mock_only": True, "cross_version_handoff": bool(previous),
                              "names_change_cached_prefix": bool(previous), "readable_distinct_names": True,
                              "real_claude_toolsearch": True, "selected_mcp_called_once": True,
                              "tool_result_replayed": True}, sort_keys=True))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None
