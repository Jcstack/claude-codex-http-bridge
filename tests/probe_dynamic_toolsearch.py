"""Observe dynamic MCP ToolSearch discovery with a local synthetic client.

This uses the test-only alias transport, not an installed plugin or provider.
All names, requests, tool results, and the model token are synthetic.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
from contextlib import nullcontext
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
from probe_mcp_alias_candidate import ALIASES, DYNAMIC_ALIAS, MODEL, ORIGINALS, run_dynamic_client
from probe_proxy_features import names_in_additional_tools, raw_json_array_items, raw_json_field


def search_result(body, call_id, alias, responses=False):
    """Check a result and its synthetic Messages reference or Responses tool."""
    if responses:
        seen = any(isinstance(item, dict) and item.get("type") == "function_call_output"
                   and item.get("call_id") == call_id for item in body.get("input", []))
        return seen, alias in names_in_additional_tools(body)
    def contains_reference(value):
        if isinstance(value, dict):
            if value.get("type") == "tool_reference" and value.get("tool_name") == alias:
                return True
            return any(contains_reference(item) for item in value.values())
        return isinstance(value, list) and any(contains_reference(item) for item in value)

    for message in body.get("messages", []):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            if (isinstance(part, dict) and part.get("type") == "tool_result"
                    and part.get("tool_use_id") == call_id):
                return True, contains_reference(part.get("content"))
    return False, False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)
        if "count_tokens" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        server = self.server
        server.captured.append((raw, body))
        step = server.requests
        server.requests += 1
        names = {item.get("name") for item in body.get("tools", []) if isinstance(item, dict)}
        name, arguments, text = None, {}, "DONE"
        if server.phase == "search_old":
            server.search_offered = "ToolSearch" in names
            name, arguments, server.phase = "ToolSearch", {"query": "select:" + ALIASES[0]}, "call_old"
            call_id = "fixture_search_old"
        elif server.phase == "call_old":
            server.old_result_seen, server.old_reference = search_result(
                body, "fixture_search_old", ALIASES[0], server.responses)
            if server.old_result_seen and server.old_reference:
                name, server.phase = ALIASES[0], "search_new"
            else:
                text = "OLD_SEARCH_UNAVAILABLE"
            call_id = f"fixture_call_{step}"
        elif server.phase == "search_new":
            server.new_searches += 1
            if server.new_searches == 1:
                server.first_search_before_server_list = not server.refresh_marker.exists()
                server.second_marker.touch()
            call_id = f"fixture_search_new_{server.new_searches}"
            server.last_search = call_id
            name, arguments, server.phase = "ToolSearch", {"query": server.new_query}, "inspect_new"
        elif server.phase == "inspect_new":
            seen, found = search_result(body, server.last_search, DYNAMIC_ALIAS, server.responses)
            server.new_results_seen += int(seen)
            if server.new_searches == 1:
                server.first_new_reference = found
            if seen and found:
                server.new_reference = True
                name, server.phase = DYNAMIC_ALIAS, "done"
            else:
                text, server.phase = "REFRESH_PENDING", "search_new"
            call_id = f"fixture_call_{step}"
        else:
            call_id = f"fixture_call_{step}"

        calling = name is not None
        if server.responses:
            self.send_response_stream(step, call_id, name, arguments, text)
            return
        stop = "tool_use" if calling else "end_turn"
        message = {"id": f"msg_fixture_{step}", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        block = ({"type": "tool_use", "id": call_id, "name": name, "input": {}} if calling
                 else {"type": "text", "text": ""})
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(arguments)} if calling
                 else {"type": "text_delta", "text": text})
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": block},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}},
                  {"type": "message_stop"}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass

    def send_response_stream(self, step, call_id, name, arguments, text):
        response = {"id": f"resp_fixture_{step}", "model": MODEL, "status": "in_progress"}
        events = [{"type": "response.created", "response": response}]
        if name:
            args = json.dumps(arguments)
            item = {"id": f"item_{step}", "type": "function_call", "call_id": call_id,
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
            item = {"id": f"msg_fixture_{step}", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
            events.extend([
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "content": [], "status": "in_progress"}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": text},
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--natural-query", action="store_true")
    parser.add_argument("--hold-refresh", action="store_true")
    parser.add_argument("--require-discovery", action="store_true")
    parser.add_argument("--require-prefix", action="store_true")
    parser.add_argument("--translator", type=Path, help="Reviewed translator for a synthetic Responses origin")
    args = parser.parse_args()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    with tempfile.TemporaryDirectory(prefix="bridge-mcp-toolsearch-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        marker = root / "calls"
        script = Path(__file__).with_name("mcp_alias_fixture.py").resolve()
        mcp = root / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
            "args": [str(script), "--fake-adapter", str(marker),
                     "dynamic-barrier" if args.hold_refresh else "dynamic"]}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.responses = args.translator is not None
        server.captured = []
        server.phase, server.requests = "search_old", 0
        server.new_query = "lookup extra" if args.natural_query else "select:" + DYNAMIC_ALIAS
        server.refresh_marker = Path(str(marker) + ".refreshed")
        server.second_marker = Path(str(marker) + ".second-request")
        server.first_search_before_server_list = None
        server.first_new_reference = None
        server.search_offered = server.old_result_seen = server.old_reference = server.new_reference = False
        server.new_searches = server.new_results_seen = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                   ENABLE_TOOL_SEARCH="true", MCP_CONNECTION_NONBLOCKING="0",
                   CLAUDE_CODE_MCP_STARTUP_WAIT_MS="5000", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1")
        command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp), "--settings", str(settings),
                   "--permission-mode", "dontAsk", "--model", MODEL, "--tools", "ToolSearch", "--allowedTools",
                   ",".join(["ToolSearch", *ALIASES, DYNAMIC_ALIAS]), "--output-format", "json", "--max-turns", "8"]
        try:
            prompt = ("Use " + ALIASES[0] + " and find the extra lookup tool."
                      if args.natural_query else "Use the synthetic tools named " + ALIASES[0] + " and " + DYNAMIC_ALIAS + ".")
            spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                    "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
            upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
            translator = (temporary_translator(args.translator.resolve(), upstream, MODEL)
                          if args.translator is not None else nullcontext(None))
            with translator as route:
                if route:
                    port, key = route
                    env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key,
                               ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL)
                completed = run_dynamic_client(command, work, env, Path(str(marker) + ".forwarded"),
                                               prompt=prompt, delay=0.5)
            calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
            result = {"synthetic_only": True, "test_only_transport": True,
                "through_translator": server.responses,
                "discovery_form": "responses_additional_tools" if server.responses else "messages_tool_reference",
                "natural_query": args.natural_query, "intentional_refresh_barrier": args.hold_refresh,
                "toolsearch_offered": server.search_offered, "old_search_result_seen": server.old_result_seen,
                "old_discovery_found": server.old_reference, "first_original_called": bool(calls and calls[0] == ORIGINALS[0]),
                "changed_list_forwarded": Path(str(marker) + ".forwarded").exists(),
                "refresh_hold_timed_out": Path(str(marker) + ".hold-timeout").exists(),
                "first_search_request_before_server_prepared_list": server.first_search_before_server_list,
                "first_new_search_discovery": server.first_new_reference,
                "new_searches": server.new_searches, "new_search_results_seen": server.new_results_seen,
                "new_discovery_found": server.new_reference, "new_original_called": calls == [ORIGINALS[0], "lookup.extra"],
                "completed": completed}
            if server.responses and len(server.captured) >= 2:
                first_raw, first_body = server.captured[0]
                last_raw, _ = server.captured[-1]
                cache_key = first_body.get("prompt_cache_key")
                first_tools = raw_json_field(first_raw, "tools")
                first_instructions = raw_json_field(first_raw, "instructions")
                first_input = raw_json_array_items(raw_json_field(first_raw, "input"))
                last_input = raw_json_array_items(raw_json_field(last_raw, "input"))
                result.update(provider_cache_not_tested=True,
                    cache_key_stable=isinstance(cache_key, str) and bool(cache_key)
                        and all(body.get("prompt_cache_key") == cache_key for _, body in server.captured),
                    first_and_last_tools_identical=first_tools is not None
                        and first_tools == raw_json_field(last_raw, "tools"),
                    first_and_last_instructions_identical=first_instructions is not None
                        and first_instructions == raw_json_field(last_raw, "instructions"),
                    first_input_prefix_in_last=bool(first_input) and last_input is not None
                        and last_input[:len(first_input)] == first_input)
            print(json.dumps(result, sort_keys=True))
            if not (server.search_offered and server.old_result_seen and server.old_reference
                    and calls and calls[0] == ORIGINALS[0] and result["changed_list_forwarded"]):
                raise RuntimeError("Synthetic ToolSearch control did not exercise the initial tool and refresh")
            if result["new_original_called"] and not server.new_reference:
                raise RuntimeError("New synthetic tool was called without discovery")
            if args.require_discovery and not (completed and server.new_reference and result["new_original_called"]):
                raise RuntimeError("Dynamic synthetic tool was not found and called")
            if args.require_prefix and not all(result.get(field) is True for field in (
                    "provider_cache_not_tested", "cache_key_stable", "first_and_last_tools_identical",
                    "first_and_last_instructions_identical", "first_input_prefix_in_last")):
                raise RuntimeError("Synthetic translator cache structure changed or was not captured")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic dynamic ToolSearch probe failed; raw output withheld") from None
