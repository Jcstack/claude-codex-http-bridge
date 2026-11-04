"""Check real Claude tool execution at interrupted stream boundaries, without accounts.

Only synthetic Messages and an owned Write target are used. Tool hooks count
effects independently of request attempts. No model, user session or existing bridge
service is called. This is a safety gate, not a provider recovery benchmark.
"""

from claude_fixture import isolated_environment

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

from live_support import temporary_translator
from codex_stream_fixture import temporary_codex_translator
from diagnostics import proxy_summary, stream_observation


def result_hints(items):
    text = json.dumps(items).lower()
    return [label for label, needles in (
        ("permission", ("permission", "denied", "not allowed", "not permitted")),
        ("validation", ("validation", "invalid", "required")),
        ("path", ("file_path", "absolute", "directory", "path")),
        ("read_first", ("not been read", "read it first", "must read", "read the file")),
        ("missing", ("enoent", "does not exist", "not found")),
        ("hook", ("hook",)),
        ("stream", ("stream",)),
    ) if any(needle in text for needle in needles)]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw)
        if self.server.responses and getattr(self.server, "capture_contract", False):
            from probe_proxy_features import raw_json_field
            self.server.contracts.append({field: raw_json_field(raw, field) for field in
                ("model", "instructions", "input", "tools", "tool_choice", "reasoning", "prompt_cache_key")})
        if "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":1000}')
            return
        if self.server.responses:
            self.emit_responses(body)
            return
        tool_results = [block for message in body.get("messages", []) for block in message.get("content", [])
                        if isinstance(block, dict) and block.get("type") == "tool_result"]
        self.server.requests.append({"has_tool_result": bool(tool_results), "tool_result_hints": result_hints(tool_results)})
        count = len(self.server.requests)
        tool = not tool_results or not (self.server.work / "owned.txt").is_file()
        block = ({"type": "tool_use", "id": f"call_fixture_{count}", "name": "Write",
                  "input": {"file_path": (self.server.work / self.server.target_name).as_posix(), "content": "SYNTHETIC"}}
                 if tool else {"type": "text", "text": "DONE"})
        message = {"id": f"msg_fixture_{count}", "type": "message", "role": "assistant",
                   "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 1000, "output_tokens": 10}}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if tool else
                 {"type": "text_delta", "text": block["text"]})
        first = count == 1
        mode = self.server.mode.removeprefix("eof_")
        if first and mode == "partial_arguments":
            delta["partial_json"] = delta["partial_json"][:10]
        events = [
            {"type": "message_start", "message": message},
            {"type": "content_block_start", "index": 0,
             "content_block": {**block, "input": {}} if tool else {**block, "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool else "end_turn"},
             "usage": {"output_tokens": 10}},
            {"type": "message_stop"},
        ]
        cutoff = {"denied_target": 6, "healthy": 6, "partial_arguments": 3, "tool_block": 4, "stop_reason": 5, "message_stop": 6}[mode] if first else len(events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events[:cutoff]:
                self.emit(event)
            if first and self.server.mode.startswith("eof_"):
                self.connection.shutdown(socket.SHUT_WR)
            elif first and mode not in ("healthy", "denied_target"):
                self.emit({"type": "error", "error": {"type": "api_error", "message": "Synthetic incomplete-stream fixture"}})
        except OSError:
            pass

    def emit(self, event):
        self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
        self.wfile.flush()

    def emit_responses(self, body):
        results = [item for item in body.get("input", [])
                   if isinstance(item, dict) and item.get("type") == "function_call_output"]
        self.server.requests.append({"has_tool_result": bool(results), "tool_result_hints": result_hints(results)})
        count = len(self.server.requests)
        first = count == 1
        mode = self.server.mode.removeprefix("eof_").removeprefix("failed_").removeprefix("max_")
        response = {"id": f"resp_fixture_{count}", "model": body.get("model"), "status": "in_progress"}
        incomplete_retry = self.server.mode in ("max_partial_arguments", "max_arguments_done", "max_item_incomplete",
                                                "max_item_incomplete_then_completed") and not first
        if not incomplete_retry and (not results or not (self.server.work / "owned.txt").is_file()):
            arguments = json.dumps({"file_path": (self.server.work / self.server.target_name).as_posix(), "content": "SYNTHETIC"})
            item = {"id": f"fc_fixture_{count}", "type": "function_call", "call_id": f"call_fixture_{count}",
                    "name": "Write", "arguments": arguments, "status": "completed"}
            events = [
                {"type": "response.created", "response": response},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": item["id"],
                 "delta": arguments[:10] if first and mode == "partial_arguments" else arguments},
                {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": item["id"], "arguments": arguments},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ]
            if first and mode.startswith("item_incomplete"):
                events[-1] = {**events[-1], "item": {**item, "status": "incomplete"}}
            cutoff = {"denied_target": 6, "healthy": 6, "partial_arguments": 3, "arguments_done": 4, "item_incomplete": 5,
                      "item_incomplete_then_completed": 5,
                      "tool_item_done": 5, "response_completed": 6}[mode] if first else 6
        else:
            item = {"id": f"msg_fixture_{count}", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "DONE", "annotations": []}]}
            events = [
                {"type": "response.created", "response": response},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "content": [], "status": "in_progress"}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "DONE"},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ]
            cutoff = 6
        events.append({"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
                      "usage": {"input_tokens": 1000, "output_tokens": 10}}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events[:cutoff]:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            if first and self.server.mode.startswith("max_"):
                finished = mode in ("tool_item_done", "item_incomplete_then_completed")
                partial = {**item, "status": "completed" if finished else "in_progress"}
                if mode == "partial_arguments":
                    partial["arguments"] = arguments[:10]
                terminal_response = {**response, "status": "completed" if mode == "item_incomplete_then_completed" else "incomplete",
                                     "output": [partial], "usage": {"input_tokens": 1000, "output_tokens": 10}}
                if mode != "item_incomplete_then_completed":
                    terminal_response["incomplete_details"] = {"reason": "max_output_tokens"}
                terminal = {"type": "response.completed" if mode == "item_incomplete_then_completed" else "response.incomplete",
                            "response": terminal_response}
                self.wfile.write(("data: " + json.dumps(terminal) + "\n\n").encode())
                self.wfile.flush()
            elif first and self.server.mode.startswith("eof_"):
                self.connection.shutdown(socket.SHUT_WR)
            elif first and mode not in ("healthy", "denied_target"):
                error = getattr(self.server, "synthetic_error", {
                    "type": "server_error", "code": "request_timeout", "message": "Synthetic incomplete-stream fixture"})
                failure = ({"type": "response.failed", "response": {"status": "failed", "error": error}} if
                           self.server.mode.startswith("failed_") else {"type": "error", "error": error})
                self.wfile.write(("data: " + json.dumps(failure) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-binary", type=Path, help="Also exercise the actual Responses-to-Messages translator")
    parser.add_argument("--codex-executor", action="store_true", help="Exercise the Codex executor against a fake Responses upstream")
    parser.add_argument("--codex-observation", action="store_true", help="Exercise the opt-in Codex-executor observer")
    parser.add_argument("--codex-origin-binary", type=Path, help="Insert a second owned proxy before the fake upstream")
    parser.add_argument("--codex-managed-policy", action="store_true", help="Use the actual managed-backend cooldown/retry settings")
    parser.add_argument("--forwarded-incomplete", action="store_true", help="Emit the exact synthetic forwarded incomplete-stream error")
    parser.add_argument("--case", action="append", help="Run only the named synthetic case; may be repeated")
    parser.add_argument("--client-timeout", type=float, default=45, help="Maximum seconds for the disposable client")
    parser.add_argument("--debug-shapes", action="store_true", help="Classify disposable client debug lines on timeout")
    parser.add_argument("--file-output", action="store_true", help="Collect disposable client output through private files")
    args = parser.parse_args()
    if (args.codex_observation or args.codex_executor) and args.proxy_binary is None:
        parser.error("The Codex executor requires --proxy-binary")
    if args.forwarded_incomplete and not (args.codex_observation or args.codex_executor):
        parser.error("--forwarded-incomplete requires the Codex executor")
    if args.codex_origin_binary and not (args.codex_observation or args.codex_executor):
        parser.error("--codex-origin-binary requires the Codex executor")
    if args.codex_managed_policy and not (args.codex_observation or args.codex_executor):
        parser.error("--codex-managed-policy requires the Codex executor")
    if not 0 < args.client_timeout <= 120:
        parser.error("--client-timeout must be greater than zero and at most 120")
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if claude is None:
        raise RuntimeError("Install the pinned Claude fixture before running the safety gate")
    metrics = []
    modes = (("denied_target", "healthy", "partial_arguments", "arguments_done", "tool_item_done", "response_completed",
              "eof_partial_arguments", "eof_arguments_done", "eof_tool_item_done", "eof_response_completed",
              "failed_partial_arguments", "failed_tool_item_done", "max_partial_arguments", "max_arguments_done",
              "max_item_incomplete", "max_item_incomplete_then_completed", "max_tool_item_done") if args.proxy_binary else
             ("denied_target", "healthy", "partial_arguments", "tool_block", "stop_reason", "message_stop",
              "eof_partial_arguments", "eof_tool_block", "eof_stop_reason", "eof_message_stop"))
    if args.case:
        if any(case not in modes for case in args.case):
            parser.error("Unknown synthetic stream case")
        modes = tuple(dict.fromkeys(args.case))
    for mode in modes:
        with tempfile.TemporaryDirectory(prefix="bridge-stream-safety-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            work = root / "work"
            work.mkdir()
            config = root / "config"
            config.mkdir(mode=0o700)
            marker = root / "hooks"
            hook = root / "hook.py"
            hook.write_text('''import json,os,pathlib,sys
event=json.load(sys.stdin)
if event.get("hook_event_name")=="PreToolUse":
    data=event.get("tool_input",{})
    try:
        same=os.path.normcase(str(pathlib.Path(data["file_path"]).resolve()))==os.path.normcase(str(pathlib.Path(sys.argv[2]).resolve()))
    except (KeyError,TypeError,ValueError,OSError):
        same=False
    allowed=event.get("tool_name")=="Write" and same and data.get("content")=="SYNTHETIC"
    print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow" if allowed else "deny","permissionDecisionReason":"Owned synthetic fixture only"}}))
else:
    with pathlib.Path(sys.argv[1]).open("a") as handle:
        handle.write("write\\n")
''', encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"modelPicker": {"options": [
                {"model": "gpt-6.1-sol", "label": "Fixture", "behavesAs": "claude-sonnet-5-5"}],
                "replaceBuiltInOptions": True}, "hooks": {event: [{"matcher": "Write", "hooks": [
                    {"type": "command", "command": sys.executable, "args": [str(hook), str(marker), str(work / "owned.txt")], "timeout": 5}]}]
                    for event in ("PreToolUse", "PostToolUse")}}), encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.requests = []
            server.mode = mode
            if args.forwarded_incomplete:
                server.synthetic_error = {"type": "server_error", "code": "request_timeout",
                    "message": "stream error: stream disconnected before completion: stream closed before response.completed"}
            server.responses = bool(args.proxy_binary)
            server.work = work
            server.target_name = "blocked.txt" if mode == "denied_target" else "owned.txt"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env = isolated_environment(root)
            env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_AUTH_TOKEN="fixture-only",
                ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                ANTHROPIC_DEFAULT_SONNET_MODEL="gpt-6.1-sol", ANTHROPIC_DEFAULT_HAIKU_MODEL="gpt-6.1-sol",
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
            command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "json",
                "--model", "gpt-6.1-sol", "--tools", "Write",
                "--add-dir", work.as_posix(), "--max-turns", "4",
                "-p", "Write SYNTHETIC to owned.txt exactly once."]
            debug = root / "client-debug.log"
            if args.debug_shapes:
                command.extend(["--debug-file", str(debug)])
            stack = ExitStack()
            try:
                if args.proxy_binary:
                    spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                            "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
                    upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream",
                                "models": {"gpt-6.1-sol": spec}}
                    if args.codex_observation or args.codex_executor:
                        if args.codex_origin_binary:
                            inner_port, inner_key, _inner_log = stack.enter_context(
                                temporary_codex_translator(args.codex_origin_binary.resolve(), server.server_port))
                            port, key, proxy_log = stack.enter_context(temporary_codex_translator(
                                args.proxy_binary.resolve(), inner_port, upstream_prefix="/v1", upstream_key=inner_key,
                                managed_policy=args.codex_managed_policy))
                        else:
                            port, key, proxy_log = stack.enter_context(temporary_codex_translator(
                                args.proxy_binary.resolve(), server.server_port, managed_policy=args.codex_managed_policy))
                    else:
                        port, key = stack.enter_context(temporary_translator(args.proxy_binary.resolve(), upstream, "gpt-6.1-sol"))
                    env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key)
                try:
                    if args.file_output:
                        with tempfile.TemporaryFile(dir=root) as stdout, tempfile.TemporaryFile(dir=root) as stderr:
                            completed = subprocess.run(command, cwd=work, env=env, stdout=stdout, stderr=stderr,
                                                       timeout=args.client_timeout)
                            stdout.seek(0); stderr.seek(0)
                            result = SimpleNamespace(returncode=completed.returncode,
                                stdout=stdout.read(10_000_001).decode("utf-8", errors="replace"),
                                stderr=stderr.read(10_000_001).decode("utf-8", errors="replace"))
                            if len(result.stdout) > 10_000_000 or len(result.stderr) > 10_000_000:
                                raise RuntimeError("Synthetic client output exceeded the fixture limit")
                    else:
                        result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=args.client_timeout)
                except subprocess.TimeoutExpired:
                    detail = {"failed_case": mode, "reason": "client_timeout", "requests": len(server.requests),
                              "codex_observation": args.codex_observation,
                              "write_hook_count": len(marker.read_text(encoding="utf-8").splitlines()) if marker.exists() else 0,
                              "file_exists": (work / "owned.txt").exists()}
                    if args.codex_observation or args.codex_executor:
                        rows = [stream_observation(line) for line in proxy_log.read_text(encoding="utf-8", errors="replace").splitlines()]
                        detail["observation_outcomes"] = [row["outcome"] for row in rows if row is not None]
                        summary = proxy_summary(proxy_log, datetime.now(timezone.utc) - timedelta(hours=1))
                        detail["proxy_http_statuses"] = summary["completed_http_statuses"]
                        detail["proxy_inference_requests"] = summary["inference_requests"]
                    if args.debug_shapes and debug.is_file():
                        lines = debug.read_text(encoding="utf-8", errors="replace").lower().splitlines()
                        detail["debug_categories"] = {name: sum(any(term in line for term in terms) for line in lines)
                            for name, terms in (("retry", ("retrying", "retry after", "retry in")),
                                ("api_error", ("api error", "apierror", "api_error", "server_error")),
                                ("stream", ("stream",)), ("tool", ("tool_use", "tool use")),
                                ("timeout", ("timed out", "timeout")), ("abort", ("abort", "cancel")))}
                    print(json.dumps(detail), flush=True)
                    raise
                try:
                    final = json.loads(result.stdout)
                except ValueError:
                    print(json.dumps({"failed_case": mode, "reason": "client_result_not_json", "returncode": result.returncode,
                        "stdout_characters": len(result.stdout), "stderr_characters": len(result.stderr), "requests": len(server.requests)}), flush=True)
                    raise
                hooks = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
                target = work / "owned.txt"
                succeeded = result.returncode == 0 and final.get("is_error") is False
                try:
                    assert server.requests, "Client never reached the synthetic endpoint"
                    if mode == "healthy":
                        assert succeeded, "Healthy fixture did not complete successfully"
                    if mode == "denied_target":
                        assert not succeeded and not target.exists() and not (work / "blocked.txt").exists() and not hooks, "Scoped handler did not deny the other target"
                    assert len(hooks) <= 1, "A stream interruption executed the same logical write twice"
                    incomplete_tool = mode in ("max_partial_arguments", "max_arguments_done", "max_item_incomplete",
                                               "max_item_incomplete_then_completed")
                    if incomplete_tool:
                        assert not target.exists() and not hooks, "An unconfirmed tool call was executed"
                    elif succeeded:
                        assert len(hooks) == 1 and target.read_text(encoding="utf-8") == "SYNTHETIC", "Client reported success without completing the owned write"
                        assert any(request["has_tool_result"] for request in server.requests[1:]), "Continuation lost the committed tool result"
                    else:
                        assert final.get("is_error") is True or result.returncode != 0, "Client termination was ambiguous"
                    if args.codex_observation:
                        rows = [line for line in proxy_log.read_text(encoding="utf-8", errors="replace").splitlines()
                                if "codex stream observation:" in line]
                        assert all(str(root) not in line and "SYNTHETIC" not in line and "fixture-upstream" not in line
                                   for line in rows), "Synthetic observation leaked private content"
                        if mode == "eof_partial_arguments":
                            assert any("outcome=eof" in line for line in rows), "Synthetic EOF was not observed"
                        if mode == "failed_partial_arguments":
                            assert any("outcome=terminal_event" in line for line in rows), "Synthetic terminal error was not observed"
                except AssertionError as error:
                    # Every assertion string is fixed fixture code, never model output.
                    print(json.dumps({"failed_case": mode, "reason": str(error), "returncode": result.returncode,
                        "reported_success": succeeded, "requests": len(server.requests), "write_hook_count": len(hooks),
                        "file_exists": target.exists(), "exact_content": target.is_file() and target.read_text(encoding="utf-8") == "SYNTHETIC",
                        "permission_denials_count": len(final.get("permission_denials", [])),
                        "request_metadata": server.requests, "client_error_hints": result_hints(final.get("errors", []))}), flush=True)
                    raise
                metrics.append({"cut_after": mode, "reported_success": succeeded, "requests": len(server.requests),
                    "write_hook_count": len(hooks), "followup_has_tool_result": any(request["has_tool_result"] for request in server.requests[1:]),
                    "tool_write_at_most_once": True})
            finally:
                stack.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
    print(json.dumps({"mock_only": True, "through_translator": bool(args.proxy_binary),
                      "codex_observation": args.codex_observation, "managed_policy": args.codex_managed_policy,
                      "two_layers": bool(args.codex_origin_binary), "cases": metrics}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.TimeoutExpired):
        raise SystemExit("Synthetic stream-safety gate failed; raw output withheld") from None
