"""Test EOF recovery with the real generated managed configuration and fake origin.

Two owned proxies and the reviewed local TLS ingress are used. The source
identity in the disposable bundle is explicitly synthetic; no user account,
existing listener, prompt, or model is accessed.
"""

from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import argparse
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

from codex_stream_fixture import temporary_codex_translator
from probe_claude_stream_safety import Handler
from smoke_managed_runtime import MODEL, SPEC
import api_profile
import launcher
import process_identity
import rolling_runtime
import runtime_bundle


def check(origin_binary: Path, backend_binary: Path, ingress_binary: Path):
    with tempfile.TemporaryDirectory(prefix="bridge-managed-stream-policy-") as temporary:
        root = Path(temporary)
        runtime_bundle.private_dir(root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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
                with patch.object(launcher, "ROOT", root):
                    value = rolling_runtime.ensure(root, original_runtime, state, {MODEL: SPEC})
                    try:
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
    args = parser.parse_args()
    rows = check(args.origin.resolve(), args.backend.resolve(), args.ingress.resolve())
    print(json.dumps({"mock_only": True, "actual_generated_managed_config": True,
                      "tls_ingress": True, "requests": rows}))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError, urllib.error.URLError):
        raise SystemExit("Synthetic managed-stream policy gate failed; raw output withheld") from None
