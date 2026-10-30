"""Check the Anthropic-to-OpenAI API-key route with a loopback fake upstream.

No real OpenAI key, OAuth credential, user prompt, or network service is used.
Run as: python tests/smoke_api_contract.py /path/to/cli-proxy-api
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


MODEL = "gpt-6.1-sol"


class Upstream(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict]] = []

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        self.requests.append((self.path, self.headers.get("Authorization", ""), body))
        if len(self.requests) == 1:
            output = [{
                "id": "fc_mock", "type": "function_call", "status": "completed",
                "call_id": "call_mock", "name": "Read",
                "arguments": '{"file_path":"/fixture"}',
            }]
        else:
            output = [{
                "id": "msg_mock", "type": "message", "status": "completed",
                "role": "assistant", "content": [{
                    "type": "output_text", "text": "ok", "annotations": [],
                }],
            }]
        response = {
            "id": "resp_mock", "object": "response", "created_at": 1,
            "status": "completed", "model": MODEL, "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }
        events = [
            {"type": "response.created", "response": {
                "id": response["id"], "model": MODEL, "status": "in_progress"}},
            {"type": "response.completed", "response": response},
        ]
        if len(self.requests) == 4:
            events = [
                events[0],
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {"id": "msg_stream", "type": "message", "role": "assistant", "content": []}},
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0,
                 "part": {"type": "output_text", "text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                 "delta": "live "},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                 "delta": "stream"},
                {"type": "response.output_item.done", "output_index": 0,
                 "item": {"id": "msg_stream", "type": "message", "status": "completed",
                          "role": "assistant", "content": [{"type": "output_text",
                                                       "text": "live stream", "annotations": []}]}},
                events[1],
            ]
        encoded = b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if len(self.requests) != 4:
            self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if len(self.requests) == 4:
            for index, event in enumerate(events):
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
                if index == 3:
                    time.sleep(.25)
        else:
            self.wfile.write(encoded)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def post(port: int, payload: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer mock-local",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Mock route returned HTTP {error.code}: {error.read().decode(errors='replace')}") from None


def rich_catalog(port: int) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/models?client_version=cpa",
        headers={"Authorization": "Bearer mock-local"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        return json.load(response)


def post_stream(port: int, payload: dict) -> list[tuple[float, dict]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer mock-local",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        assert response.headers.get_content_type() == "text/event-stream"
        return [(time.monotonic(), json.loads(line[6:])) for line in response
                if line.startswith(b"data: ") and line[6:].strip() != b"[DONE]"]


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: smoke_api_contract.py /path/to/cli-proxy-api")
    binary = Path(sys.argv[1]).resolve()
    if not binary.is_file():
        raise RuntimeError("Proxy binary is missing")
    Upstream.requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    with tempfile.TemporaryDirectory(prefix="bridge-api-contract-") as temporary:
        root = Path(temporary)
        auth = root / "auth"
        auth.mkdir()
        port = free_port()
        config = f'''config-version: 8
server:
  host: "127.0.0.1"
  port: {port}
management:
  allow-remote: false
  secret-key: "mock-management"
  disable-control-panel: true
  disable-auto-update-panel: true
access:
  api-keys:
    - "mock-local"
oauth:
  auth-dir: "{auth.as_posix()}"
api-keys:
  codex:
    - name: mock-openai-api
      base-url: "http://127.0.0.1:{upstream.server_port}/v1"
      models:
        - name: "{MODEL}"
          max-context-length: 1050000
      keys:
        - api-key: "mock-upstream"
observability:
  logs:
    debug: false
    logging-to-file: false
    request-log: false
  usage:
    usage-statistics-enabled: false
multimedia:
  disable-image-generation: "passthrough"
'''
        config_path = root / "config.yaml"
        config_path.write_text(config)
        process = subprocess.Popen(
            [str(binary), "-config", str(config_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(50):
                if process.poll() is not None:
                    raise RuntimeError("Proxy exited before becoming ready")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.2)
            else:
                raise RuntimeError("Proxy startup timed out")

            for _ in range(50):
                model_rows = [row for row in rich_catalog(port).get("models", [])
                              if row.get("slug") == MODEL]
                if model_rows:
                    break
                time.sleep(.2)
            assert len(model_rows) == 1 and model_rows[0].get("context_window") == 1050000, model_rows

            tools = [{
                "name": "Read", "description": "Read a fixture file",
                "input_schema": {"type": "object", "properties": {
                    "file_path": {"type": "string"}}, "required": ["file_path"]},
            }]
            first = post(port, {
                "model": MODEL, "max_tokens": 1024, "stream": False,
                "messages": [{"role": "user", "content": "Read /fixture"}],
                "tools": tools, "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
            })
            calls = [part for part in first.get("content", []) if part.get("type") == "tool_use"]
            assert len(calls) == 1 and calls[0]["name"] == "Read", first
            assert calls[0]["input"] == {"file_path": "/fixture"}, first

            second = post(port, {
                "model": MODEL, "max_tokens": 1024, "stream": False,
                "messages": [
                    {"role": "user", "content": "Read /fixture"},
                    {"role": "assistant", "content": [calls[0]]},
                    {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": calls[0]["id"],
                        "content": "fixture result",
                    }]},
                ],
                "tools": tools,
            })
            assert any(part.get("type") == "text" and part.get("text") == "ok"
                       for part in second.get("content", [])), second

            third = post(port, {
                "model": MODEL, "max_tokens": 1024, "stream": False,
                "messages": [{"role": "user", "content": "Find a source"}],
                "tools": [{"type": "web_search_20260209", "name": "web_search"}],
            })
            assert third.get("type") == "message", third
            stream_events = post_stream(port, {
                "model": MODEL, "max_tokens": 1024, "stream": True,
                "messages": [{"role": "user", "content": "Stream a reply"}],
            })
            event_types = [event.get("type") for _, event in stream_events]
            assert event_types[0] == "message_start" and event_types[-1] == "message_stop", event_types
            deltas = [event.get("delta", {}).get("text", "") for _, event in stream_events
                      if event.get("type") == "content_block_delta"]
            assert "live " in deltas and "stream" in deltas, stream_events
            first_delta_at = next(when for when, event in stream_events
                                  if event.get("type") == "content_block_delta" and
                                  event.get("delta", {}).get("text") == "live ")
            stop_at = next(when for when, event in stream_events if event.get("type") == "message_stop")
            assert stop_at - first_delta_at >= .10, stream_events
            assert len(Upstream.requests) == 4, Upstream.requests
            for path, authorization, body in Upstream.requests:
                assert path.endswith("/v1/responses"), path
                assert authorization == "Bearer mock-upstream", authorization
                assert body.get("model") == MODEL, body
                assert body.get("store") is False, body
                assert all(tool.get("type") != "image_generation" for tool in body.get("tools", [])), body
            first_body = Upstream.requests[0][2]
            assert first_body.get("reasoning", {}).get("effort") == "high", first_body
            assert "reasoning.encrypted_content" in first_body.get("include", []), first_body
            assert any(tool.get("name") == "Read" for tool in first_body.get("tools", [])), first_body
            second_items = Upstream.requests[1][2].get("input", [])
            assert any(item.get("type") == "function_call" and item.get("call_id") == "call_mock"
                       for item in second_items), second_items
            assert any(item.get("type") == "function_call_output" and
                       item.get("call_id") == "call_mock" and item.get("output") == "fixture result"
                       for item in second_items), second_items
            assert any(tool.get("type") == "web_search" for tool in Upstream.requests[2][2].get("tools", [])), Upstream.requests[2]
            print("Mock OpenAI API Responses contract: PASS")
        finally:
            process.terminate()
            process.wait(timeout=5)
    upstream.shutdown()
    upstream.server_close()


if __name__ == "__main__":
    main()
