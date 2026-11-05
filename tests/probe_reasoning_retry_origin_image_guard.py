"""Verify a fake default origin's injected image tool disables replay."""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_stream_fixture as fixture  # noqa: E402
from probe_reasoning_retry_candidate import (  # noqa: E402
    enabled_environment,
    fixture_write_text,
    original_write_text,
)


def origin_default_write(path, data, *args, **kwargs):
    if path.name == "config.yaml" and path.parent.name.startswith(
        "bridge-codex-stream-fixture-"
    ):
        if "\nrouting:" not in data:
            # Keep the disposable origin's default image-tool injection while
            # matching the intended fake-origin retry/cooldown policy.
            data += "\nrouting:\n  retry:\n    request-retry: 2\n  cooldown:\n    transient-error-cooldown-seconds: -1\n"
            return original_write_text(path, data, *args, **kwargs)
    return fixture_write_text(path, data, *args, **kwargs)


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        tools = body.get("tools", [])
        self.server.attempts += 1
        self.server.saw_image |= isinstance(tools, list) and any(
            isinstance(tool, dict) and tool.get("type") == "image_generation"
            for tool in tools
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            {
                "type": "response.created",
                "response": {"id": "synthetic", "model": "gpt-6.1-sol"},
            },
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
                    "type": "server_error",
                    "code": "request_timeout",
                    "message": "stream error: stream disconnected before completion: stream closed before response.completed",
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
    server.attempts, server.saw_image = 0, False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (
            patch.object(
                fixture, "isolated_environment", side_effect=enabled_environment
            ),
            patch.object(Path, "write_text", new=origin_default_write),
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
                "tools": [
                    {
                        "name": "write_fixture",
                        "description": "Synthetic only",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
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
                response = urllib.request.build_opener(
                    urllib.request.ProxyHandler({})
                ).open(request, timeout=20)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                data = response.read(1_000_000).lower()
            checks = {
                "origin_injected_image_tool": server.saw_image,
                "no_automatic_retry": server.attempts == 1,
                "original_failure_visible": b"stream disconnected before completion"
                in data,
            }
            print(json.dumps({"mock_only": True, **checks}))
            if not all(checks.values()):
                raise AssertionError("Synthetic origin image-tool guard changed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError):
        raise SystemExit(
            "Synthetic origin image-tool gate failed; raw output withheld"
        ) from None
