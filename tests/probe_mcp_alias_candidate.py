"""Test candidate mapping with real Claude and only synthetic local servers."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from claude_fixture import isolated_environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_alias_policy import alias_for  # noqa: E402


MODEL = "gpt-6.1-sol"
ORIGINALS = ("lookup.fixture", "lookup_fixture")
ALIASES = ["mcp__gateway__" + alias_for(name, "gateway") for name in ORIGINALS]
DYNAMIC_ALIAS = "mcp__gateway__" + alias_for("lookup.extra", "gateway")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        self.server.bodies.append(body)
        step = len(self.server.bodies) - 1
        if self.server.removed_control and step > 1:
            for message in body.get("messages", []):
                if isinstance(message, dict) and isinstance(message.get("content"), list):
                    for part in message["content"]:
                        if (isinstance(part, dict) and part.get("type") == "tool_result"
                                and part.get("tool_use_id") == "fixture_call_1"):
                            self.server.stale_result_error = part.get("is_error") is True
        if self.server.dynamic and step == 1:
            # The request body is already fixed at this point. Waiting for the
            # server's changed list cannot update the tools in this request.
            self.server.second_before_refresh = not self.server.refresh_marker.exists()
            self.server.second_marker.touch()
            deadline = time.monotonic() + 8
            while not self.server.refresh_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if self.server.removed_control:
                deadline = time.monotonic() + 8
                while not self.server.forwarded_marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
        text_result = "DONE"
        if self.server.dynamic:
            offered = {item.get("name") for item in body.get("tools", []) if isinstance(item, dict)}
            if step == 0:
                name = ALIASES[0]
                self.server.first_tool_offered = name in offered
            elif self.server.removed_control:
                name = ALIASES[0] if step == 1 and ALIASES[0] in offered else None
                if name:
                    self.server.stale_call_sent = True
            elif not self.server.dynamic_call_sent and DYNAMIC_ALIAS in offered:
                name = DYNAMIC_ALIAS
                self.server.dynamic_call_sent = True
                self.server.dynamic_call_offered = True
            else:
                name = None
                if not self.server.dynamic_call_sent:
                    text_result = "REFRESH_PENDING"
        else:
            name = ALIASES[step] if step < len(ALIASES) else None
        calling = name is not None
        block = ({"type": "tool_use", "id": f"fixture_call_{step}", "name": name, "input": {}}
                 if calling else {"type": "text", "text": text_result})
        stop = "tool_use" if calling else "end_turn"
        message = {"id": f"msg_fixture_{step}", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        events = [
            {"type": "message_start", "message": message},
            {"type": "content_block_start", "index": 0,
             "content_block": {**block, "input": {}} if calling else {**block, "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": "{}"} if calling
                      else {"type": "text_delta", "text": text_result}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def run_dynamic_client(command, work, env, forwarded, *, prompt="Use the synthetic fixture tools.", delay=0.1,
                       session_out=None, forwarded_timeout=8):
    """Use one client and issue another user turn if refresh is still pending."""
    command = list(command)
    command[command.index("--output-format") + 1] = "stream-json"
    command += ["--input-format", "stream-json", "--verbose", "-p"]
    process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    events = queue.Queue()

    def read_events():
        for line in process.stdout:
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.put(event)
            except ValueError:
                pass
        events.put({"type": "fixture_eof"})

    thread = threading.Thread(target=read_events, daemon=True)
    thread.start()

    def turn():
        process.stdin.write(json.dumps({"type": "user", "message": {
            "role": "user", "content": prompt}}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 20
        while True:
            try:
                event = events.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if event.get("type") == "fixture_eof" or event.get("is_error"):
                return None
            if event.get("type") == "result":
                if session_out is not None and isinstance(event.get("session_id"), str):
                    session_out.append(event["session_id"])
                return event.get("result")

    try:
        for attempt in range(6):
            result = turn()
            if result == "DONE":
                return True
            if result != "REFRESH_PENDING":
                return False
            deadline = time.monotonic() + forwarded_timeout
            while not forwarded.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            if not forwarded.exists():
                return False
            # Forwarding is not an acknowledgement. Bounded later user turns
            # observe when the client actually offers the refreshed tool.
            if attempt:
                time.sleep(delay)
        return False
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        thread.join(timeout=2)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def run_case(claude, deny, *, resume=False, dynamic=False, hold_refresh=False, removed_control=False):
    if (resume and dynamic) or ((hold_refresh or removed_control) and not dynamic):
        raise ValueError("Unsupported synthetic fixture combination")
    with tempfile.TemporaryDirectory(prefix="bridge-mcp-alias-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        marker = root / "calls"
        script = Path(__file__).with_name("mcp_alias_fixture.py").resolve()
        mcp = root / "mcp.json"
        mcp_args = [str(script), "--fake-adapter", str(marker)]
        if dynamic:
            mcp_args.append("dynamic-barrier" if hold_refresh else "dynamic")
        mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
            "args": mcp_args}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.bodies = []
        server.dynamic = dynamic
        server.refresh_marker = Path(str(marker) + ".refreshed")
        server.forwarded_marker = Path(str(marker) + ".forwarded")
        server.second_marker = Path(str(marker) + ".second-request")
        server.second_before_refresh = None
        server.dynamic_call_sent = False
        server.dynamic_call_offered = False
        server.first_tool_offered = False
        server.removed_control = removed_control
        server.stale_call_sent = False
        server.stale_result_error = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                   ENABLE_TOOL_SEARCH="false", MCP_CONNECTION_NONBLOCKING="0",
                   CLAUDE_CODE_MCP_STARTUP_WAIT_MS="5000", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1")
        command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp), "--settings", str(settings),
                   "--permission-mode", "dontAsk", "--model", MODEL, "--tools", "", "--allowedTools",
                   ",".join([*ALIASES, DYNAMIC_ALIAS] if dynamic else ALIASES),
                   "--output-format", "json", "--max-turns", "5"]
        if deny:
            command += ["--disallowedTools", deny]
        try:
            if dynamic:
                completed = run_dynamic_client(command, work, env, server.forwarded_marker)
                message = {}
            else:
                result = subprocess.run([*command, "-p", "Use the synthetic fixture tools."], cwd=work,
                                        env=env, capture_output=True, text=True, timeout=60)
                try:
                    message = json.loads(result.stdout)
                    completed = result.returncode == 0 and not message.get("is_error") and "DONE" in message.get("result", "")
                except (ValueError, AttributeError, TypeError):
                    message = {}
                    completed = False
            offered = {item.get("name") for body in server.bodies for item in body.get("tools", [])
                       if isinstance(item, dict)}
            if resume and completed:
                session_id = message.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    raise RuntimeError("Synthetic session could not be resumed")
                server.bodies.clear()
                result = subprocess.run([*command, "--resume", session_id, "-p", "Use both synthetic tools again."],
                                        cwd=work, env=env, capture_output=True, text=True, timeout=60)
                try:
                    message = json.loads(result.stdout)
                    completed = (result.returncode == 0 and not message.get("is_error")
                                 and "DONE" in message.get("result", ""))
                except (ValueError, AttributeError, TypeError):
                    completed = False
                resumed_offered = {item.get("name") for body in server.bodies for item in body.get("tools", [])
                                   if isinstance(item, dict)}
                completed = completed and set(ALIASES).issubset(resumed_offered)
            calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
            observations = {"second_request_before_server_prepared_list": server.second_before_refresh,
                            "refresh_hold_timed_out": Path(str(marker) + ".hold-timeout").exists(),
                            "changed_list_forwarded": Path(str(marker) + ".forwarded").exists(),
                            "first_tool_offered": server.first_tool_offered,
                            "new_tool_called_only_when_offered": server.dynamic_call_offered,
                            "stale_call_sent": server.stale_call_sent,
                            "stale_result_error": server.stale_result_error,
                            "removed_call_reached_adapter": Path(str(marker) + ".removed-call-received").exists(),
                            "removed_call_rejected_by_adapter": Path(str(marker) + ".removed-call-rejected").exists(),
                            "new_tool_offered_per_request": [DYNAMIC_ALIAS in {
                                item.get("name") for item in body.get("tools", []) if isinstance(item, dict)
                            } for body in server.bodies] if dynamic else []}
            return completed, offered, calls, server.refresh_marker.exists(), observations
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main():
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    if sys.argv[1:] == ["--diagnose-removed-tool"]:
        ok, _, calls, refreshed, observations = run_case(
            claude, None, dynamic=True, hold_refresh=True, removed_control=True)
        old_called_once = calls == [ORIGINALS[0]]
        evidence = {key: observations[key] for key in (
            "changed_list_forwarded", "first_tool_offered", "refresh_hold_timed_out",
            "removed_call_reached_adapter", "removed_call_rejected_by_adapter",
            "second_request_before_server_prepared_list", "stale_call_sent", "stale_result_error")}
        print(json.dumps({"synthetic_only": True, "intentional_refresh_barrier": True,
            "completed": ok, "server_prepared_list": refreshed,
            "old_original_called_once": old_called_once, **evidence}, sort_keys=True))
        if not (ok and refreshed and old_called_once and observations["first_tool_offered"]
                and observations["changed_list_forwarded"]):
            raise RuntimeError("Synthetic removed-tool control did not settle safely")
        if not observations["stale_call_sent"]:
            raise RuntimeError("Synthetic stale-call control was not exercised")
        if not observations["stale_result_error"]:
            raise RuntimeError("Synthetic stale call did not return an error")
        return
    if sys.argv[1:] == ["--diagnose-dynamic-race"]:
        ok, offered, calls, refreshed, observations = run_case(claude, None, dynamic=True, hold_refresh=True)
        print(json.dumps({"synthetic_only": True, "intentional_refresh_barrier": True,
            "completed": ok, "server_prepared_list": refreshed,
            "new_tool_offered": DYNAMIC_ALIAS in offered,
            "expected_calls": calls == [ORIGINALS[0], "lookup.extra"], **observations}, sort_keys=True))
        if not (ok and refreshed and observations["changed_list_forwarded"]
                and observations["first_tool_offered"]
                and observations["new_tool_called_only_when_offered"]
                and calls == [ORIGINALS[0], "lookup.extra"]):
            raise RuntimeError("Synthetic dynamic refresh did not reach the new tool")
        return
    if sys.argv[1:]:
        raise RuntimeError("Unknown fixture option")
    def observed(label, *args, **kwargs):
        try:
            return run_case(claude, *args, **kwargs)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(json.dumps({"synthetic_only": True, "case": label, "exception_type": type(error).__name__}), flush=True)
            raise

    ok, offered, calls, _, _ = observed("resume", None, resume=True)
    denied_ok, denied_offered, denied_calls, _, _ = observed("deny", ALIASES[0])
    dynamic_ok, dynamic_offered, dynamic_calls, refreshed, observations = observed("dynamic", None, dynamic=True)
    checks = {
        "resume": {"completed": ok, "both_offered": set(ALIASES).issubset(offered),
                   "expected_calls": calls == list(ORIGINALS) * 2},
        "deny": {"completed": denied_ok, "allowed_offered": ALIASES[1] in denied_offered,
                 "expected_calls": denied_calls == [ORIGINALS[1]]},
        "dynamic": {"completed": dynamic_ok, "refreshed": refreshed, "new_offered": DYNAMIC_ALIAS in dynamic_offered,
                    "changed_list_forwarded": observations["changed_list_forwarded"],
                    "first_tool_offered": observations["first_tool_offered"],
                    "new_tool_called_only_when_offered": observations["new_tool_called_only_when_offered"],
                    "expected_calls": dynamic_calls == [ORIGINALS[0], "lookup.extra"]},
    }
    if any(not all(case.values()) for case in checks.values()):
        # Only synthetic boolean observations leave the fixture; do not emit
        # client output, request bodies, paths, or tool arguments on failure.
        print(json.dumps({"synthetic_only": True, "failed_checks": checks,
                          "dynamic_observations": observations}, sort_keys=True), flush=True)
        raise RuntimeError("Synthetic MCP alias candidate did not meet all controls")
    print(json.dumps({"synthetic_only": True, "test_only_transport": True,
                      "distinct_originals_called_once_per_run_including_resume": True,
                      "dynamic_list_changed_refreshed_and_called_new_original": True,
                      "scoped_encoded_alias_deny_blocked_only_that_tool": True,
                      "dynamic_observations": observations}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic MCP alias candidate probe failed; raw output withheld") from None
