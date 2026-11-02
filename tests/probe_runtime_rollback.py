"""Test upgrade and rollback with a fake origin and an owned managed endpoint.

The two private bundles use the same reviewed binaries but intentionally
different synthetic source identities. No user account or model is contacted.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
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


def main() -> None:
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] != "--claude-client"):
        raise RuntimeError("Usage: probe_runtime_rollback.py BUNDLE [--claude-client]")
    bundle = Path(sys.argv[1]).resolve()
    source_manifest, _ = runtime_bundle.verify(bundle)
    Upstream.requests = []
    Upstream.quota_mode = False
    Upstream.admitted.clear()
    Upstream.release.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-runtime-rollback-") as temporary:
            root = Path(temporary)
            runtime_bundle.private_dir(root)
            base_runtime = root / "runtime"
            runtime_bundle.private_dir(base_runtime)
            source = base_runtime / "session-fixture"
            runtime_bundle.private_dir(source)
            for name, value in (("config.yaml", "synthetic"), ("key", "a" * 64), ("proxy.log", "")):
                api_profile.write_private(source / name, value)
            base = {"process_identity": process_identity.stamp(os.getpid()), "pid": os.getpid(),
                    "port": server.server_port, "session": source.name}
            old_bundle = root / "old-bundle"
            old_source = {**source_manifest["source"], "synthetic_fixture": True}
            with patch.object(runtime_bundle, "source_lock", return_value=old_source):
                runtime_bundle.pack(bundle / source_manifest["binaries"]["proxy"]["file"],
                                    bundle / source_manifest["binaries"]["ingress"]["file"], old_bundle)
                old_identity = runtime_bundle.activate(root, old_bundle)
            model_catalog = {MODEL: SPEC}
            with patch.object(launcher, "ROOT", root), patch.object(launcher, "helper_output", return_value=""), \
                 patch.object(launcher, "ensure_service", return_value=(base_runtime, base, False)), \
                 patch.object(launcher, "managed_catalog", return_value=(model_catalog, lambda: None)), \
                 patch.object(quota_recovery, "ensure_watch"):
                def command(*arguments):
                    with patch.object(sys, "argv", ["launcher.py", "bridge", *arguments]), \
                         redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        assert launcher.main() == 0
                    return rolling_runtime.state(root)

                try:
                    value = rolling_runtime.ensure(root, base_runtime, base, model_catalog)
                    runtime = root / "runtime-managed"
                    key = rolling_runtime.private_key(runtime / "front-key")
                    ca = runtime / "ca.pem"
                    ca_before = ca.read_bytes()
                    request = {"model": MODEL, "max_tokens": 100,
                               "messages": [{"role": "user", "content": "synthetic fixture"}]}
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(stream, value["port"], key, request, ca_file=ca)
                        if not Upstream.admitted.wait(15): raise RuntimeError("Synthetic stream was not admitted")
                        # The replacement has already been published when old
                        # generation retirement runs. A failure there must not
                        # restore an inconsistent selected identity.
                        with patch.object(rolling_runtime, "drain", side_effect=ValueError("synthetic retirement failure")):
                            updated = command("upgrade", str(bundle))
                        new_identity = runtime_bundle.selected(root)[2]
                        assert new_identity != old_identity and not pending.done()
                        assert updated["port"] == value["port"] and updated["pid"] == value["pid"]
                        assert json.loads(runtime_bundle.history_snapshot(root)) == {
                            "current": new_identity, "previous": old_identity}
                        content, _ = stream(value["port"], key, request, ca_file=ca)
                        assert "FIXTURE" == "".join(b.get("text", "") for b in content)
                        rolled = command("rollback")
                        assert runtime_bundle.selected(root)[2] == old_identity and not pending.done()
                        assert rolled["port"] == value["port"] and rolled["pid"] == value["pid"]
                        content, _ = stream(value["port"], key, request, ca_file=ca)
                        assert "FIXTURE" == "".join(b.get("text", "") for b in content)
                        Upstream.release.set()
                        content, _ = pending.result(timeout=20)
                        assert "FIXTURE" == "".join(b.get("text", "") for b in content)
                    updated = command("rollback")
                    assert runtime_bundle.selected(root)[2] == new_identity
                    assert ca.read_bytes() == ca_before
                    assert launcher.listener_pids(base["port"]) == {base["pid"]}
                    if len(sys.argv) == 3:
                        # Keep one real Claude process and conversation across
                        # another rollback, using only the isolated fake origin.
                        persistent_claude(root, updated, base_runtime, base, transition=lambda: command("rollback"))
                        assert runtime_bundle.selected(root)[2] == old_identity
                    print(json.dumps({"mock_only": True, "old_stream_completed_after_two_handoffs": True,
                        "new_requests_on_each_generation": True, "stable_ingress_and_ca": True,
                        "origin_process_preserved": True, "post_publish_retirement_failure_deferred": True,
                        "real_client_continued_across_rollback": len(sys.argv) == 3}))
                finally:
                    Upstream.release.set()
                    if rolling_runtime.state(root) is not None: rolling_runtime.stop(root)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
