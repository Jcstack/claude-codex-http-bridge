"""Exercise a real bounded backoff through two disposable fake proxies only."""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_stream_fixture as fixture  # noqa: E402
from probe_reasoning_retry_candidate import enabled_environment  # noqa: E402
from probe_reasoning_retry_quota import keepalive_write  # noqa: E402


def real_wait_environment(root):
    env = enabled_environment(root)
    env["BRIDGE_SYNTHETIC_REAL_BACKOFF"] = "1"
    return env


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.server.bodies.append(
            json.dumps(body, sort_keys=True, separators=(",", ":"))
        )
        first = len(self.server.bodies) == 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        response = {"id": "synthetic", "model": "gpt-6.1-sol", "status": "in_progress"}
        if first:
            events = [
                {"type": "response.created", "response": response},
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "reasoning",
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": "synthetic",
                    },
                },
                {
                    "type": "error",
                    "error": {
                        "type": "service_unavailable_error",
                        "code": "server_is_overloaded",
                        "message": "synthetic overload",
                    },
                },
            ]
        else:
            item = {
                "id": "synthetic-message",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "DONE", "annotations": []}],
            }
            events = [
                {"type": "response.created", "response": response},
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "content": [], "status": "in_progress"},
                },
                {
                    "type": "response.content_part.added",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "DONE",
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {
                    "type": "response.completed",
                    "response": {
                        **response,
                        "status": "completed",
                        "output": [item],
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                },
            ]
        try:
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    server.bodies = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (
            patch.object(
                fixture, "isolated_environment", side_effect=real_wait_environment
            ),
            patch.object(Path, "write_text", new=keepalive_write),
            ExitStack() as stack,
        ):
            inner_port, inner_key, _ = stack.enter_context(
                fixture.temporary_codex_translator(
                    args.candidate.resolve(), server.server_port
                )
            )
            port, key, _ = stack.enter_context(
                fixture.temporary_codex_translator(
                    args.baseline.resolve(),
                    inner_port,
                    upstream_prefix="/v1",
                    upstream_key=inner_key,
                    managed_policy=True,
                )
            )
            payload = {
                "model": "gpt-6.1-sol",
                "stream": True,
                "max_tokens": 128,
                "output_config": {"effort": "xhigh"},
                "messages": [{"role": "user", "content": "synthetic"}],
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/messages",
                data=json.dumps(payload).encode(),
                headers={
                    "x-api-key": key,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
            )
            start = time.monotonic()
            comments, done = 0, False
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=95
            ) as response:
                if response.status != 200:
                    raise AssertionError("Synthetic stream did not start")
                for line in response:
                    if line.startswith(b":"):
                        comments += 1
                    if line.startswith(b"data:"):
                        try:
                            event = json.loads(line[5:])
                        except ValueError:
                            continue
                        done |= (
                            isinstance(event, dict)
                            and event.get("type") == "message_stop"
                        )
            elapsed = time.monotonic() - start
            checks = {
                "waited_at_least_30_seconds": elapsed >= 29.5,
                "completed_within_90_seconds": elapsed < 90,
                "received_multiple_keepalives": comments >= 2,
                "completed_message": done,
                "same_request_retried_once": len(server.bodies) == 2
                and server.bodies[0] == server.bodies[1],
            }
            print(
                json.dumps(
                    {
                        "mock_only": True,
                        "elapsed_seconds": round(elapsed, 1),
                        "keepalive_comments": comments,
                        **checks,
                    }
                )
            )
            if not all(checks.values()):
                raise AssertionError("Synthetic long-wait invariant failed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError):
        raise SystemExit(
            "Synthetic long-wait gate failed; raw output withheld"
        ) from None
