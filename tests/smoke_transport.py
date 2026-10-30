"""Credential-free HTTP retry, incomplete-stream and cancellation gates."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from live_support import temporary_translator


MODEL = "gpt-6.1-sol"


class FixtureServer(ThreadingHTTPServer):
    mode = "ok"
    attempts = 0
    disconnected = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.attempts += 1
        mode = self.server.mode
        if mode == "quota" or (mode == "retry" and self.server.attempts < 3):
            status = 429 if mode == "quota" else 503
            payload = {"error": {"type": "insufficient_quota" if mode == "quota" else "server_error",
                                 "code": "insufficient_quota" if mode == "quota" else "server_error",
                                 "message": "Synthetic fixture failure"}}
            self.send_response(status); self.send_header("Content-Type", "application/json")
            if mode == "quota":
                self.send_header("Retry-After", "60")
            self.end_headers(); self.wfile.write(json.dumps(payload).encode()); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()

        def emit(event):
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode()); self.wfile.flush()

        message = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": "fixture", "annotations": []}]}
        try:
            emit({"type": "response.created", "response": {"id": "resp_fixture", "model": MODEL, "status": "in_progress"}})
            emit({"type": "response.output_item.added", "output_index": 0,
                  "item": {**message, "content": [], "status": "in_progress"}})
            emit({"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                  "part": {"type": "output_text", "text": ""}})
            emit({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "fixture"})
            if mode == "truncated":
                self.connection.shutdown(socket.SHUT_WR)
                return
            if mode == "cancel":
                for _ in range(60):
                    time.sleep(.05)
                    emit({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "."})
                return
            emit({"type": "response.output_item.done", "output_index": 0, "item": message})
            emit({"type": "response.completed", "response": {"id": "resp_fixture", "model": MODEL,
                 "status": "completed", "output": [message], "usage": {"input_tokens": 10, "output_tokens": 2}}})
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.server.disconnected.set()


def request(port, key):
    body = {"model": MODEL, "max_tokens": 40, "stream": True,
            "messages": [{"role": "user", "content": "Reply fixture."}]}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=12)


def events(response):
    return [json.loads(line[5:]) for line in response if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"]


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_transport.py /path/to/proxy")
    server = FixtureServer(("127.0.0.1", 0), Handler)
    server.disconnected = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
            "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]}
    upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
    checks = {}
    try:
        with temporary_translator(Path(sys.argv[1]).resolve(), upstream, MODEL) as (port, key):
            try:
                request(port, "incorrect-fixture-key")
                raise AssertionError("Unauthenticated client was accepted")
            except urllib.error.HTTPError as error:
                assert error.code == 401
            assert server.attempts == 0
            checks["inbound_auth_before_upstream"] = True
            server.mode = "retry"
            with request(port, key) as response:
                result = events(response)
            assert server.attempts == 3, server.attempts
            assert result[-1].get("type") == "message_stop"
            checks["retry_before_output"] = True
            server.mode = "truncated"; server.attempts = 0
            with request(port, key) as response:
                result = events(response)
            assert server.attempts == 1, "A partially emitted stream must not be replayed"
            assert result[-1].get("type") != "message_stop", "Truncation must not masquerade as success"
            checks["partial_stream_not_replayed"] = True
            checks["truncation_not_success"] = True
            server.mode = "cancel"; server.attempts = 0
            with request(port, key) as response:
                for line in response:
                    if line.startswith(b"data:") and json.loads(line[5:]).get("type") == "content_block_delta":
                        break
            assert server.disconnected.wait(5), "Upstream did not observe cancellation"
            assert server.attempts == 1
            checks["client_disconnect_releases_upstream"] = True
            server.mode = "ok"
            with request(port, key) as response:
                assert events(response)[-1].get("type") == "message_stop"
            checks["next_request_after_cancel"] = True
        # A separate fixture credential keeps a quota cooldown out of other cases.
        with temporary_translator(Path(sys.argv[1]).resolve(), upstream, MODEL) as (port, key):
            server.mode = "quota"; server.attempts = 0
            for _ in range(2):
                try:
                    with request(port, key) as response:
                        result = events(response)
                    assert any(e.get("type") == "error" for e in result)
                except urllib.error.HTTPError as error:
                    assert error.code in (429, 503)
            assert server.attempts == 1, "Quota cooldown must prevent repeated upstream calls"
            checks["quota_cooldown_retained"] = True
        print(json.dumps(checks))
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    main()
