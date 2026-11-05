"""Exercise synthetic opaque reasoning items through two local Codex translators.

The fixture uses no user credentials, prompts, conversation files, or running
services. It does not establish which upstream events occurred in a real run.
"""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.request

from codex_stream_fixture import temporary_codex_translator


MODEL = "gpt-6.1-sol"


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

        def event(item):
            self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
            self.wfile.flush()

        event({"type": "response.created", "response": {"id": "resp_fixture", "model": MODEL}})
        if self.server.mode == "opaque":
            for index in range(3):
                item = {"id": f"reasoning_fixture_{index}", "type": "reasoning", "summary": []}
                event({"type": "response.output_item.added", "output_index": index, "item": item})
                event({"type": "response.output_item.done", "output_index": index,
                       "item": {**item, "encrypted_content": "synthetic-opaque-" + str(index)}})
        elif self.server.mode == "comments":
            for _ in range(3):
                self.wfile.write(b": synthetic ping\n\n")
                self.wfile.flush()
        else:
            raise AssertionError("Unexpected synthetic origin mode")
        # Deliberately finish without response.completed.
        self.close_connection = True


def inspect_response(port, key):
    payload = {"model": MODEL, "max_tokens": 256, "stream": True,
               "output_config": {"effort": "xhigh"},
               "messages": [{"role": "user", "content": "Synthetic request."}]}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps(payload).encode(), headers={"x-api-key": key, "content-type": "application/json",
        "anthropic-version": "2023-06-01"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        data = response.read(200_001)
    if len(data) > 200_000:
        raise AssertionError("Synthetic stream exceeded its bound")
    counts = {"thinking_starts": 0, "signatures": 0, "visible_thinking_chars": 0,
              "errors": 0, "completed": 0}
    for line in data.splitlines():
        if not line.startswith(b"data:"):
            continue
        try:
            item = json.loads(line[5:])
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        block = item.get("content_block") or {}
        delta = item.get("delta") or {}
        if kind == "content_block_start" and block.get("type") == "thinking":
            counts["thinking_starts"] += 1
            counts["visible_thinking_chars"] += len(block.get("thinking", ""))
        elif kind == "content_block_delta" and delta.get("type") == "signature_delta":
            counts["signatures"] += 1
        elif kind == "content_block_delta" and delta.get("type") == "thinking_delta":
            counts["visible_thinking_chars"] += len(delta.get("thinking", ""))
        elif kind == "error":
            counts["errors"] += 1
        elif kind == "message_stop":
            counts["completed"] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin_binary", type=Path)
    parser.add_argument("outer_binary", type=Path)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ExitStack() as stack:
            inner_port, inner_key, _ = stack.enter_context(temporary_codex_translator(
                args.origin_binary.resolve(), server.server_port))
            port, key, _ = stack.enter_context(temporary_codex_translator(
                args.outer_binary.resolve(), inner_port, upstream_prefix="/v1",
                upstream_key=inner_key, managed_policy=True))
            rows = {}
            for mode in ("opaque", "comments"):
                server.mode = mode
                rows[mode] = inspect_response(port, key)
        if rows["opaque"] != {"thinking_starts": 3, "signatures": 3, "visible_thinking_chars": 0,
                              "errors": 1, "completed": 0}:
            raise AssertionError("Unexpected opaque reasoning translation")
        if rows["comments"] != {"thinking_starts": 0, "signatures": 0, "visible_thinking_chars": 0,
                                "errors": 1, "completed": 0}:
            raise AssertionError("Unexpected comment-only translation")
        print(json.dumps({"mock_only": True, "layers": 2, "cases": rows}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, ValueError, RuntimeError):
        raise SystemExit("Synthetic opaque reasoning gate failed; raw output withheld") from None
