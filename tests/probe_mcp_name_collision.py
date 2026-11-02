"""Observe Claude MCP name collisions using only a synthetic direct Messages server.

No bridge, real MCP server, user configuration, credentials or external model
is involved. A negative observation does not certify other collision patterns.
"""

import argparse
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


MODEL = "gpt-6.1-sol"
DOTTED = "lookup.fixture"
UNDERSCORED = "lookup_fixture"
ALIAS = "mcp__gateway__lookup_fixture"
RAW_DENY = "mcp__gateway__lookup.fixture"

MCP_SCRIPT = r'''import json, sys
names, marker = json.loads(sys.argv[1]), sys.argv[2]
for line in sys.stdin:
    try:
        request = json.loads(line)
        ident, method = request.get("id"), request.get("method")
        if ident is None:
            continue
        if method == "initialize":
            result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2025-11-25"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": "Synthetic fixture",
                      "inputSchema": {"type": "object", "properties": {}}} for name in names]}
        elif method == "tools/call":
            with open(marker, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(request.get("params", {}).get("name")) + "\n")
            result = {"content": [{"type": "text", "text": "OK"}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": ident,
                             "error": {"code": -32601, "message": "unknown"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
    except (ValueError, TypeError):
        pass
'''


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
        first = len(self.server.bodies) == 1
        block = ({"type": "tool_use", "id": "fixture_call", "name": ALIAS, "input": {}} if first
                 else {"type": "text", "text": "DONE"})
        stop = "tool_use" if first else "end_turn"
        message = {"id": "msg_fixture_" + str(len(self.server.bodies)), "type": "message", "role": "assistant",
                   "model": MODEL, "content": [block], "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        events = [
            {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0,
             "content_block": {**block, "input": {}} if first else {**block, "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": "{}"} if first
                      else {"type": "text_delta", "text": "DONE"}},
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


def run_case(claude, names, deny):
    with tempfile.TemporaryDirectory(prefix="bridge-mcp-name-collision-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        marker, script = root / "calls", root / "mcp.py"
        script.write_text(MCP_SCRIPT, encoding="utf-8")
        mcp = root / "mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
            "args": [str(script), json.dumps(names), str(marker)]}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.bodies = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = isolated_environment(root)
        environment.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                           ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                           ENABLE_TOOL_SEARCH="false", MCP_CONNECTION_NONBLOCKING="0",
                           CLAUDE_CODE_MCP_STARTUP_WAIT_MS="5000", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                           DISABLE_TELEMETRY="1")
        command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp), "--settings", str(settings),
                   "--permission-mode", "dontAsk", "--model", MODEL, "--tools", "", "--allowedTools", ALIAS,
                   "--output-format", "json", "--max-turns", "5"]
        if deny:
            command += ["--disallowedTools", deny]
        try:
            result = subprocess.run([*command, "-p", "Use the synthetic fixture tool."], cwd=work,
                                    env=environment, capture_output=True, text=True, timeout=60)
            try:
                message = json.loads(result.stdout)
                completed = result.returncode == 0 and not message.get("is_error") and "DONE" in message.get("result", "")
            except (ValueError, AttributeError, TypeError):
                completed = False
            calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
            alias_counts = [sum(ALIAS == tool.get("name") for tool in body.get("tools", [])
                                if isinstance(tool, dict)) for body in server.bodies]
            offered = any(alias_counts)
            return {"client_completed": completed, "request_count": len(server.bodies), "alias_offered": offered,
                    "exactly_one_alias_per_model_request": bool(alias_counts) and all(count == 1 for count in alias_counts),
                    "called_dotted": calls == [DOTTED], "called_underscored": calls == [UNDERSCORED],
                    "no_tool_call": not calls, "single_tool_call": len(calls) == 1}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-reproduced", action="store_true",
                        help="Fail unless the specific order-sensitive collision is reproduced")
    args = parser.parse_args()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    dotted_first = run_case(claude, [DOTTED, UNDERSCORED], RAW_DENY)
    under_first = run_case(claude, [UNDERSCORED, DOTTED], RAW_DENY)
    deny_alias = run_case(claude, [DOTTED, UNDERSCORED], ALIAS)
    unique = run_case(claude, [UNDERSCORED], None)
    cases = [dotted_first, under_first, deny_alias, unique]
    if not all(case["client_completed"] and case["request_count"] >= 2 for case in cases):
        raise RuntimeError("Synthetic Claude MCP fixture did not complete")
    if not unique["called_underscored"] or not deny_alias["no_tool_call"]:
        raise RuntimeError("Synthetic MCP positive or normalized-deny control failed")
    collision = (dotted_first["alias_offered"] and under_first["alias_offered"]
                 and dotted_first["called_dotted"] and under_first["called_underscored"])
    print(json.dumps({"synthetic_only": True, "direct_messages_without_bridge": True,
        "order_sensitive_collision_observed": collision,
        "single_shared_definition_in_each_collision_case":
            dotted_first["exactly_one_alias_per_model_request"] and under_first["exactly_one_alias_per_model_request"],
        "raw_dotted_deny_did_not_block_dotted_call": dotted_first["called_dotted"],
        "normalized_alias_deny_blocked_both": deny_alias["no_tool_call"],
        "unique_tool_control_passed": unique["called_underscored"]}, sort_keys=True))
    if args.expect_reproduced and not collision:
        raise RuntimeError("The specific synthetic collision was not reproduced")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic MCP name-collision probe failed; raw output withheld") from None
