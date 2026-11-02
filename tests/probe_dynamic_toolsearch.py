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
import uuid

from claude_fixture import isolated_environment
from live_support import temporary_translator
from mcp_alias_fixture import UPDATED_DESCRIPTION
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


def description_flags(body, alias, responses):
    """Inspect only known synthetic definitions in the selected request form."""
    if responses:
        tools = [tool for item in body.get("input", []) if isinstance(item, dict)
                 and item.get("type") == "additional_tools" for tool in item.get("tools", [])]
    else:
        tools = body.get("tools", [])
    descriptions = [tool.get("description") for tool in tools if isinstance(tool, dict) and tool.get("name") == alias]
    return "Synthetic fixture" in descriptions, UPDATED_DESCRIPTION in descriptions


def cache_resume_facts(initial, resumed):
    """Compare synthetic request structure only; never return a key or body."""
    if not initial or not resumed:
        return {"complete": False, "provider_cache_not_tested": True}
    initial_raw, initial_body = initial[0]
    resumed_raw, resumed_body = resumed[0]
    initial_key, resumed_key = initial_body.get("prompt_cache_key"), resumed_body.get("prompt_cache_key")
    initial_valid = isinstance(initial_key, str) and bool(initial_key)
    resumed_valid = isinstance(resumed_key, str) and bool(resumed_key)
    initial_first = raw_json_array_items(raw_json_field(initial_raw, "input"))
    initial_last = raw_json_array_items(raw_json_field(initial[-1][0], "input"))
    resumed_first = raw_json_array_items(raw_json_field(resumed_raw, "input"))
    def prefix(older, newer):
        return older is not None and newer is not None and newer[:len(older)] == older
    def equal_field(name):
        left, right = raw_json_field(initial_raw, name), raw_json_field(resumed_raw, name)
        return left is not None and right is not None and left == right
    return {"complete": True, "provider_cache_not_tested": True,
            "initial_cache_key_present": initial_valid, "resumed_cache_key_present": resumed_valid,
            "cache_key_same_across_resume": initial_valid and resumed_valid and initial_key == resumed_key,
            "initial_cache_key_stable": initial_valid and all(body.get("prompt_cache_key") == initial_key for _, body in initial),
            "resumed_cache_key_stable": resumed_valid and all(body.get("prompt_cache_key") == resumed_key for _, body in resumed),
            "initial_and_resumed_tools_identical": equal_field("tools"),
            "initial_and_resumed_instructions_identical": equal_field("instructions"),
            "initial_first_input_prefix_of_resumed": prefix(initial_first, resumed_first),
            "initial_last_input_prefix_of_resumed": prefix(initial_last, resumed_first)}


def synthetic_deferred_records(config, session_id):
    """Summarize only this fixture's disposable session records, never their contents."""
    try:
        if str(uuid.UUID(session_id)) != session_id.lower():
            return {"available": False}
    except (ValueError, TypeError, AttributeError):
        return {"available": False}
    paths = list((config / "projects").glob("*/" + session_id + ".jsonl"))
    if len(paths) != 1 or not paths[0].is_file() or paths[0].stat().st_size > 16 * 1024 * 1024:
        return {"available": False}
    records = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "deferred_tools_record":
                records.append(value)
                return
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    with paths[0].open(encoding="utf-8") as handle:
        for line in handle:
            if len(line) > 2 * 1024 * 1024:
                return {"available": False}
            try:
                visit(json.loads(line))
            except ValueError:
                return {"available": False}
    def contains_named_description(value, description):
        if isinstance(value, dict):
            if (ALIASES[0] in (value.get("name"), value.get("tool_name"), value.get("toolName"))
                    and value.get("description") == description):
                return True
            return any(contains_named_description(child, description) for child in value.values())
        return isinstance(value, list) and any(contains_named_description(child, description) for child in value)

    return {"available": True, "record_count": len(records),
            "named_old_description_in_record": any(contains_named_description(record, "Synthetic fixture") for record in records),
            "named_updated_description_in_record": any(contains_named_description(record, UPDATED_DESCRIPTION) for record in records)}


def rewrite_synthetic_deferred_record(config, session_id):
    """Change one known description in this probe's own disposable session only."""
    if not config.parent.name.startswith("bridge-mcp-toolsearch-"):
        raise RuntimeError("Refusing to modify a non-fixture session")
    try:
        if str(uuid.UUID(session_id)) != session_id.lower():
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("Invalid synthetic session") from None
    paths = list((config / "projects").glob("*/" + session_id + ".jsonl"))
    if (len(paths) != 1 or config.is_symlink() or (config / "projects").is_symlink()
            or paths[0].parent.is_symlink() or paths[0].is_symlink()
            or not paths[0].resolve().is_relative_to(config.parent.resolve())
            or paths[0].stat().st_size > 16 * 1024 * 1024):
        raise RuntimeError("Synthetic session is unavailable")
    changed = 0

    def edit(value, inside_record=False):
        nonlocal changed
        if isinstance(value, dict):
            inside_record = inside_record or value.get("type") == "deferred_tools_record"
            if (inside_record and ALIASES[0] in (value.get("name"), value.get("tool_name"), value.get("toolName"))
                    and value.get("description") == "Synthetic fixture"):
                value["description"] = UPDATED_DESCRIPTION
                changed += 1
            for child in value.values():
                edit(child, inside_record)
        elif isinstance(value, list):
            for child in value:
                edit(child, inside_record)

    lines = []
    with paths[0].open(encoding="utf-8") as handle:
        for line in handle:
            if len(line) > 2 * 1024 * 1024:
                raise RuntimeError("Synthetic session record is too large")
            value = json.loads(line)
            before = changed
            edit(value)
            lines.append(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n" if changed != before else line)
    if changed != 1:
        raise RuntimeError("Synthetic description record was not unique")
    temporary = paths[0].with_name(".synthetic-record-update")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        os.replace(temporary, paths[0])
    finally:
        temporary.unlink(missing_ok=True)
    return True


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
            call_id = "fixture_search_resume" if server.resume_phase else "fixture_search_old"
            server.old_search_id = call_id
        elif server.phase == "call_old":
            server.old_result_seen, server.old_reference = search_result(
                body, server.old_search_id, ALIASES[0], server.responses)
            if server.description_change:
                server.old_description_seen, updated = description_flags(body, ALIASES[0], server.responses)
                server.updated_description_seen |= updated
            if server.old_result_seen and server.old_reference:
                name, server.phase = ALIASES[0], "done" if server.fresh_description else "search_new"
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
            seen, found = search_result(body, server.last_search, server.target_alias, server.responses)
            if server.description_change:
                old, found = description_flags(body, server.target_alias, server.responses)
                server.old_description_later_seen += int(old)
                server.updated_description_seen |= found
            server.new_results_seen += int(seen)
            if server.new_searches == 1:
                server.first_new_reference = found
            if seen and found:
                server.new_reference = True
                if server.description_change:
                    text, server.phase = "DONE", "done"
                else:
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
    parser.add_argument("--require-resume-structure", action="store_true")
    parser.add_argument("--require-fork-structure", action="store_true")
    parser.add_argument("--translator", type=Path, help="Reviewed translator for a synthetic Responses origin")
    parser.add_argument("--description-change", action="store_true")
    parser.add_argument("--fresh-description", action="store_true")
    parser.add_argument("--resume-observation", action="store_true")
    parser.add_argument("--fork-observation", action="store_true")
    parser.add_argument("--repair-synthetic-record", action="store_true")
    parser.add_argument("--audit-synthetic", action="store_true")
    args = parser.parse_args()
    if args.description_change and args.natural_query:
        raise RuntimeError("The synthetic description observation needs the exact selector")
    if args.fresh_description and (not args.description_change or args.hold_refresh):
        raise RuntimeError("The fresh-description control cannot hold the initial list")
    if args.resume_observation and (not args.description_change or args.fresh_description):
        raise RuntimeError("The resume control requires the description-change scenario")
    if args.repair_synthetic_record and not args.resume_observation:
        raise RuntimeError("The synthetic record experiment requires the resume control")
    if args.audit_synthetic and (not args.resume_observation or args.repair_synthetic_record):
        raise RuntimeError("The synthetic audit needs the unchanged resume observation")
    if args.require_resume_structure and (not args.resume_observation or args.translator is None):
        raise RuntimeError("The resume structure check requires the fake Responses translator route")
    if args.fork_observation and (not args.resume_observation or args.repair_synthetic_record or args.audit_synthetic):
        raise RuntimeError("The fork observation needs an unchanged synthetic resume fixture")
    if args.require_fork_structure and (not args.fork_observation or args.translator is None):
        raise RuntimeError("The fork structure check requires the fake Responses translator route")
    if args.fork_observation and args.require_resume_structure:
        raise RuntimeError("A fork is not the same-session resume control")
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
        mode = ("description-current" if args.fresh_description else
                ("description" if args.description_change else "dynamic") + ("-barrier" if args.hold_refresh else ""))
        mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
            "args": [str(script), "--fake-adapter", str(marker), mode]}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.responses = args.translator is not None
        server.captured = []
        server.phase, server.requests = "search_old", 0
        server.target_alias = ALIASES[0] if args.description_change else DYNAMIC_ALIAS
        server.new_query = "lookup extra" if args.natural_query else "select:" + server.target_alias
        server.description_change = args.description_change
        server.fresh_description = args.fresh_description
        server.resume_phase = False
        server.old_description_seen = server.updated_description_seen = False
        server.old_description_later_seen = 0
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
            prompt = ("Use " + ALIASES[0] + " and check its updated description." if args.description_change else
                      "Use " + ALIASES[0] + " and find the extra lookup tool." if args.natural_query else
                      "Use the synthetic tools named " + ALIASES[0] + " and " + DYNAMIC_ALIAS + ".")
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
                sessions = []
                completed = run_dynamic_client(command, work, env, Path(str(marker) + ".forwarded"),
                                               prompt=prompt, delay=0.5, session_out=sessions)
                calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
                captured = list(server.captured)
                initial = {name: getattr(server, name) for name in (
                    "search_offered", "old_result_seen", "old_reference", "first_search_before_server_list",
                    "first_new_reference", "new_searches", "new_results_seen", "new_reference",
                    "old_description_seen", "old_description_later_seen", "updated_description_seen")}
                initial["forwarded"] = Path(str(marker) + ".forwarded").exists()
                initial["hold_timed_out"] = Path(str(marker) + ".hold-timeout").exists()
                resumed = None
                audit_result = None
                resumed_captured = None
                if args.resume_observation and sessions:
                    initial_records = synthetic_deferred_records(config, sessions[-1])
                    if args.audit_synthetic:
                        import mcp_definition_audit
                        if not initial_records.get("available"):
                            raise RuntimeError("Synthetic session for audit is unavailable")
                        session_files = list((config / "projects").glob("*/" + sessions[-1] + ".jsonl"))
                        if len(session_files) != 1:
                            raise RuntimeError("Synthetic session for audit is unavailable")
                        snapshot = root / "synthetic-inventory.json"
                        snapshot.write_text(json.dumps({"tools": [{"name": ALIASES[0], "description": UPDATED_DESCRIPTION,
                            "input_schema": {"type": "object", "properties": {}}}]}), encoding="utf-8")
                        old, ambiguous, records, skipped = mcp_definition_audit.retained(session_files[0])
                        current, duplicates = mcp_definition_audit.inventory(snapshot)
                        audit_result = mcp_definition_audit.compare(old, ambiguous, current, duplicates, records, skipped)
                        if audit_result["description_differences"] != 1 or audit_result["input_schema_differences"] != 0:
                            raise RuntimeError("Synthetic offline audit did not detect the changed description")
                    rewritten = rewrite_synthetic_deferred_record(config, sessions[-1]) if args.repair_synthetic_record else False
                    for path in (server.refresh_marker, Path(str(marker) + ".forwarded")):
                        path.unlink(missing_ok=True)
                    mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
                        "args": [str(script), "--fake-adapter", str(marker), "description-current"]}}}), encoding="utf-8")
                    server.phase, server.resume_phase, server.fresh_description = "search_old", True, True
                    server.captured = []
                    server.old_description_seen = server.updated_description_seen = False
                    server.old_result_seen = server.old_reference = server.search_offered = False
                    continuation = [*command, "--resume", sessions[-1]]
                    if args.fork_observation:
                        continuation.append("--fork-session")
                    continued_sessions = []
                    resumed_ok = run_dynamic_client(continuation, work, env,
                        Path(str(marker) + ".forwarded"), prompt=prompt, delay=0.5, session_out=continued_sessions)
                    resumed_captured = list(server.captured)
                    record_session = continued_sessions[-1] if args.fork_observation and continued_sessions else sessions[-1]
                    resumed = {"completed": resumed_ok,
                        "current_list_forwarded": Path(str(marker) + ".forwarded").exists(),
                        "search_result_seen": server.old_result_seen, "alias_discovered": server.old_reference,
                        "old_description_seen": server.old_description_seen,
                        "updated_description_seen": server.updated_description_seen,
                        "synthetic_record_rewritten": rewritten,
                        "forked_session_id_differs": (bool(continued_sessions) and continued_sessions[-1] != sessions[-1])
                            if args.fork_observation else None,
                        "initial_deferred_records": initial_records,
                        "resumed_deferred_records": synthetic_deferred_records(config, record_session)}
            result = {"synthetic_only": True, "test_only_transport": True,
                "through_translator": server.responses,
                "discovery_form": "responses_additional_tools" if server.responses else "messages_tool_reference",
                "description_change": args.description_change,
                "fresh_description": args.fresh_description,
                "fork_observation": args.fork_observation,
                "resume_observation": resumed,
                "synthetic_offline_audit": audit_result,
                "natural_query": args.natural_query, "intentional_refresh_barrier": args.hold_refresh,
                "toolsearch_offered": initial["search_offered"], "old_search_result_seen": initial["old_result_seen"],
                "old_discovery_found": initial["old_reference"], "first_original_called": bool(calls and calls[0] == ORIGINALS[0]),
                "changed_list_forwarded": initial["forwarded"], "refresh_hold_timed_out": initial["hold_timed_out"],
                "first_search_request_before_server_prepared_list": initial["first_search_before_server_list"],
                "first_new_search_discovery": initial["first_new_reference"],
                "new_searches": initial["new_searches"], "new_search_results_seen": initial["new_results_seen"],
                "new_discovery_found": initial["new_reference"], "new_original_called": calls == [ORIGINALS[0], "lookup.extra"],
                "completed": completed}
            if args.description_change:
                result.update(old_description_seen=initial["old_description_seen"],
                              old_description_later_requests=initial["old_description_later_seen"],
                              updated_description_seen=initial["updated_description_seen"],
                              original_called_once=calls == [ORIGINALS[0]])
            if server.responses and args.resume_observation:
                facts = cache_resume_facts(captured, resumed_captured)
                if not facts["complete"]:
                    raise RuntimeError("Synthetic initial or resumed translator requests were not captured")
                if args.fork_observation:
                    facts["cache_key_same_across_fork"] = facts.pop("cache_key_same_across_resume")
                    result["fork_cache_structure"] = facts
                else:
                    result["resume_cache_structure"] = facts
                if args.require_resume_structure and not all(facts.values()):
                    raise RuntimeError("Synthetic resume cache-key or prefix structure changed")
                if args.require_fork_structure and (facts["cache_key_same_across_fork"] or
                        not all(value for name, value in facts.items() if name != "cache_key_same_across_fork")):
                    raise RuntimeError("Synthetic fork cache-key or prefix structure changed")
            if server.responses and len(captured) >= 2:
                first_raw, first_body = captured[0]
                last_raw, _ = captured[-1]
                cache_key = first_body.get("prompt_cache_key")
                first_tools = raw_json_field(first_raw, "tools")
                first_instructions = raw_json_field(first_raw, "instructions")
                first_input = raw_json_array_items(raw_json_field(first_raw, "input"))
                last_input = raw_json_array_items(raw_json_field(last_raw, "input"))
                input_arrays = [raw_json_array_items(raw_json_field(raw, "input")) for raw, _ in captured]
                result.update(provider_cache_not_tested=True,
                    cache_key_stable=isinstance(cache_key, str) and bool(cache_key)
                        and all(body.get("prompt_cache_key") == cache_key for _, body in captured),
                    all_tools_identical=first_tools is not None
                        and all(raw_json_field(raw, "tools") == first_tools for raw, _ in captured),
                    all_instructions_identical=first_instructions is not None
                        and all(raw_json_field(raw, "instructions") == first_instructions for raw, _ in captured),
                    all_input_transitions_append_only=all(
                        older is not None and newer is not None and newer[:len(older)] == older
                        for older, newer in zip(input_arrays, input_arrays[1:])),
                    first_and_last_tools_identical=first_tools is not None
                        and first_tools == raw_json_field(last_raw, "tools"),
                    first_and_last_instructions_identical=first_instructions is not None
                        and first_instructions == raw_json_field(last_raw, "instructions"),
                    first_input_prefix_in_last=bool(first_input) and last_input is not None
                        and last_input[:len(first_input)] == first_input)
            print(json.dumps(result, sort_keys=True))
            if (os.environ.get("GITHUB_ACTIONS") == "true" and args.description_change
                    and not args.fresh_description and initial["old_description_later_seen"]
                    and not initial["updated_description_seen"]):
                print("::warning::Synthetic running session retained an old MCP tool description; inspect the observation.")
            if not (initial["search_offered"] and initial["old_result_seen"] and initial["old_reference"]
                    and calls and calls[0] == ORIGINALS[0] and result["changed_list_forwarded"]):
                raise RuntimeError("Synthetic ToolSearch control did not exercise the initial tool and refresh")
            if args.description_change:
                if args.fresh_description:
                    if not (completed and initial["updated_description_seen"] and result["original_called_once"]):
                        raise RuntimeError("Fresh synthetic session did not see the current description")
                elif not (initial["old_description_seen"] and initial["new_results_seen"] and result["original_called_once"]):
                    raise RuntimeError("Synthetic description-change control did not exercise its baseline")
                elif not (initial["old_description_later_seen"] or initial["updated_description_seen"]):
                    raise RuntimeError("Synthetic description-change result was not visible")
            if result["new_original_called"] and not initial["new_reference"]:
                raise RuntimeError("New synthetic tool was called without discovery")
            if args.require_discovery and not (completed and initial["new_reference"] and
                    (result["original_called_once"] if args.description_change else result["new_original_called"])):
                raise RuntimeError("Dynamic synthetic tool was not found and called")
            if args.resume_observation and not (resumed and resumed["completed"] and resumed["current_list_forwarded"]
                    and resumed["search_result_seen"] and resumed["alias_discovered"]
                    and (resumed["old_description_seen"] or resumed["updated_description_seen"])):
                raise RuntimeError("Synthetic resumed-session observation was incomplete")
            if args.fork_observation and not resumed["forked_session_id_differs"]:
                raise RuntimeError("Synthetic fork did not create a distinct session")
            if args.repair_synthetic_record and not (resumed and resumed["synthetic_record_rewritten"]
                    and resumed["updated_description_seen"]
                    and resumed["resumed_deferred_records"].get("named_updated_description_in_record") is True):
                raise RuntimeError("Synthetic record-change control did not expose the updated description")
            if args.require_prefix and not all(result.get(field) is True for field in (
                    "provider_cache_not_tested", "cache_key_stable", "all_tools_identical",
                    "all_instructions_identical", "all_input_transitions_append_only", "first_and_last_tools_identical",
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
