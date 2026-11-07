"""Observe retry advice across owned Codex proxies without accounts or retries."""

import argparse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

from codex_stream_fixture import temporary_codex_translator


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests += 1
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "7")
        self.send_header("x-should-retry", "false")
        self.send_header("X-Fixture-Private", "fixture-private")
        self.send_header("Set-Cookie", "fixture-private=1")
        self.end_headers()
        self.wfile.write(json.dumps({"error": {"type": "server_error", "message": "Synthetic retry advice"}}).encode())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path)
    parser.add_argument("--origin-binary", type=Path)
    expectation = parser.add_mutually_exclusive_group()
    expectation.add_argument("--require-advice", action="store_true")
    expectation.add_argument("--expect-loss", action="store_true")
    parser.add_argument("--request-retries", type=int, choices=range(4), default=0)
    parser.add_argument("--second-credential", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    server.requests = 0
    server.status = 503
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    rows = []
    try:
        with ExitStack() as stack:
            port, key = server.server_port, "fixture-upstream"
            prefix = ""
            if args.origin_binary:
                port, key, _ = stack.enter_context(temporary_codex_translator(
                    args.origin_binary.resolve(), port, managed_policy=True,
                    request_retries=args.request_retries, second_credential=args.second_credential))
                prefix = "/v1"
            port, key, _ = stack.enter_context(temporary_codex_translator(
                args.proxy_binary.resolve(), port, upstream_key=key,
                upstream_prefix=prefix, managed_policy=True,
                request_retries=args.request_retries, second_credential=args.second_credential))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for status in (503, 429):
                for stream in (False, True):
                    server.status = status
                    before = server.requests
                    body = {"model": "gpt-6.1-sol", "max_tokens": 64, "stream": stream,
                            "messages": [{"role": "user", "content": "Synthetic fixture"}]}
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages",
                        data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + key,
                        "Content-Type": "application/json", "anthropic-version": "2023-06-01"})
                    try:
                        response = opener.open(request, timeout=15)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        response.read(65536)
                        row = {"upstream_status": status, "stream_requested": stream,
                               "status_preserved": response.status == status,
                               "retry_after_preserved": response.headers.get("Retry-After") == "7",
                               "retry_veto_preserved": response.headers.get("x-should-retry") == "false",
                               "private_headers_absent": all(response.headers.get(name) is None for name in
                                   ("X-Fixture-Private", "Set-Cookie")),
                               "one_upstream_attempt": server.requests - before == 1}
                    assert row["status_preserved"] and row["private_headers_absent"], "Synthetic error scope or privacy changed"
                    rows.append(row)
            if args.require_advice:
                assert all(row["retry_after_preserved"] and row["retry_veto_preserved"] and row["one_upstream_attempt"] for row in rows), "Retry advice was dropped or the vetoed request was repeated"
            if args.expect_loss:
                assert any(not row["retry_after_preserved"] or not row["retry_veto_preserved"] for row in rows), "The baseline unexpectedly preserved all retry advice"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    print(json.dumps({"synthetic_only": True, "two_layers": bool(args.origin_binary), "cases": rows}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, AssertionError):
        raise SystemExit("Synthetic retry-advice gate failed; raw details withheld") from None
