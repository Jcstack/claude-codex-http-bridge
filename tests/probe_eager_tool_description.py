"""Observe a synthetic MCP description change with ToolSearch disabled."""

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
from mcp_alias_fixture import UPDATED_DESCRIPTION
from probe_mcp_alias_candidate import ALIASES, MODEL, ORIGINALS, run_dynamic_client


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"input_tokens":100}')
            return
        server = self.server
        step = server.requests
        server.requests += 1
        descriptions = [tool.get("description") for tool in body.get("tools", [])
                        if isinstance(tool, dict) and tool.get("name") == ALIASES[0]]
        old, updated = "Synthetic fixture" in descriptions, UPDATED_DESCRIPTION in descriptions
        if step == 0:
            server.initial_old, server.initial_updated = old, updated
            name, text = ALIASES[0], ""
        else:
            if step == 1:
                server.second_before_list = not server.refreshed.exists()
                server.second_marker.touch()
            server.later_old += int(old)
            server.later_updated += int(updated)
            name, text = None, "DONE" if updated else "REFRESH_PENDING"
        calling = name is not None
        message = {"id": f"msg_fixture_{step}", "type": "message", "role": "assistant", "model": MODEL,
                   "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 100, "output_tokens": 5}}
        block = ({"type": "tool_use", "id": f"fixture_call_{step}", "name": name, "input": {}} if calling
                 else {"type": "text", "text": ""})
        delta = ({"type": "input_json_delta", "partial_json": "{}"} if calling
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
    parser.add_argument("--fresh-description", action="store_true")
    args = parser.parse_args()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    with tempfile.TemporaryDirectory(prefix="bridge-eager-mcp-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        marker = root / "calls"
        script = Path(__file__).with_name("mcp_alias_fixture.py").resolve()
        mcp = root / "mcp.json"
        mode = "description-current" if args.fresh_description else "description-barrier"
        mcp.write_text(json.dumps({"mcpServers": {"gateway": {"command": sys.executable,
            "args": [str(script), "--fake-adapter", str(marker), mode]}}}), encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = server.later_old = server.later_updated = 0
        server.initial_old = server.initial_updated = False
        server.second_before_list = None
        server.refreshed = Path(str(marker) + ".refreshed")
        server.second_marker = Path(str(marker) + ".second-request")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                   ENABLE_TOOL_SEARCH="false", MCP_CONNECTION_NONBLOCKING="0",
                   CLAUDE_CODE_MCP_STARTUP_WAIT_MS="5000", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1")
        command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", str(mcp), "--settings", str(settings),
                   "--permission-mode", "dontAsk", "--model", MODEL, "--tools", "", "--allowedTools", ALIASES[0],
                   "--output-format", "json", "--max-turns", "8"]
        try:
            completed = run_dynamic_client(command, work, env, Path(str(marker) + ".forwarded"), delay=0.5)
            calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()] if marker.exists() else []
            result = {"synthetic_only": True, "toolsearch_disabled": True, "fresh_description": args.fresh_description,
                      "completed": completed, "initial_old_description": server.initial_old,
                      "initial_updated_description": server.initial_updated, "later_old_requests": server.later_old,
                      "later_updated_requests": server.later_updated, "old_original_called_once": calls == [ORIGINALS[0]],
                      "changed_list_forwarded": Path(str(marker) + ".forwarded").exists(),
                      "followup_before_server_prepared_list": server.second_before_list}
            print(json.dumps(result, sort_keys=True))
            if not (result["old_original_called_once"] and result["changed_list_forwarded"]):
                raise RuntimeError("Synthetic eager-tool control did not run and refresh")
            if args.fresh_description:
                if not (server.initial_updated and completed):
                    raise RuntimeError("Fresh synthetic eager client did not see the current description")
            elif not (server.initial_old and server.later_old + server.later_updated):
                raise RuntimeError("Synthetic eager description change was not observed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic eager-tool probe failed; raw output withheld") from None
