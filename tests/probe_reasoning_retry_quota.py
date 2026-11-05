"""Check a synthetic retry's 429 with and without pre-output keepalives."""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_stream_fixture as fixture  # noqa: E402
from probe_reasoning_retry_candidate import enabled_environment, fixture_write_text  # noqa: E402


def keepalive_write(path, data, *args, **kwargs):
    if path.name == "config.yaml" and path.parent.name.startswith(
        "bridge-codex-stream-fixture-"
    ):
        data += "\nrequests:\n  streaming:\n    keepalive-seconds: 1\n    send-keepalive-before-ft: true\n"
    return fixture_write_text(path, data, *args, **kwargs)


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.attempts += 1
        if self.server.attempts > 1:
            data = json.dumps(
                {
                    "error": {
                        "type": "rate_limit_error",
                        "code": "usage_limit_reached",
                        "message": "synthetic quota",
                    }
                }
            ).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        item = {"id": "fixture", "type": "reasoning", "summary": []}
        events = [
            {
                "type": "response.created",
                "response": {"id": "fixture", "model": "gpt-6.1-sol"},
            },
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {**item, "encrypted_content": "synthetic"},
            },
            {
                "type": "error",
                "error": {
                    "type": "server_error",
                    "code": "request_timeout",
                    "message": "stream error: stream disconnected before completion: stream closed before response.completed",
                },
            },
        ]
        try:
            for event in events:
                if self.server.keepalive and event["type"] == "error":
                    time.sleep(2.5)
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("--keepalive", action="store_true")
    parser.add_argument("--origin", action="store_true")
    args = parser.parse_args()
    outer = args.baseline if args.origin else args.candidate
    inner = args.candidate if args.origin else args.baseline
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    server.attempts, server.keepalive = 0, args.keepalive
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config_write = keepalive_write if args.keepalive else fixture_write_text
        with (
            patch.object(
                fixture, "isolated_environment", side_effect=enabled_environment
            ),
            patch.object(Path, "write_text", new=config_write),
            ExitStack() as stack,
        ):
            inner_port, inner_key, _ = stack.enter_context(
                fixture.temporary_codex_translator(
                    inner.resolve(), server.server_port
                )
            )
            port, key, _ = stack.enter_context(
                fixture.temporary_codex_translator(
                    outer.resolve(),
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
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    request, timeout=20
                ) as response:
                    status, data = response.status, response.read(100_001)
            except urllib.error.HTTPError as error:
                status, data = error.code, error.read(100_001)
            if len(data) > 100_000:
                raise AssertionError("Synthetic response exceeded its limit")
            comments = sum(line.startswith(b":") for line in data.splitlines())
            errors = []
            if status == 429:
                value = json.loads(data)
                error = value.get("error") if isinstance(value, dict) else None
                if isinstance(error, dict):
                    errors.append(error)
            else:
                for line in data.splitlines():
                    if not line.startswith(b"data:"):
                        continue
                    try:
                        event = json.loads(line[5:])
                    except ValueError:
                        continue
                    if (
                        isinstance(event, dict)
                        and event.get("type") == "error"
                        and isinstance(event.get("error"), dict)
                    ):
                        errors.append(event["error"])
            correctly_reported = any(
                error.get("type") == "rate_limit_error"
                and "quota" in str(error.get("message", "")).lower()
                for error in errors
            )
            if (
                server.attempts != 2
                or not correctly_reported
                or (args.keepalive and (status != 200 or comments < 1))
                or (not args.keepalive and status != 429)
            ):
                raise AssertionError(
                    "Synthetic quota status or keepalive behavior changed"
                )
            print(
                json.dumps(
                    {
                        "mock_only": True,
                        "origin_attempts": server.attempts,
                        "status": status,
                        "keepalive_comments": comments,
                        "quota_error_preserved": correctly_reported,
                    }
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError):
        raise SystemExit(
            "Synthetic reasoning retry quota gate failed; raw output withheld"
        ) from None
