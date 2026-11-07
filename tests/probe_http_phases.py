"""Verify HTTP phase observations with owned proxies and gated fake responses.

No account, real service, user request, or upstream model is used. The output
contains only fixed synthetic outcomes, never raw logs or local identities.
"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics
from codex_stream_fixture import temporary_codex_translator


SECRET = "SYNTHETIC_PRIVATE_HTTP_PHASE"
MARKER = "HTTP_PHASE_COMPLETE"


def response_bytes(model):
    item = {"type": "message", "id": "fixture-message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": MARKER, "annotations": []}]}
    values = [
        {"type": "response.created", "response": {"id": "fixture", "model": model}},
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}},
        {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
         "part": {"type": "output_text", "text": ""}},
        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": MARKER},
        {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "part": item["content"][0]},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"id": "fixture", "model": model, "status": "completed",
         "output": [item], "usage": {"input_tokens": 1000, "output_tokens": 4}}},
    ]
    return b"".join(("data: " + json.dumps(value) + "\n\n").encode() for value in values)


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        if not 0 <= size <= 1024 * 1024:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(size))
        main = SECRET in json.dumps(body)
        mode = self.server.mode if main else "healthy"
        if mode == "decode_inner":
            mode = "decode"
        if main:
            self.server.main_calls += 1
            self.server.entered.set()
        try:
            if mode in ("headers", "cancel_headers"):
                self.server.release.wait(30)
            payload = response_bytes(body.get("model"))
            if mode == "refusal":
                payload = json.dumps({"error": {"type": "server_error", "message": "Owned refusal"}}).encode()
            if mode in ("decode", "cancel_decode"):
                payload = gzip.compress(payload)
            self.send_response(503 if mode == "refusal" else 200)
            self.send_header("Content-Type", "application/json" if mode == "refusal" else "text/event-stream")
            if mode in ("decode", "cancel_decode"):
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Set-Cookie", "fixture-private=1")
            self.end_headers()
            self.wfile.flush()
            if main:
                self.server.headers_sent.set()
            if mode in ("decode", "cancel_decode"):
                self.server.release.wait(30)
            self.wfile.write(payload)
            self.wfile.flush()
        except OSError:
            pass
        finally:
            self.close_connection = True


def request(port, key, content=SECRET):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    payload = {"model": "gpt-6.1-sol", "max_tokens": 64, "stream": False,
               "messages": [{"role": "user", "content": content}]}
    connection.request("POST", "/v1/messages", json.dumps(payload),
        {"Authorization": "Bearer " + key, "Content-Type": "application/json",
         "X-Claude-Code-Request-Class": "compaction"})
    return connection


def wait_for(log, predicate):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        report = diagnostics.proxy_summary(log, datetime(2020, 1, 1, tzinfo=timezone.utc))["http_request_phases"]
        if SECRET in json.dumps(report):
            raise RuntimeError("Synthetic request content escaped HTTP phase metadata")
        if predicate(report):
            return report
        threading.Event().wait(.05)
    raise RuntimeError("Owned proxy did not expose the expected HTTP phase")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path)
    parser.add_argument("--origin-binary", type=Path)
    args = parser.parse_args()
    rows = []
    # A legacy inner proxy absorbs its own decoder wait. The upgraded outer
    # can observe only its call to that proxy, not provider-side header timing.
    modes = ("headers", "decode_inner", "cancel_headers", "refusal") if args.origin_binary else (
        "headers", "decode", "cancel_headers", "cancel_decode", "refusal")
    for mode in modes:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        server.mode, server.main_calls = mode, 0
        server.entered, server.release = threading.Event(), threading.Event()
        server.headers_sent = threading.Event()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = reader = None
        try:
            with ExitStack() as stack:
                upstream, upstream_key, prefix = server.server_port, "fixture-upstream", ""
                if args.origin_binary:
                    upstream, upstream_key, _ = stack.enter_context(temporary_codex_translator(
                        args.origin_binary.resolve(), upstream, managed_policy=True))
                    prefix = "/v1"
                port, key, log = stack.enter_context(temporary_codex_translator(
                    args.proxy_binary.resolve(), upstream, upstream_key=upstream_key,
                    upstream_prefix=prefix, managed_policy=True))
                # Release the fake before the gateways or their temporary files disappear.
                stack.callback(server.release.set)
                result = {}
                connection = request(port, key)

                def read_response():
                    try:
                        response = connection.getresponse()
                        result.update(status=response.status, body=response.read(1024 * 1024))
                    except (OSError, http.client.HTTPException):
                        result["connection_closed"] = True

                reader = threading.Thread(target=read_response, daemon=True)
                reader.start()
                assert server.entered.wait(12), "The owned origin was not reached"
                if mode == "decode_inner":
                    assert server.headers_sent.wait(12), "The owned inner response headers were not sent"
                if mode in ("headers", "cancel_headers", "decode_inner"):
                    observed = wait_for(log, lambda value: value["phase_outcome_mentions"].get("dispatch.started") == 1)
                    assert not observed["phase_outcome_mentions"].get("headers.returned"), "A header return was invented before the gate opened"
                elif mode in ("decode", "cancel_decode"):
                    observed = wait_for(log, lambda value: value["phase_outcome_mentions"].get("headers.returned") == 1)
                    assert not observed["phase_outcome_mentions"].get("decode.ready"), "Decoder readiness was invented while gzip setup was blocked"
                if mode != "refusal":
                    control = request(port, key, "OWNED_INDEPENDENT_CONTROL")
                    try:
                        response = control.getresponse()
                        assert response.status == 200 and MARKER.encode() in response.read(65536), "Independent work failed while another call waited"
                    finally:
                        control.close()
                cancelled = mode.startswith("cancel_")
                if cancelled:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                    connection.close()
                    observed = wait_for(log, lambda value: any(
                        row["context_state"] == "cancelled" for row in value["recent_errors"]))
                else:
                    server.release.set()
                reader.join(timeout=15)
                assert not reader.is_alive(), "The owned client did not finish"
                if not cancelled:
                    assert result.get("status") == (503 if mode == "refusal" else 200), "Observed HTTP status changed"
                    if mode != "refusal":
                        body = json.loads(result["body"])
                        assert body.get("stop_reason") == "end_turn" and any(
                            block.get("text") == MARKER for block in body.get("content", []) if isinstance(block, dict)), "Observed response content changed"
                    observed = wait_for(log, lambda value: bool(value["phase_outcome_mentions"].get("decode.ready")))
                assert server.main_calls == 1, "Observation added an upstream attempt"
                assert any(row["request_class"] == "compaction" for row in observed["recent_attempts"]), "The fixed client request class was lost"
                rows.append({"mode": mode, "phase_boundary_observed": True, "cancelled": cancelled,
                    "one_main_upstream_attempt": True, "private_content_absent": True,
                    "independent_work_completed": mode != "refusal"})
                connection.close()
                connection = None
        finally:
            server.release.set()
            if connection is not None:
                connection.close()
            if reader is not None:
                reader.join(timeout=2)
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
    print(json.dumps({"synthetic_only": True, "two_layers": bool(args.origin_binary), "cases": rows}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, AssertionError, http.client.HTTPException):
        raise SystemExit("Owned HTTP phase observation failed; raw details withheld") from None
