"""Observe a changed synthetic MCP input schema with the locked Claude client.

The client, tool server, Messages endpoint, credentials and all tool effects
belong only to this temporary fixture. This is not a production transport.
"""

from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
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
from mcp_alias_fixture import UPDATED_SCHEMA
from probe_dynamic_toolsearch import Handler as ResponsesHandler, search_result
from probe_mcp_alias_candidate import ALIASES, MODEL, ORIGINALS, run_dynamic_client


def schema_flags(body, responses):
    """Classify only the fixture's known schema, without displaying request data."""
    if responses:
        tools = [tool for item in body.get("input", []) if isinstance(item, dict)
                 and item.get("type") == "additional_tools" for tool in item.get("tools", [])]
    else:
        tools = body.get("tools", [])
    schemas = [tool.get("parameters", tool.get("input_schema")) for tool in tools
               if isinstance(tool, dict) and tool.get("name") == ALIASES[0]]
    old = updated = False
    for schema in schemas:
        if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
            continue
        properties = schema["properties"]
        old |= "revision" not in properties
        updated |= "revision" in properties and "revision" in schema.get("required", [])
    return old, updated


def publish_synthetic_schema(body, path):
    """Expose one request's synthetic schema to the fixture transport only.

    This untrusted local file does not bind concurrent requests and is not a
    production design. No actual session or model request is inspected here.
    """
    tools = body.get("tools", [])
    matches = [tool.get("input_schema") for tool in tools if isinstance(tool, dict)
               and tool.get("name") == ALIASES[0] and isinstance(tool.get("input_schema"), dict)]
    if len(matches) != 1:
        path.unlink(missing_ok=True)
        return
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"name": ALIASES[0], "input_schema": matches[0]}), encoding="utf-8")
    os.replace(temp, path)


def tool_error(body, call_id, responses):
    if responses:
        return any(isinstance(item, dict) and item.get("type") == "function_call_output"
                   and item.get("call_id") == call_id for item in body.get("input", []))
    for message in body.get("messages", []):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        if any(isinstance(part, dict) and part.get("type") == "tool_result"
               and part.get("tool_use_id") == call_id and part.get("is_error") is True
               for part in message["content"]):
            return True
    return False


class Handler(ResponsesHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        server = self.server
        if server.guard_schema:
            if server.omit_schema:
                server.model_schema.unlink(missing_ok=True)
            else:
                publish_synthetic_schema(body, server.model_schema)
        step = server.requests
        server.requests += 1
        name, arguments, text = None, {}, "DONE"
        call_id = f"fixture_schema_{step}"
        if server.phase == "search_initial":
            server.search_offered |= any(isinstance(tool, dict) and tool.get("name") == "ToolSearch"
                                         for tool in body.get("tools", []))
            name, arguments, server.phase = "ToolSearch", {"query": "select:" + ALIASES[0]}, "call_initial"
            server.search_id = call_id
        elif server.phase == "call_initial":
            seen, found = search_result(body, server.search_id, ALIASES[0], server.responses)
            server.baseline_result_seen, server.baseline_reference = seen, found
            server.baseline_old, server.baseline_updated = schema_flags(body, server.responses)
            if seen and found:
                name, arguments = ALIASES[0], {"revision": "synthetic"}
                if server.omit_schema:
                    server.phase, server.stale_call_id, server.unknown_call_sent = "inspect_error", call_id, True
                else:
                    server.phase = "done" if server.fresh else "search_changed"
            else:
                text = "BASELINE_SEARCH_UNAVAILABLE"
        elif server.phase == "search_changed":
            server.searches += 1
            if server.searches == 1:
                server.first_before_list = not server.forwarded.exists()
                server.second_marker.touch()
            name, arguments, server.phase = "ToolSearch", {"query": "select:" + ALIASES[0]}, "inspect_changed"
            server.search_id = call_id
        elif server.phase == "inspect_changed":
            seen, found = search_result(body, server.search_id, ALIASES[0], server.responses)
            old, updated = schema_flags(body, server.responses)
            if seen and found and server.forwarded.exists():
                server.later_old += int(old)
                server.later_updated += int(updated)
            if seen and found and updated:
                server.phase, text = "done", "DONE"
            elif seen and found and old and server.forwarded.exists() and server.attempt_stale:
                # The fake model sends the old empty arguments once. The fake
                # server rejects them without recording a successful effect.
                name, arguments, server.phase = ALIASES[0], {}, "inspect_error"
                server.stale_call_id = call_id
                server.stale_call_sent = True
            else:
                server.phase, text = "search_changed", "REFRESH_PENDING"
        elif server.phase == "inspect_error":
            server.error_observed = tool_error(body, server.stale_call_id, server.responses)
            server.phase, text = "done", "DONE"

        if server.responses:
            self.send_response_stream(step, call_id, name, arguments, text)
            return
        calling = name is not None
        message = {"id": f"msg_schema_{step}", "type": "message", "role": "assistant", "model": MODEL,
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
                  {"type": "message_delta", "delta": {"stop_reason": "tool_use" if calling else "end_turn"},
                   "usage": {"output_tokens": 5}}, {"type": "message_stop"}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--attempt-stale-call", action="store_true")
    parser.add_argument("--guard-schema", action="store_true", help="Test an unsafe fixture-only comparison channel")
    parser.add_argument("--omit-model-schema", action="store_true", help="Test fixture-only refusal without a baseline")
    parser.add_argument("--translator", type=Path)
    args = parser.parse_args()
    if args.fresh and (args.resume or args.attempt_stale_call):
        raise ValueError("Incompatible synthetic controls")
    if args.attempt_stale_call and args.translator:
        raise ValueError("The direct Messages control checks the tool error flag")
    if args.guard_schema and not (args.attempt_stale_call or args.fresh):
        raise ValueError("The fixture-only guard needs a fresh or stale-call control")
    if args.omit_model_schema and not (args.guard_schema and args.fresh):
        raise ValueError("The missing-baseline control requires a fresh guarded fixture")
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    with tempfile.TemporaryDirectory(prefix="bridge-mcp-schema-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        marker = root / "calls"
        script = Path(__file__).with_name("mcp_alias_fixture.py").resolve()
        mcp = root / "mcp.json"

        def configure(mode):
            mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
                "args": [str(script), "--fake-adapter", str(marker), mode]}}}), encoding="utf-8")

        configure(("schema-guard-current" if args.fresh else "schema-guard-barrier") if args.guard_schema
                  else "schema-current" if args.fresh else "schema-barrier")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.responses = args.translator is not None
        server.phase, server.requests, server.searches = "search_initial", 0, 0
        server.fresh, server.attempt_stale = args.fresh, args.attempt_stale_call
        server.guard_schema, server.model_schema = args.guard_schema, Path(str(marker) + ".model-schema.json")
        server.omit_schema, server.unknown_call_sent = args.omit_model_schema, False
        server.search_offered = server.baseline_result_seen = server.baseline_reference = False
        server.baseline_old = server.baseline_updated = False
        server.later_old = server.later_updated = 0
        server.first_before_list = None
        server.stale_call_sent = server.error_observed = False
        server.forwarded, server.second_marker = Path(str(marker) + ".forwarded"), Path(str(marker) + ".second-request")
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
                   ",".join(["ToolSearch", *ALIASES]), "--output-format", "json", "--max-turns", "8"]
        spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
        upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
        translator = temporary_translator(args.translator.resolve(), upstream, MODEL) if args.translator else nullcontext(None)
        try:
            with translator as route:
                if route:
                    port, key = route
                    env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key,
                               ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL)
                sessions = []
                completed = run_dynamic_client(command, work, env, server.forwarded,
                    prompt="Use the synthetic fixture tool and check its input schema.", delay=0.5, session_out=sessions)
                calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
                initial = {"completed": completed, "toolsearch_offered": server.search_offered,
                    "search_result_seen": server.baseline_result_seen, "alias_discovered": server.baseline_reference,
                    "old_schema_at_baseline": server.baseline_old, "updated_schema_at_baseline": server.baseline_updated,
                    "changed_list_forwarded": server.forwarded.exists(), "first_search_before_list": server.first_before_list,
                    "later_old_schema_requests": server.later_old, "later_updated_schema_requests": server.later_updated,
                    "stale_call_sent": server.stale_call_sent, "tool_error_observed": server.error_observed,
                    "fixture_guard_rejected": Path(str(marker) + ".schema-guard-rejected").exists(),
                    "unknown_baseline_call_sent": server.unknown_call_sent,
                    "invalid_call_reached_fake_server": Path(str(marker) + ".schema-invalid").exists(),
                    "successful_synthetic_calls": len(calls)}
                expected_calls = [] if args.omit_model_schema else [ORIGINALS[0]]
                if not (server.search_offered and server.baseline_result_seen and server.baseline_reference
                        and calls == expected_calls and server.forwarded.exists()):
                    raise RuntimeError("Synthetic schema baseline or refreshed list was not exercised")
                if args.fresh:
                    if not (completed and server.baseline_updated):
                        raise RuntimeError("Fresh client did not expose the current synthetic schema")
                elif not (server.baseline_old and (server.later_old or server.later_updated)):
                    raise RuntimeError("No post-refresh synthetic schema was observed")
                if server.stale_call_sent and not (completed and server.error_observed):
                    raise RuntimeError("The synthetic stale call did not produce an observed tool error")
                if args.guard_schema and args.attempt_stale_call and not (server.stale_call_sent and initial["fixture_guard_rejected"]
                        and not initial["invalid_call_reached_fake_server"]):
                    raise RuntimeError("The fixture-only guard did not reject the synthetic stale call")
                if args.guard_schema and args.fresh and not args.omit_model_schema and initial["fixture_guard_rejected"]:
                    raise RuntimeError("The fixture-only guard rejected the current synthetic schema")
                if args.omit_model_schema and not (server.unknown_call_sent and completed and server.error_observed
                        and initial["fixture_guard_rejected"] and not initial["invalid_call_reached_fake_server"]):
                    raise RuntimeError("The fixture-only guard did not refuse an unknown schema")

                audit = resumed = None
                if args.resume:
                    if not sessions:
                        raise RuntimeError("Synthetic session identifier was unavailable")
                    import mcp_definition_audit
                    session_files = list((config / "projects").glob("*/" + sessions[-1] + ".jsonl"))
                    if len(session_files) != 1:
                        raise RuntimeError("Synthetic session file was unavailable")
                    snapshot = root / "synthetic-inventory.json"
                    snapshot.write_text(json.dumps({"tools": [{"name": ALIASES[0], "description": "Synthetic fixture",
                        "input_schema": UPDATED_SCHEMA}]}), encoding="utf-8")
                    retained, ambiguous, records, skipped = mcp_definition_audit.retained(session_files[0])
                    current, duplicates = mcp_definition_audit.inventory(snapshot)
                    audit = mcp_definition_audit.compare(retained, ambiguous, current, duplicates, records, skipped)
                    if audit["input_schema_differences"] != 1 or audit["description_differences"] != 0:
                        raise RuntimeError("Offline audit did not detect the synthetic schema change")
                    server.forwarded.unlink(missing_ok=True)
                    configure("schema-current")
                    server.phase, server.fresh = "search_initial", True
                    server.search_offered = server.baseline_result_seen = server.baseline_reference = False
                    server.baseline_old = server.baseline_updated = False
                    resumed_ok = run_dynamic_client([*command, "--resume", sessions[-1]], work, env, server.forwarded,
                        prompt="Use the synthetic fixture tool and check its input schema.", delay=0.5)
                    resumed = {"completed": resumed_ok, "current_list_forwarded": server.forwarded.exists(),
                        "search_result_seen": server.baseline_result_seen, "alias_discovered": server.baseline_reference,
                        "old_schema_seen": server.baseline_old, "updated_schema_seen": server.baseline_updated}
                    if not (resumed_ok and server.forwarded.exists() and server.baseline_result_seen
                            and server.baseline_reference and (server.baseline_old or server.baseline_updated)):
                        raise RuntimeError("Synthetic resumed-session observation was incomplete")
                print(json.dumps({"synthetic_only": True, "test_only_transport": True,
                    "fixture_only_guard": args.guard_schema, "through_translator": server.responses, "fresh": args.fresh,
                    "initial": initial, "resumed": resumed, "offline_audit": audit}, sort_keys=True))
                if os.environ.get("GITHUB_ACTIONS") == "true" and initial["later_old_schema_requests"] and not initial["later_updated_schema_requests"]:
                    print("::warning::Synthetic session retained an old MCP input schema; inspect the observation.")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic MCP schema probe failed; raw output withheld") from None
