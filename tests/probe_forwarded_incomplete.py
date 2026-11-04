"""Compare two request attempts against an owned fake Codex stream and proxy.

No user home, account, existing service, real model, or user payload is used.
The fixture checks scope/cooldown only; it does not establish an outage's cause.
"""

import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import urllib.error
import urllib.request

from codex_stream_fixture import temporary_codex_translator
from probe_claude_stream_safety import Handler


SENTINEL = "stream error: stream disconnected before completion: stream closed before response.completed"


def send_twice(port, key, server):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    payload = json.dumps({
        "model": "gpt-6.1-sol", "max_tokens": 1024, "stream": True,
        "messages": [{"role": "user", "content": "Write a synthetic fixture."}],
        "tools": [{"name": "Write", "description": "Write a fixture", "input_schema": {
            "type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["file_path", "content"]}}],
    }).encode("utf-8")
    results = []
    for _ in range(2):
        request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/messages", data=payload,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with opener.open(request, timeout=10) as response:
                status, content = response.status, response.read(1_000_001)
        except urllib.error.HTTPError as exc:
            status, content = exc.code, exc.read(1_000_001)
        if len(content) > 1_000_000:
            raise AssertionError("Synthetic response exceeded its limit")
        results.append((status, content, len(server.requests)))
    return results


def check(binary, name, mode, error, expected_second, expected_upstream):
    with tempfile.TemporaryDirectory(prefix="bridge-forwarded-stream-") as root:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        server.mode = mode
        server.responses = True
        server.work = Path(root)
        server.target_name = "owned.txt"
        if error is not None:
            server.synthetic_error = error
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with temporary_codex_translator(binary, server.server_port) as (port, key, _log):
                results = send_twice(port, key, server)
                if results[0][0] != 200 or b"event: error" not in results[0][1] or results[0][2] != 1:
                    raise AssertionError("First synthetic stream did not fail once after output")
                if results[1][0] != expected_second or results[1][2] != expected_upstream:
                    raise AssertionError("Second synthetic request took an unexpected route")
                if expected_second == 200 and b"event: message_stop" not in results[1][1]:
                    raise AssertionError("Follow-up synthetic stream did not complete")
                return {"case": name, "first_status": results[0][0], "followup_status": results[1][0],
                        "upstream_attempts": results[1][2]}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def check_two_layers(inner, outer, expect_old):
    with tempfile.TemporaryDirectory(prefix="bridge-two-layer-stream-") as root:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests = []
        server.mode = "eof_partial_arguments"
        server.responses = True
        server.work = Path(root)
        server.target_name = "owned.txt"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with temporary_codex_translator(inner, server.server_port) as (inner_port, inner_key, _log):
                with temporary_codex_translator(outer, inner_port, upstream_prefix="/v1", upstream_key=inner_key) as (port, key, _outer_log):
                    first, second = send_twice(port, key, server)
            if first[0] != 200 or b"event: error" not in first[1] or SENTINEL.encode() not in first[1] or first[2] != 1:
                raise AssertionError("The two-layer fixture did not forward its incomplete stream")
            if expect_old:
                if second[0] != 503 or second[2] != 1:
                    raise AssertionError("The baseline two-layer cooldown did not reproduce")
            elif second[0] != 200 or b"event: message_stop" not in second[1] or second[2] != 2:
                raise AssertionError("The two-layer candidate did not admit the follow-up")
            return {"case": "two-layer-eof", "first_status": first[0], "followup_status": second[0],
                    "upstream_attempts": second[2]}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_binary", type=Path)
    parser.add_argument("--origin-binary", type=Path, help="Also test two separate local proxy layers")
    parser.add_argument("--expect-old", action="store_true", help="Expect the unfixed comparison behavior")
    args = parser.parse_args()
    exact = {"type": "server_error", "code": "request_timeout", "message": SENTINEL}
    cases = [
        ("forwarded-error", "partial_arguments", exact, 503 if args.expect_old else 200, 1 if args.expect_old else 2),
        ("forwarded-failed", "failed_partial_arguments", exact, 503 if args.expect_old else 200, 1 if args.expect_old else 2),
        ("forwarded-408", "partial_arguments", {**exact, "status_code": 408}, 503 if args.expect_old else 200, 1 if args.expect_old else 2),
        ("direct-eof", "eof_partial_arguments", None, 200, 2),
        ("other-timeout", "partial_arguments", {**exact, "message": "Synthetic unrelated timeout"}, 503, 1),
        ("genuine-quota", "partial_arguments", {"type": "rate_limit_error", "code": "usage_limit_reached",
                                               "message": "Synthetic quota limit reached", "status_code": 429}, 429, 1),
        ("genuine-auth", "partial_arguments", {"type": "authentication_error", "code": "invalid_api_key",
                                              "message": "Synthetic invalid credential", "status_code": 401}, 503, 1),
        ("sentinel-with-quota-status", "partial_arguments", {**exact, "status_code": 429}, 429, 1),
        ("sentinel-with-auth-status", "partial_arguments", {**exact, "status_code": 401}, 503, 1),
    ]
    rows = [check(args.proxy_binary.resolve(), *case) for case in cases]
    if args.origin_binary:
        rows.append(check_two_layers(args.origin_binary.resolve(), args.proxy_binary.resolve(), args.expect_old))
    print(json.dumps({"mock_only": True, "comparison": "baseline" if args.expect_old else "candidate", "cases": rows}))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, ValueError, RuntimeError):
        raise SystemExit("Synthetic forwarded-stream gate failed; raw output withheld") from None
