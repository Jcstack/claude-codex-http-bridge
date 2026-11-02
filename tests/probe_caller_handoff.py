"""Compare a caller-policy candidate across a disposable managed handoff.

Uses two supplied synthetic-test binaries, a temporary TLS ingress, an owned
fake origin and optionally the locked Claude client. No real account, running
service, user session or model inference is contacted. Mocked source locks
make this a behavior test, not binary provenance or production-rollout proof.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile
import launcher
import process_identity
import quota_recovery
import rolling_runtime
import runtime_bundle
from live_compare_translators import stream
from smoke_managed_runtime import MODEL, SPEC, Upstream, persistent_claude


def post(port, key, ca, payload):
    request = urllib.request.Request(f"https://127.0.0.1:{port}/v1/messages", data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "anthropic-version": "2023-06-01"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))))
    try:
        with opener.open(request, timeout=30) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        try:
            error.read()
            return error.code
        finally:
            error.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("ingress", type=Path)
    parser.add_argument("--claude-client", action="store_true")
    args = parser.parse_args()
    for binary in (args.baseline, args.candidate, args.ingress):
        if not binary.is_file():
            raise RuntimeError("Synthetic handoff binary missing")
    Upstream.requests = []
    Upstream.quota_mode = False
    Upstream.admitted.clear()
    Upstream.release.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-caller-handoff-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
            root = Path(temporary).resolve()
            runtime_bundle.private_dir(root)
            base_runtime = root / "runtime"
            runtime_bundle.private_dir(base_runtime)
            source = base_runtime / "session-fixture"
            runtime_bundle.private_dir(source)
            for name, value in (("config.yaml", "synthetic"), ("key", "a" * 64), ("proxy.log", "")):
                api_profile.write_private(source / name, value)
            base = {"process_identity": process_identity.stamp(os.getpid()), "pid": os.getpid(),
                    "port": server.server_port, "session": source.name}
            old_bundle, new_bundle = root / "baseline-bundle", root / "candidate-bundle"
            fixture_lock = {**runtime_bundle.source_lock(), "synthetic_fixture_only": True}
            with patch.object(runtime_bundle, "source_lock", return_value=fixture_lock):
                runtime_bundle.pack(args.baseline.resolve(), args.ingress.resolve(), old_bundle)
                runtime_bundle.pack(args.candidate.resolve(), args.ingress.resolve(), new_bundle)
                old_identity = runtime_bundle.activate(root, old_bundle)
                catalog = {MODEL: SPEC}
                with patch.object(launcher, "ROOT", root), patch.object(launcher, "helper_output", return_value=""), \
                     patch.object(launcher, "ensure_service", return_value=(base_runtime, base, False)), \
                     patch.object(launcher, "managed_catalog", return_value=(catalog, lambda: None)), \
                     patch.object(quota_recovery, "ensure_watch"):
                    def command(*arguments):
                        with patch.object(sys, "argv", ["launcher.py", "bridge", *arguments]), \
                             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                            if launcher.main() != 0:
                                raise RuntimeError("Synthetic handoff command failed")
                        return rolling_runtime.state(root)

                    try:
                        value = rolling_runtime.ensure(root, base_runtime, base, catalog)
                        runtime = root / "runtime-managed"
                        key, ca = rolling_runtime.private_key(runtime / "front-key"), runtime / "ca.pem"
                        ca_before = ca.read_bytes()
                        normal = {"model": MODEL, "max_tokens": 100, "messages": [{"role": "user", "content": "fixture"}]}
                        restricted = {**normal, "tools": [{"name": "fixture_tool",
                            "allowed_callers": ["code_execution_20260120"],
                            "input_schema": {"type": "object", "properties": {}}}]}
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            pending = pool.submit(stream, value["port"], key, normal, ca_file=ca)
                            try:
                                if not Upstream.admitted.wait(15):
                                    raise RuntimeError("Synthetic stream was not admitted")
                                before = len(Upstream.requests)
                                if post(value["port"], key, ca, restricted) != 200 or len(Upstream.requests) != before + 1:
                                    raise RuntimeError("Synthetic baseline did not expose the caller-policy gap")
                                tools = Upstream.requests[-1].get("tools", [])
                                if len(tools) != 1 or "allowed_callers" in tools[0]:
                                    raise RuntimeError("Synthetic baseline did not omit the caller field")
                                updated = command("upgrade", str(new_bundle))
                                new_identity = runtime_bundle.selected(root)[2]
                                if new_identity == old_identity or pending.done() or updated["pid"] != value["pid"] or updated["port"] != value["port"]:
                                    raise RuntimeError("Synthetic handoff did not preserve the admitted stream")
                                before = len(Upstream.requests)
                                if post(value["port"], key, ca, restricted) != 400 or len(Upstream.requests) != before:
                                    raise RuntimeError("Synthetic candidate did not reject the policy before upstream")
                                if post(value["port"], key, ca, normal) != 200 or len(Upstream.requests) != before + 1:
                                    raise RuntimeError("Synthetic valid request failed after rejection")
                            finally:
                                Upstream.release.set()
                            content, _ = pending.result(timeout=20)
                            if "FIXTURE" != "".join(block.get("text", "") for block in content):
                                raise RuntimeError("Synthetic old stream did not finish")
                        rolled = command("rollback")
                        if (runtime_bundle.selected(root)[2] != old_identity or ca.read_bytes() != ca_before
                                or rolled["pid"] != value["pid"] or rolled["port"] != value["port"]):
                            raise RuntimeError("Synthetic rollback or TLS identity changed")
                        before = len(Upstream.requests)
                        if post(value["port"], key, ca, restricted) != 200 or len(Upstream.requests) != before + 1:
                            raise RuntimeError("Synthetic rollback did not restore baseline behavior")
                        if args.claude_client:
                            persistent_claude(root, rolled, base_runtime, base, transition=lambda: command("upgrade", str(new_bundle)))
                            if runtime_bundle.selected(root)[2] != new_identity:
                                raise RuntimeError("Synthetic client transition did not select candidate")
                        print(json.dumps({"synthetic_only": True, "real_provider": False, "source_lock_mocked": True,
                            "old_stream_completed_after_handoff": True, "ingress_and_ca_unchanged": True,
                            "baseline_dropped_caller_field": True, "candidate_rejected_before_upstream": True,
                            "valid_request_after_rejection": True, "rollback_restored_baseline_behavior": True,
                            "real_client_continued_across_handoff": args.claude_client}, sort_keys=True))
                    finally:
                        Upstream.release.set()
                        if rolling_runtime.state(root) is not None:
                            rolling_runtime.stop(root)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, AssertionError, subprocess.SubprocessError, urllib.error.URLError):
        raise SystemExit("Synthetic caller handoff failed; raw output withheld") from None
