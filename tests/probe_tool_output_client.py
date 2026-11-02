"""Observe a locked Claude client receiving synthetic MCP tool calls.

Only an owned temporary MCP server, a loopback fake, and synthetic credentials
are used. The MCP server deliberately records calls without schema validation.
This is an observation, not a production guard or a real-provider test.
"""

import argparse
from contextlib import contextmanager, nullcontext
from http.client import HTTPConnection
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
from probe_dynamic_toolsearch import Handler as ResponsesHandler


MODEL = "gpt-6.1-sol"
NAME = "mcp__fixture__observe"
CALL = "fixture_tool_call"
SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}},
          "required": ["value"], "additionalProperties": False}


def mcp_server(marker):
    """Record only the synthetic argument type, with no tool-side validation."""
    for line in sys.stdin:
        message = json.loads(line)
        ident = message.get("id")
        if ident is None:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-11-25"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "observe", "description": "Synthetic observation", "inputSchema": SCHEMA}]}
        elif method == "tools/call":
            value = message.get("params", {}).get("arguments", {}).get("value")
            with marker.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"number": type(value) is int, "string": isinstance(value, str)}) + "\n")
            result = {"content": [{"type": "text", "text": "Synthetic call recorded"}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": ident,
                              "error": {"code": -32601, "message": "unknown"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)


class Handler(ResponsesHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        server = self.server
        server.requests += 1
        if server.responses:
            tools = body.get("tools", [])
            tools += [tool for item in body.get("input", []) if isinstance(item, dict)
                      and item.get("type") == "additional_tools" for tool in item.get("tools", [])]
            result_seen = any(isinstance(item, dict) and item.get("type") == "function_call_output"
                              and item.get("call_id") == CALL for item in body.get("input", []))
        else:
            tools = body.get("tools", [])
            results = [part for message in body.get("messages", []) if isinstance(message, dict)
                       and isinstance(message.get("content"), list) for part in message["content"]
                       if isinstance(part, dict) and part.get("type") == "tool_result"
                       and part.get("tool_use_id") == CALL]
            result_seen = bool(results)
            server.error_result_seen |= any(part.get("is_error") is True for part in results)
        matching = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == NAME]
        server.offered |= bool(matching)
        server.strict_seen |= any(tool.get("strict") is True for tool in matching)
        server.caller_field_seen |= any("allowed_callers" in tool for tool in matching)
        server.result_seen |= result_seen
        name = None if result_seen or not matching else NAME
        arguments = {"value": 123 if server.invalid else "synthetic"}
        if name:
            server.call_sent = True
        if server.responses:
            self.send_response_stream(server.requests, CALL, name, arguments, "SYNTHETIC_DONE")
            return
        block = ({"type": "tool_use", "id": CALL, "name": name, "input": {}} if name
                 else {"type": "text", "text": ""})
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(arguments)} if name
                 else {"type": "text_delta", "text": "SYNTHETIC_DONE"})
        message = {"id": "msg_synthetic", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": block},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "tool_use" if name else "end_turn"},
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


@contextmanager
def synthetic_policy_injector(port, key, policy):
    """Mutate only this fixture's tool definition and forward to loopback."""
    class Injector(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if (self.path.split("?", 1)[0] not in ("/v1/messages", "/v1/messages/count_tokens")
                    or self.headers.get("Authorization") != "Bearer synthetic-injector-key"):
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 8 * 1024 * 1024:
                    self.send_error(400)
                    return
                body = json.loads(self.rfile.read(length))
                matching = [tool for tool in body.get("tools", [])
                            if isinstance(tool, dict) and tool.get("name") == NAME]
                if len(matching) > 1:
                    self.send_error(400)
                    return
                if matching:
                    self.server.client_already_strict |= matching[0].get("strict") is True
                    self.server.client_caller_field |= "allowed_callers" in matching[0]
                    if policy == "strict":
                        matching[0]["strict"] = True
                    elif policy == "programmatic":
                        matching[0]["allowed_callers"] = ["code_execution_20260120"]
                    else:
                        self.send_error(400)
                        return
                    self.server.injected += 1
                encoded = json.dumps(body).encode()
                connection = HTTPConnection("127.0.0.1", port, timeout=30)
                try:
                    connection.request("POST", self.path, body=encoded,
                        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                                 "anthropic-version": "2023-06-01"})
                    response = connection.getresponse()
                    self.server.rejected += int(bool(matching) and response.status == 400)
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                finally:
                    connection.close()
                    self.close_connection = True
            except (OSError, ValueError, TypeError):
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Injector)
    server.injected = 0
    server.client_already_strict = False
    server.client_caller_field = False
    server.rejected = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def observe(claude, translator, invalid, denied, inject_policy=None, expect_rejection=False):
    with tempfile.TemporaryDirectory(prefix="bridge-output-client-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        marker = root / "synthetic-calls"
        mcp = root / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"fixture": {"command": sys.executable,
            "args": [str(Path(__file__).resolve()), "--mcp-server", str(marker)]}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings_doc = {"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}
        hook_marker = root / "synthetic-denial"
        if denied:
            hook = root / "deny.py"
            hook.write_text('import json,pathlib,sys\n'
                'json.load(sys.stdin)\npathlib.Path(sys.argv[1]).touch()\n'
                'print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse",'
                '"permissionDecision":"deny","permissionDecisionReason":"Synthetic denial"}}))\n', encoding="utf-8")
            settings_doc["hooks"] = {"PreToolUse": [{"matcher": NAME, "hooks": [{"type": "command",
                "command": sys.executable, "args": [str(hook), str(hook_marker)], "timeout": 5}]}]}
        settings.write_text(json.dumps(settings_doc), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.responses, server.invalid = translator is not None, invalid
        server.requests = 0
        server.offered = server.strict_seen = server.caller_field_seen = server.result_seen = server.error_result_seen = server.call_sent = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                   ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL, ENABLE_TOOL_SEARCH="false", MCP_CONNECTION_NONBLOCKING="0",
                   CLAUDE_CODE_MCP_STARTUP_WAIT_MS="5000", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1", HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
                   NO_PROXY="localhost,127.0.0.1", http_proxy="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9",
                   no_proxy="localhost,127.0.0.1")
        command = [str(claude), "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp), "--settings", str(settings),
                   "--permission-mode", "dontAsk", "--model", MODEL, "--tools", "", "--allowedTools", NAME,
                   "--output-format", "json", "--max-turns", "4"]
        command += ["-p", "Call the synthetic fixture tool."]
        spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
        upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
        route = temporary_translator(translator, upstream, MODEL) if translator else nullcontext(None)
        try:
            with route as proxy:
                if proxy:
                    port, key = proxy
                    env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key)
                injected = synthetic_policy_injector(port, key, inject_policy) if inject_policy else nullcontext(None)
                with injected as injector:
                    if injector:
                        env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{injector.server_port}",
                                   ANTHROPIC_AUTH_TOKEN="synthetic-injector-key")
                    completed = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=60)
                    calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
                    final = json.loads(completed.stdout)
                    client_error = bool(completed.returncode or final.get("is_error"))
                    if expect_rejection:
                        if not (client_error and injector and injector.injected and injector.rejected
                                and server.requests == 0 and not calls):
                            raise RuntimeError("Synthetic caller restriction was not rejected before the fake")
                        return {"policy_injected": inject_policy, "client_reported_error": client_error,
                                "policy_request_rejected_400": bool(injector.rejected),
                                "fake_upstream_contacted": bool(server.requests), "call_count": len(calls),
                                "client_sent_caller_field_before_injection": injector.client_caller_field}
                    if client_error:
                        raise RuntimeError("Synthetic Claude client did not complete")
                    if not (server.offered and server.call_sent and server.result_seen):
                        raise RuntimeError("Synthetic tool-call control did not complete")
                    if injector and not injector.injected:
                        raise RuntimeError("Synthetic policy injection was not observed")
                    if inject_policy == "strict" and not server.strict_seen:
                        raise RuntimeError("Synthetic strict injection was not observed at the fake")
                    return {"invalid_arguments": invalid, "permission_denied": denied, "through_translator": bool(translator),
                            "call_count": len(calls), "number_reached_mcp": any(call.get("number") for call in calls),
                            "string_reached_mcp": any(call.get("string") for call in calls),
                            "client_result_returned": server.result_seen, "strict_seen_by_fake": server.strict_seen,
                            "client_sent_strict_before_injection": injector.client_already_strict if injector else None,
                            "client_sent_caller_field_before_injection": injector.client_caller_field if injector else None,
                            "caller_field_seen_by_fake": server.caller_field_seen,
                            "deny_hook_called": hook_marker.exists(),
                            "messages_error_result": server.error_result_seen if not translator else None}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-server", type=Path)
    parser.add_argument("--translator", type=Path)
    parser.add_argument("--inject-strict", action="store_true", help="Inject strict into only the synthetic client request")
    parser.add_argument("--inject-programmatic-only", action="store_true", help="Inject only a synthetic caller restriction")
    parser.add_argument("--expect-programmatic-rejection", action="store_true", help="Require rejection before the fake upstream")
    options = parser.parse_args()
    if options.mcp_server:
        mcp_server(options.mcp_server)
        return
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The pinned Claude client is required")
    translator = options.translator.resolve() if options.translator else None
    if (options.inject_strict or options.inject_programmatic_only) and not translator:
        raise RuntimeError("Policy injection requires a disposable translator")
    if options.inject_strict and options.inject_programmatic_only:
        raise RuntimeError("Choose one synthetic policy")
    if options.expect_programmatic_rejection and not options.inject_programmatic_only:
        raise RuntimeError("The rejection control requires programmatic-only injection")
    if options.inject_programmatic_only:
        result = observe(claude, translator, False, False, "programmatic", options.expect_programmatic_rejection)
        if result["client_sent_caller_field_before_injection"] is not False:
            raise RuntimeError("The locked client unexpectedly supplied a caller policy")
        if not options.expect_programmatic_rejection and not (result["call_count"] == 1
                and result["string_reached_mcp"] and result["caller_field_seen_by_fake"] is False):
            raise RuntimeError("Synthetic caller-policy loss and direct-call control did not match")
        print(json.dumps({"synthetic_only": True, "real_claude_client": True, "real_provider": False,
                          "programmatic_only_field_injected": True, "observation": result}, sort_keys=True))
        return
    controls = [observe(claude, translator, invalid, denied, "strict" if options.inject_strict else None) for invalid, denied in
                ((False, False), (True, False), (True, True))]
    if not (controls[0]["call_count"] == 1 and controls[0]["string_reached_mcp"]
            and controls[2]["deny_hook_called"] and controls[2]["call_count"] == 0):
        raise RuntimeError("Synthetic positive or denied control failed")
    print(json.dumps({"synthetic_only": True, "real_claude_client": True, "real_provider": False,
                      "strict_field_injected": options.inject_strict, "observations": controls}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic output-client probe failed; raw output withheld") from None
