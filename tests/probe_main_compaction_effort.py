"""Observe automatic compact effort using an isolated Claude and fake server.

No model inference, user config, account, prompt, or session is used. The
server advertises synthetic token usage to trigger the client's own compaction.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading

from claude_fixture import isolated_environment

MODEL = "gpt-6-astra"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if "count_tokens" in self.path:
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"input_tokens":1000}'); return
        request_class = self.headers.get("x-claude-code-request-class")
        effort = body.get("output_config", {}).get("effort")
        self.server.requests.append({"class": request_class, "model": body.get("model"), "effort": effort,
                                     "trigger": self.headers.get("x-claude-code-compaction")})
        for message in body.get("messages", []):
            parts = message.get("content") if isinstance(message, dict) else None
            for part in parts if isinstance(parts, list) else []:
                if (isinstance(part, dict) and part.get("type") == "tool_result" and
                        part.get("tool_use_id") == "fixture_read" and part.get("is_error") is not True):
                    self.server.read_result_seen = True
        if request_class == "compaction":
            kind = "summary"
        elif not self.server.read_requested:
            self.server.read_requested = True
            kind = "read"
        else:
            kind = "done"
        if kind == "read":
            block = {"type": "tool_use", "id": "fixture_read", "name": "Read",
                     "input": {"file_path": str(self.server.work / "fixture.txt")}}
        else:
            block = {"type": "text", "text": "<summary>Synthetic task remains active.</summary>" if kind == "summary" else "DONE"}
        stop = "tool_use" if kind == "read" else "end_turn"
        usage = {"input_tokens": 1000 if kind == "summary" else 780000,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 20}
        message = {"id": f"fixture_{len(self.server.requests)}", "type": "message", "role": "assistant",
                   "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}
        initial = {**block, "input": {}} if kind == "read" else {**block, "text": ""}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if kind == "read"
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default="xhigh")
    args = parser.parse_args()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude client is required")
    with tempfile.TemporaryDirectory(prefix="bridge-main-compact-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir(); config.mkdir(mode=0o700)
        (work / "fixture.txt").write_text("synthetic\n", encoding="utf-8")
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [
            {"model": MODEL, "label": "Fixture", "behavesAs": "claude-opus-5-5"}], "replaceBuiltInOptions": True},
            "modelSettings": {MODEL: {"autoCompactWindow": 784800}}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests, server.read_requested, server.read_result_seen, server.work = [], False, False, work
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_AUTH_TOKEN="fixture-only", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,
                   CLAUDE_CODE_GATEWAY_HINT_HEADERS="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1", ENABLE_TOOL_SEARCH="false")
        try:
            command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                       "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "stream-json",
                       "--input-format", "stream-json", "--verbose",
                       "--model", MODEL, "--effort", args.effort, "--tools", "Read", "--allowedTools", "Read",
                       "--max-turns", "8", "-p"]
            process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            events = queue.Queue()

            def read_events():
                for line in process.stdout:
                    try:
                        events.put(json.loads(line))
                    except ValueError:
                        pass
                events.put({"type": "fixture_eof"})

            reader = threading.Thread(target=read_events, daemon=True); reader.start()

            def turn(prompt):
                process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
                process.stdin.flush()
                while True:
                    event = events.get(timeout=75)
                    if event.get("type") == "fixture_eof":
                        return False
                    if event.get("type") == "result":
                        return event.get("is_error") is False

            try:
                success = turn("Read the synthetic fixture and finish.") and turn("Continue the synthetic task.")
            except (ValueError, AttributeError, TypeError, queue.Empty, OSError):
                success = False
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)
                reader.join(timeout=2)
                process.stdin.close(); process.stdout.close()
            compact = [row for row in server.requests if row["class"] == "compaction"]
            efforts = {row["effort"] for row in compact}
            safe_efforts = sorted(effort if effort in ("low", "medium", "high", "xhigh", "max") else "other" for effort in efforts)
            triggers = sorted(row["trigger"] if row["trigger"] in ("auto", "manual") else "other" for row in compact)
            print(json.dumps({"synthetic_only": True, "completed": success, "automatic_compaction_requests": len(compact),
                              "compact_model_correct": bool(compact) and all(row["model"] == MODEL for row in compact),
                              "compact_efforts": safe_efforts, "compact_triggers": triggers,
                              "read_result_seen": server.read_result_seen, "request_classes": [row["class"] if row["class"] in
                                  ("main", "compaction") else "other" for row in server.requests]}, sort_keys=True))
            if (not success or not server.read_result_seen or not compact or efforts != {args.effort} or
                    triggers != ["auto"] * len(compact) or not all(row["model"] == MODEL for row in compact)):
                raise RuntimeError("Synthetic main-session automatic compaction was not observed")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic automatic compaction observation failed; raw output withheld") from None
