"""Exercise candidate MCP aliases, Claude ToolSearch and a fake Responses origin.

Only synthetic local processes are used; this does not demonstrate a provider
cache hit, production proxy safety, actual plugins or user permissions.
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

from claude_fixture import isolated_environment
from live_support import temporary_translator
from probe_mcp_alias_candidate import ALIASES, MODEL, ORIGINALS
from probe_proxy_features import names_in_additional_tools, raw_json_array_items, raw_json_field


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)
        self.server.requests.append((raw, body))
        step = len(self.server.requests) - 1
        response = {"id": f"resp_fixture_{step}", "model": MODEL, "status": "in_progress"}
        events = [{"type": "response.created", "response": response}]
        if step <= 2:
            if step == 0:
                name, arguments = "ToolSearch", {"query": "select:" + ",".join(ALIASES)}
            else:
                name, arguments = ALIASES[step - 1], {}
            args = json.dumps(arguments)
            item = {"id": f"item_{step}", "type": "function_call", "call_id": f"call_{step}",
                    "name": name, "arguments": args, "status": "completed"}
            events.extend([
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.delta", "output_index": 0,
                 "item_id": item["id"], "delta": args},
                {"type": "response.function_call_arguments.done", "output_index": 0,
                 "item_id": item["id"], "arguments": args},
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


def main():
    if len(sys.argv) != 2:
        raise RuntimeError("A tested candidate translator path is required")
    binary = Path(sys.argv[1]).resolve()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude or not binary.is_file():
        raise RuntimeError("The locked Claude fixture and candidate translator are required")
    with tempfile.TemporaryDirectory(prefix="bridge-alias-toolsearch-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        marker = root / "calls"
        script = Path(__file__).with_name("mcp_alias_fixture.py").resolve()
        mcp = root / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
            "args": [str(script), "--fake-adapter", str(marker)]}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   CLAUDE_CODE_ATTRIBUTION_HEADER="0", ENABLE_TOOL_SEARCH="true", DISABLE_TELEMETRY="1",
                   MCP_CONNECTION_NONBLOCKING="0", CLAUDE_CODE_MCP_STARTUP_WAIT_MS="5000")
        spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
        upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
        try:
            with temporary_translator(binary, upstream, MODEL) as (port, key):
                env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key,
                           ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL)
                result = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp),
                    "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "json",
                    "--model", MODEL, "--tools", "ToolSearch", "--allowedTools", ",".join(["ToolSearch", *ALIASES]),
                    "--max-turns", "6", "-p", "Find and use both synthetic tools."],
                    cwd=work, env=env, capture_output=True, text=True, timeout=90)
                if result.returncode or json.loads(result.stdout).get("is_error"):
                    raise RuntimeError("Synthetic Claude session did not complete")
            calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
            if calls != list(ORIGINALS) or len(server.requests) < 4:
                raise RuntimeError("Synthetic tools were not each called once")
            before_raw, before = server.requests[0]
            after_raw, after = server.requests[1]
            first_names = {item.get("name") for item in before.get("tools", []) if isinstance(item, dict)}
            before_input = raw_json_array_items(raw_json_field(before_raw, "input"))
            after_input = raw_json_array_items(raw_json_field(after_raw, "input"))
            cache_key = before.get("prompt_cache_key")
            checks = {
                "aliases_deferred_then_revealed": "ToolSearch" in first_names and
                    not set(ALIASES).intersection(first_names) and set(ALIASES).issubset(names_in_additional_tools(after)),
                "distinct_originals_called_once": True,
                "structural_cache_key_stable": isinstance(cache_key, str) and bool(cache_key)
                    and all(body.get("prompt_cache_key") == cache_key for _, body in server.requests),
                "tool_definitions_stable": raw_json_field(before_raw, "tools") is not None
                    and raw_json_field(before_raw, "tools") == raw_json_field(after_raw, "tools"),
                "instructions_stable": raw_json_field(before_raw, "instructions") is not None
                    and raw_json_field(before_raw, "instructions") == raw_json_field(after_raw, "instructions"),
                "history_prefix_stable": bool(before_input) and after_input is not None
                    and after_input[:len(before_input)] == before_input,
            }
            if not all(checks.values()):
                raise RuntimeError("Synthetic ToolSearch/cache structure check failed: "
                                   + ", ".join(name for name, passed in checks.items() if not passed))
            print(json.dumps({"synthetic_only": True, "provider_cache_not_tested": True,
                              "test_only_transport": True, **checks}, sort_keys=True))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic MCP alias gateway probe failed; raw output withheld") from None
