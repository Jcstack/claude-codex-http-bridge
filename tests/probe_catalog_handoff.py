"""Exercise catalog handoff with disposable services and synthetic accounts."""

from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

from live_compare_translators import stream
from smoke_managed_runtime import MODEL, SPEC, Upstream
import accounts
import api_profile
import launcher
import rolling_runtime
import runtime_bundle


def main() -> None:
    if len(sys.argv) != 2:
        raise RuntimeError("Usage: probe_catalog_handoff.py /path/to/reviewed-bundle")
    bundle = Path(sys.argv[1]).resolve()
    runtime_bundle.verify(bundle)
    Upstream.requests = []
    Upstream.admitted.clear()
    Upstream.release.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-catalog-handoff-") as temporary:
            root = Path(temporary)
            runtime_bundle.private_dir(root)
            base_runtime = root / "runtime"
            runtime_bundle.private_dir(base_runtime)
            source = base_runtime / "session-fixture"
            runtime_bundle.private_dir(source)
            for name, text in (("config.yaml", "synthetic"), ("key", "a" * 64), ("proxy.log", "")):
                api_profile.write_private(source / name, text)
            base = {"pid": os.getpid(), "port": server.server_port, "session": source.name}
            management = SimpleNamespace(state=base)
            with patch.object(launcher, "ROOT", root):
                runtime_bundle.activate(root, bundle)
                value = None
                try:
                    value = rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC})
                    runtime = root / "runtime-managed"
                    key = rolling_runtime.private_key(runtime / "front-key")
                    request = {"model": MODEL, "max_tokens": 100,
                               "messages": [{"role": "user", "content": "synthetic fixture"}]}
                    updated_models = {MODEL: {**SPEC, "context_window": 900000}, "gpt-6.2-sol": SPEC}
                    different_models = {MODEL: SPEC, "gpt-6.2-sol": SPEC}
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(stream, value["port"], key, request, ca_file=runtime / "ca.pem")
                        if not Upstream.admitted.wait(15):
                            raise RuntimeError("Synthetic stream was not admitted")
                        with patch.object(accounts, "selected_catalog", return_value={MODEL: SPEC}):
                            assert not accounts.synchronize_managed_catalog(root, management)
                        old_state = rolling_runtime.state(root)
                        old_manifest = (runtime / "backend.json").read_bytes()
                        with patch.object(accounts, "selected_catalog", side_effect=[updated_models, different_models]):
                            try:
                                accounts.synchronize_managed_catalog(root, management)
                            except RuntimeError as error:
                                assert "changed" in str(error)
                            else:
                                raise AssertionError("Changed catalog was published")
                        assert rolling_runtime.state(root) == old_state
                        assert (runtime / "backend.json").read_bytes() == old_manifest
                        with patch.object(accounts, "selected_catalog", return_value=updated_models):
                            assert accounts.synchronize_managed_catalog(root, management)
                        updated = rolling_runtime.state(root)
                        assert updated["pid"] == value["pid"] and updated["port"] == value["port"]
                        assert updated["current"] != value["current"]
                        assert rolling_runtime.catalog_matches(root, updated, updated_models)
                        content, _ = stream(value["port"], key, request, ca_file=runtime / "ca.pem")
                        assert "FIXTURE" == "".join(block.get("text", "") for block in content)
                        Upstream.release.set()
                        pending.result(timeout=20)
                    rolling_runtime.ensure(root, base_runtime, base, updated_models)
                    print(json.dumps({"mock_only": True, "unchanged_catalog_no_roll": True,
                        "changed_during_prepare_rollback": True, "changed_catalog_published": True,
                        "old_stream_completed": True, "new_request_completed": True,
                        "ingress_unchanged": True}))
                finally:
                    Upstream.release.set()
                    if rolling_runtime.state(root) is not None:
                        rolling_runtime.stop(root)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
