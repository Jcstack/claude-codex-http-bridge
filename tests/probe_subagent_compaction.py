"""Observe per-model subagent compaction with synthetic token usage.

This uses a disposable real Claude client and a fake loopback Messages
service. It does not test model quality, provider limits or a real session.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_models  # noqa: E402


PARENT = "gpt-6-astra"
CHILD = "gpt-6.1-sol"
SYNTHETIC_USAGE = 160_000


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":1000}'); return
        is_child = bool(self.headers.get("x-claude-code-agent-id"))
        request_class = self.headers.get("x-claude-code-request-class")
        with self.server.lock:
            if is_child:
                for message in body.get("messages", []):
                    parts = message.get("content") if isinstance(message, dict) else None
                    for part in parts if isinstance(parts, list) else []:
                        if (isinstance(part, dict) and part.get("type") == "tool_result" and
                                isinstance(part.get("tool_use_id"), str) and part["tool_use_id"].startswith("fixture_read_") and
                                part.get("is_error") is not True):
                            self.server.successful_read_results.add(part["tool_use_id"])
            self.server.requests.append({"child": is_child, "class": request_class, "model": body.get("model")})
            if request_class == "compaction":
                kind = "summary"
            elif is_child:
                self.server.child_steps += 1
                kind = "read" if self.server.child_steps <= 2 else "done"
                if kind == "read": self.server.read_calls += 1
            elif not self.server.started:
                self.server.started = True
                kind = "agent"
            else:
                kind = "done"
        if kind == "agent":
            block = {"type": "tool_use", "id": "fixture_agent", "name": "Agent", "input": {
                "description": "Synthetic agent", "prompt": "Read fixture.txt and complete the task.",
                "subagent_type": "fixture-worker"}}
        elif kind == "read":
            block = {"type": "tool_use", "id": f"fixture_read_{self.server.child_steps}", "name": "Read",
                     "input": {"file_path": str(self.server.work / "fixture.txt")}}
        else:
            text = "<summary>Synthetic task remains active.</summary>" if kind == "summary" else "DONE"
            block = {"type": "text", "text": text}
        stop = "tool_use" if kind in ("agent", "read") else "end_turn"
        usage = {"input_tokens": SYNTHETIC_USAGE if kind == "read" else 1_000,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 20}
        message = {"id": f"fixture_{len(self.server.requests)}", "type": "message", "role": "assistant",
                   "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
        initial = {**block, "input": {}} if block["type"] == "tool_use" else {**block, "text": ""}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if block["type"] == "tool_use"
                 else {"type": "text_delta", "text": block["text"]})
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": initial},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 20}},
                  {"type": "message_stop"}]
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        try:
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def run_case(claude: str, parent_window: int, child_window: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="bridge-subagent-compact-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir(); config.mkdir(mode=0o700)
        (work / "fixture.txt").write_text("synthetic\n", encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [
            {"model": name, "label": "Fixture", "behavesAs": bridge_models.behaves_as(name)}
            for name in (PARENT, CHILD)], "replaceBuiltInOptions": True}, "modelSettings": {
                PARENT: {"autoCompactWindow": parent_window}, CHILD: {"autoCompactWindow": child_window}}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests, server.started, server.child_steps, server.read_calls = [], False, 0, 0
        server.work, server.lock, server.successful_read_results = work, threading.Lock(), set()
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=PARENT,
                   CLAUDE_CODE_GATEWAY_HINT_HEADERS="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1", ENABLE_TOOL_SEARCH="false")
        agents = {"fixture-worker": {"description": "Synthetic agent", "prompt": "Read the fixture.", "model": CHILD}}
        try:
            command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                       "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "json",
                       "--model", PARENT, "--agents", json.dumps(agents), "--tools", "Agent,Read", "--allowedTools", "Agent,Read",
                       "--max-turns", "8", "-p", "Run the synthetic agent."]
            completed = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=75)
            try:
                result = json.loads(completed.stdout)
                success = completed.returncode == 0 and result.get("is_error") is False
            except (ValueError, AttributeError, TypeError):
                success = False
            child = [item for item in server.requests if item["child"]]
            return {"completed": success, "child_requests": len(child),
                    "child_model_correct": bool(child) and all(row["model"] == CHILD for row in child),
                    "child_compactions": sum(row["class"] == "compaction" for row in child),
                    "all_compactions": sum(row["class"] == "compaction" for row in server.requests),
                    "read_calls": server.read_calls, "successful_read_results": len(server.successful_read_results)}
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


def main() -> None:
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude: raise RuntimeError("The locked Claude client is required")
    child_smaller = run_case(claude, 800_000, 100_000)
    parent_smaller = run_case(claude, 100_000, 800_000)
    both_smaller = run_case(claude, 100_000, 100_000)
    print(json.dumps({"synthetic_only": True, "reported_child_input_tokens": SYNTHETIC_USAGE,
                      "parent_large_child_small": child_smaller, "parent_small_child_large": parent_smaller,
                      "both_small_control": both_smaller}, sort_keys=True))
    cases = (child_smaller, parent_smaller, both_smaller)
    if not all(case["completed"] and case["child_model_correct"] and case["read_calls"] == 2
               and case["successful_read_results"] == 2 for case in cases):
        raise RuntimeError("Synthetic subagent tool controls failed")
    if not (child_smaller["child_compactions"] >= 1 and both_smaller["child_compactions"] >= 1
            and parent_smaller["child_compactions"] == 0):
        raise RuntimeError("Synthetic subagent compact-window controls failed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic subagent compaction observation failed; raw output withheld") from None
