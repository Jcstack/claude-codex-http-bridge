"""Test EOF recovery with the real generated managed configuration and fake origin.

Two owned proxies and the reviewed local TLS ingress are used. The source
identity in the disposable bundle is explicitly synthetic; no user account,
existing listener, prompt, or model is accessed.
"""

from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from codex_stream_fixture import temporary_codex_translator
from probe_claude_stream_safety import Handler
from smoke_managed_runtime import MODEL, SPEC
import api_profile
import diagnostics
import launcher
import process_identity
import rolling_runtime
import runtime_bundle


class GapHandler(Handler):
    """Hold the first fake upstream stream open briefly before its EOF."""

    def emit_responses(self, body):
        if self.server.requests:
            return super().emit_responses(body)
        original = self.wfile

        class DelayedWriter:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.flushes = 0

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

            def flush(self):
                self.wrapped.flush()
                self.flushes += 1
                if self.flushes == 3:
                    time.sleep(0.3)

        self.wfile = DelayedWriter(original)
        try:
            return super().emit_responses(body)
        finally:
            self.wfile = original


def check(origin_binary: Path, backend_binary: Path, ingress_binary: Path, *, observe: bool,
          gap_check: bool = False, window_seconds: int = 3600, expired_window: bool = False):
    with tempfile.TemporaryDirectory(prefix="bridge-managed-stream-policy-") as temporary:
        root = Path(temporary)
        runtime_bundle.private_dir(root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), GapHandler if gap_check else Handler)
        server.requests = []
        server.mode = "eof_partial_arguments"
        server.responses = True
        server.work = root
        server.target_name = "owned.txt"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with ExitStack() as stack:
                origin_port, origin_key, _log = stack.enter_context(
                    temporary_codex_translator(origin_binary, server.server_port, local_key="a" * 64))
                owners = launcher.listener_pids(origin_port)
                if len(owners) != 1:
                    raise RuntimeError("Synthetic origin ownership is ambiguous")
                pid = next(iter(owners))
                original_runtime = root / "runtime"
                runtime_bundle.private_dir(original_runtime)
                session = original_runtime / "session-fixture"
                runtime_bundle.private_dir(session)
                for name, value in (("config.yaml", "synthetic"), ("key", origin_key), ("proxy.log", "")):
                    api_profile.write_private(session / name, value)
                state = {"pid": pid, "port": origin_port, "session": session.name,
                         "process_identity": process_identity.stamp(pid)}
                rolling_runtime.write(original_runtime / "service.json", state)
                bundle = root / "synthetic-bundle"
                source = {"synthetic_only": True, "files": {"gateway/process_identity.go": "synthetic"}}
                with patch.object(runtime_bundle, "source_lock", return_value=source):
                    runtime_bundle.pack(backend_binary, ingress_binary, bundle)
                    runtime_bundle.activate(root, bundle)
                with patch.object(launcher, "ROOT", root), patch.dict(os.environ,
                        {"CLAUDE_CODEX_STREAM_DIAGNOSTICS": "1" if observe else "0",
                         "CLAUDE_CODEX_STREAM_DIAGNOSTICS_SECONDS": "1" if expired_window else str(window_seconds)}):
                    value = rolling_runtime.ensure(root, original_runtime, state, {MODEL: SPEC})
                    try:
                        if expired_window:
                            time.sleep(1.2)
                        runtime = root / "runtime-managed"
                        key = rolling_runtime.private_key(runtime / "front-key")
                        context = ssl.create_default_context(cafile=str(runtime / "ca.pem"))
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                             urllib.request.HTTPSHandler(context=context))
                        payload = json.dumps({"model": MODEL, "max_tokens": 1024, "stream": True,
                            "messages": [{"role": "user", "content": "Synthetic fixture"}],
                            "tools": [{"name": "Write", "description": "Synthetic fixture", "input_schema": {
                                "type": "object", "properties": {"file_path": {"type": "string"},
                                                                     "content": {"type": "string"}}}}]}).encode()
                        rows = []
                        for _ in range(2):
                            request = urllib.request.Request(f'https://127.0.0.1:{value["port"]}/v1/messages', data=payload,
                                headers={"Authorization": "Bearer " + key, "anthropic-version": "2023-06-01",
                                         "content-type": "application/json"})
                            try:
                                with opener.open(request, timeout=15) as response:
                                    status, content = response.status, response.read(1_000_001)
                            except urllib.error.HTTPError as exc:
                                status, content = exc.code, exc.read(1_000_001)
                            if len(content) > 1_000_000:
                                raise RuntimeError("Synthetic response exceeded its limit")
                            rows.append({"status": status, "sse_error": b"event: error" in content,
                                "sse_stop": b"event: message_stop" in content,
                                "incomplete_sentinel": b"stream closed before response.completed" in content,
                                "fake_upstream_requests": len(server.requests)})
                        if not (rows[0]["status"] == 200 and rows[0]["sse_error"] and
                                rows[0]["incomplete_sentinel"] and rows[0]["fake_upstream_requests"] == 1):
                            raise AssertionError("Managed fixture did not forward the interruption")
                        if not (rows[1]["status"] == 200 and rows[1]["sse_stop"] and rows[1]["fake_upstream_requests"] == 2):
                            raise AssertionError("Managed policy did not admit the follow-up")
                        log = runtime / value["current"] / "proxy.log"
                        observation_lines = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
                                             if "codex stream observation:" in line]
                        observations = [diagnostics.stream_observation(line) for line in observation_lines]
                        if any(item is None for item in observations):
                            raise AssertionError("Managed observer emitted an unrecognized shape")
                        if any(str(root) in line or key in line or "SYNTHETIC" in line or "fixture-upstream" in line
                               for line in observation_lines):
                            raise AssertionError("Managed observation exposed synthetic private input")
                        if observe and not expired_window and not any(item["outcome"] == "terminal_event" for item in observations):
                            raise AssertionError("The opted-in managed observer did not record the synthetic failure")
                        if observe and not expired_window and not any(item["outcome"] == "completion_event" for item in observations):
                            raise AssertionError("The opted-in managed observer did not record the healthy control")
                        if gap_check and not any(item["outcome"] == "terminal_event" and
                                                 (item.get("max_read_gap_ms") or 0) >= 150 for item in observations):
                            raise AssertionError("The observer did not retain the synthetic read gap")
                        if (not observe or expired_window) and observations:
                            raise AssertionError("The disabled managed observer recorded a failure")
                        return rows
                    finally:
                        rolling_runtime.stop(root)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin", type=Path)
    parser.add_argument("backend", type=Path)
    parser.add_argument("ingress", type=Path)
    parser.add_argument("--observe", action="store_true", help="Enable bounded metadata in the disposable backend")
    parser.add_argument("--gap-check", action="store_true", help="Check a synthetic delay before EOF")
    parser.add_argument("--window-seconds", type=int, default=3600)
    parser.add_argument("--expired-window", action="store_true", help="Verify a one-second observer stops before requests")
    args = parser.parse_args()
    if args.gap_check and not args.observe:
        parser.error("--gap-check requires --observe")
    if not 1 <= args.window_seconds <= 86400 or (args.expired_window and (not args.observe or args.gap_check)):
        parser.error("Use 1–86400 seconds; --expired-window requires --observe without --gap-check")
    rows = check(args.origin.resolve(), args.backend.resolve(), args.ingress.resolve(),
                 observe=args.observe, gap_check=args.gap_check, window_seconds=args.window_seconds,
                 expired_window=args.expired_window)
    print(json.dumps({"mock_only": True, "actual_generated_managed_config": True,
                      "tls_ingress": True, "observer_enabled": args.observe,
                      "expired_window_checked": args.expired_window,
                      "synthetic_gap_checked": args.gap_check, "requests": rows}))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError, urllib.error.URLError):
        raise SystemExit("Synthetic managed-stream policy gate failed; raw output withheld") from None
