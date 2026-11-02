"""Test synthetic effort-rejection recovery through a temporary translator.

The real Claude client, upstream, model catalog, keys, and relay all use
disposable local fixtures. Only effort values and completion status are output.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import http.client
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


MODEL = "gpt-6.1-sol"


class ResponsesHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        effort = (body.get("reasoning") or {}).get("effort")
        self.server.efforts.append(effort)
        if effort == "max":
            payload = json.dumps({"error": {"type": "invalid_request_error",
                "message": "output_config.effort: Extra inputs are not permitted"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "DONE", "annotations": []}]}
        response = {"id": "resp_fixture", "model": MODEL, "status": "in_progress"}
        events = [{"type": "response.created", "response": response},
                  {"type": "response.output_item.added", "output_index": 0,
                   "item": {**item, "content": [], "status": "in_progress"}},
                  {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                   "part": {"type": "output_text", "text": ""}},
                  {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "DONE"},
                  {"type": "response.output_item.done", "output_index": 0, "item": item},
                  {"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
                   "usage": {"input_tokens": 100, "output_tokens": 5}}}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.split("?", 1)[0] == "/v1/messages":
            body = json.loads(raw)
            self.server.efforts.append((body.get("output_config") or {}).get("effort"))
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() not in ("host", "connection", "content-length", "transfer-encoding")}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.target_port, timeout=60)
        try:
            connection.request("POST", self.path, body=raw, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in ("connection", "content-length", "transfer-encoding"):
                    self.send_header(name, value)
            self.end_headers()
            while True:
                chunk = response.read1(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    expected = parser.add_mutually_exclusive_group()
    expected.add_argument("--expect-fallback", action="store_true")
    expected.add_argument("--expect-blocked", action="store_true")
    args = parser.parse_args()
    binary = args.binary.resolve()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude or not binary.is_file():
        raise RuntimeError("The locked Claude fixture and translator are required")
    with tempfile.TemporaryDirectory(prefix="bridge-effort-rejection-gateway-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": MODEL, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        origin = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
        origin.efforts = []
        thread = threading.Thread(target=origin.serve_forever, daemon=True)
        thread.start()
        spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
        upstream = {"state": {"port": origin.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1", ENABLE_TOOL_SEARCH="false", ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL)
        try:
            with temporary_translator(binary, upstream, MODEL) as (port, key):
                relay = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
                relay.target_port, relay.efforts = port, []
                relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
                relay_thread.start()
                try:
                    env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{relay.server_port}", ANTHROPIC_AUTH_TOKEN=key)
                    result = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config",
                        '{"mcpServers":{}}', "--settings", str(settings), "--permission-mode", "dontAsk",
                        "--model", MODEL, "--effort", "max", "--tools", "", "--output-format", "json",
                        "-p", "Return a synthetic response."], cwd=work, env=env,
                        capture_output=True, text=True, timeout=90)
                    message = json.loads(result.stdout)
                    completed = result.returncode == 0 and not message.get("is_error")
                    visible_reasoning_guidance = ("reasoning" in str(message.get("result", "")).lower()
                                                  and "/effort" in str(message.get("result", "")).lower())
                    if not relay.efforts or relay.efforts[0] != "max" or not origin.efforts:
                        raise RuntimeError("Synthetic effort origin did not receive its initial request")
                    if args.expect_fallback and (not completed or None not in relay.efforts or
                                                 not any(value != "max" for value in origin.efforts)):
                        raise RuntimeError("Expected unprotected effort fallback was not reproduced")
                    if args.expect_blocked and (completed or not message.get("is_error") or
                                                not visible_reasoning_guidance or
                                                any(value != "max" for value in relay.efforts) or
                                                any(value != "max" for value in origin.efforts)):
                        raise RuntimeError("Explicit synthetic effort was silently dropped")
                    print(json.dumps({"synthetic_only": True, "through_candidate_translator": True,
                        "client_efforts": relay.efforts, "fake_origin_efforts": origin.efforts,
                        "client_completed": completed, "client_reported_error": bool(message.get("is_error")),
                        "visible_reasoning_guidance": visible_reasoning_guidance}, sort_keys=True))
                finally:
                    relay.shutdown()
                    relay.server_close()
                    relay_thread.join(timeout=2)
        finally:
            origin.shutdown()
            origin.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic effort rejection gateway probe failed; raw output withheld") from None
