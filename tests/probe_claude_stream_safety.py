"""Check real Claude tool execution at interrupted stream boundaries, without accounts.

Only synthetic Messages and an owned Write target are used. Tool hooks count
effects independently of request attempts. No model, user session or existing bridge
service is called. This is a safety gate, not a provider recovery benchmark.
"""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

from live_support import temporary_translator


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
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
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
                  "input": {"file_path": str(self.server.work / "owned.txt"), "content": "SYNTHETIC"}}
                 if tool else {"type": "text", "text": "DONE"})
        message = {"id": f"msg_fixture_{count}", "type": "message", "role": "assistant",
                   "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 1000, "output_tokens": 10}}
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if tool else
                 {"type": "text_delta", "text": block["text"]})
        first = count == 1
        if first and self.server.mode == "partial_arguments":
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
        cutoff = {"healthy": 6, "partial_arguments": 3, "tool_block": 4, "stop_reason": 5, "message_stop": 6}[self.server.mode] if first else len(events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events[:cutoff]:
                self.emit(event)
            if first and self.server.mode != "healthy":
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
        response = {"id": f"resp_fixture_{count}", "model": body.get("model"), "status": "in_progress"}
        if not results or not (self.server.work / "owned.txt").is_file():
            arguments = json.dumps({"file_path": str(self.server.work / "owned.txt"), "content": "SYNTHETIC"})
            item = {"id": f"fc_fixture_{count}", "type": "function_call", "call_id": f"call_fixture_{count}",
                    "name": "Write", "arguments": arguments, "status": "completed"}
            events = [
                {"type": "response.created", "response": response},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**item, "arguments": "", "status": "in_progress"}},
                {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": item["id"],
                 "delta": arguments[:10] if first and self.server.mode == "partial_arguments" else arguments},
                {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": item["id"], "arguments": arguments},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ]
            cutoff = {"healthy": 6, "partial_arguments": 3, "arguments_done": 4, "tool_item_done": 5, "response_completed": 6}[self.server.mode] if first else 6
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
            if first and self.server.mode != "healthy":
                self.wfile.write(b'data: {"type":"error","error":{"type":"server_error","code":"request_timeout","message":"Synthetic incomplete-stream fixture"}}\n\n')
                self.wfile.flush()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-binary", type=Path, help="Also exercise the actual Responses-to-Messages translator")
    args = parser.parse_args()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if claude is None:
        raise RuntimeError("Install the pinned Claude fixture before running the safety gate")
    metrics = []
    modes = (("healthy", "partial_arguments", "arguments_done", "tool_item_done", "response_completed") if args.proxy_binary else
             ("healthy", "partial_arguments", "tool_block", "stop_reason", "message_stop"))
    for mode in modes:
        with tempfile.TemporaryDirectory(prefix="bridge-stream-safety-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            work = root / "work"
            work.mkdir()
            config = root / "config"
            config.mkdir(mode=0o700)
            marker = root / "hooks"
            hook = root / "hook.py"
            hook.write_text('import json,pathlib,sys\njson.load(sys.stdin)\nwith pathlib.Path(sys.argv[1]).open("a") as f: f.write("write\\n")\n', encoding="utf-8")
            settings = root / "settings.json"
            settings.write_text(json.dumps({"modelPicker": {"options": [
                {"model": "gpt-6.1-sol", "label": "Fixture", "behavesAs": "claude-sonnet-5-5"}],
                "replaceBuiltInOptions": True}, "hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [
                    {"type": "command", "command": sys.executable, "args": [str(hook), str(marker)], "timeout": 5}]}]}}), encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.requests = []
            server.mode = mode
            server.responses = bool(args.proxy_binary)
            server.work = work
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            allowed = ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT",
                       "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP")
            env = {key: os.environ[key] for key in allowed if key in os.environ}
            env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_AUTH_TOKEN="fixture-only",
                ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                ANTHROPIC_DEFAULT_SONNET_MODEL="gpt-6.1-sol", ANTHROPIC_DEFAULT_HAIKU_MODEL="gpt-6.1-sol",
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
            command = [claude, "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", str(settings), "--permission-mode", "dontAsk", "--output-format", "json",
                "--model", "gpt-6.1-sol", "--tools", "Write", "--allowedTools", "Write", "--max-turns", "4",
                "-p", "Write SYNTHETIC to owned.txt exactly once."]
            stack = ExitStack()
            try:
                if args.proxy_binary:
                    spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                            "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
                    upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream",
                                "models": {"gpt-6.1-sol": spec}}
                    port, key = stack.enter_context(temporary_translator(args.proxy_binary.resolve(), upstream, "gpt-6.1-sol"))
                    env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key)
                result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=45)
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
                    assert len(hooks) <= 1, "A stream interruption executed the same logical write twice"
                    if succeeded:
                        assert len(hooks) == 1 and target.read_text(encoding="utf-8") == "SYNTHETIC", "Client reported success without completing the owned write"
                        assert any(request["has_tool_result"] for request in server.requests[1:]), "Continuation lost the committed tool result"
                    else:
                        assert final.get("is_error") is True or result.returncode != 0, "Client termination was ambiguous"
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
    print(json.dumps({"mock_only": True, "through_translator": bool(args.proxy_binary), "cases": metrics}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.TimeoutExpired):
        raise SystemExit("Synthetic stream-safety gate failed; raw output withheld") from None
