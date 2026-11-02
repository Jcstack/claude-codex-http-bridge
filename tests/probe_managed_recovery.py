"""Exercise reboot recovery with disposable services and a synthetic origin.

The only processes stopped here belong to this temporary fixture. No user
service, credentials, model quota, or network endpoint is used.
"""

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile
import process_identity
import autostart
import launcher
import rolling_runtime
import runtime_bundle
from smoke_managed_runtime import MODEL, SPEC, Upstream, stream


def stop_fixture(row):
    rolling_runtime.owned_stop(row)


def main():
    bundle = Path(sys.argv[1]).resolve()
    Upstream.requests = []
    Upstream.admitted.clear()
    Upstream.release.set()
    origins = []

    def origin():
        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origins.append((server, worker))
        return server

    server = origin()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-reboot-probe-") as temporary:
            root = Path(temporary)
            runtime_bundle.private_dir(root)
            base_runtime = root / "runtime"
            runtime_bundle.private_dir(base_runtime)
            source = base_runtime / "session-fixture"
            runtime_bundle.private_dir(source)
            for name, contents in (("config.yaml", "synthetic"), ("key", "a" * 64), ("proxy.log", "")):
                api_profile.write_private(source / name, contents)
            base = {"process_identity": process_identity.stamp(os.getpid()), "pid": os.getpid(), "port": server.server_port, "session": source.name}
            rolling_runtime.write(base_runtime / "service.json", base)
            with patch.object(launcher, "ROOT", root):
                runtime_bundle.activate(root, bundle)
                value = rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC})
                runtime = root / "runtime-managed"
                front = rolling_runtime.private_key(runtime / "front-key")
                ca = (runtime / "ca.pem").read_bytes()
                initial_port = value["port"]
                old_backend = value["generations"][value["current"]]
                try:
                    # Only the fixture ingress died. Its old backend may still
                    # exist and must not be killed using a potentially reused PID.
                    stop_fixture(value)
                    value = rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC})
                    assert value["port"] == initial_port
                    assert launcher.listener_pids(old_backend["port"]) == {old_backend["pid"]}
                    stop_fixture(old_backend)

                    # Simulate a reboot: both managed processes have exited and
                    # the original service returns at a different local port.
                    stop_fixture(value["generations"][value["current"]])
                    stop_fixture(value)
                    server2 = origin()
                    base = {**base, "port": server2.server_port}
                    rolling_runtime.write(base_runtime / "service.json", base)
                    before = rolling_runtime.state(root)
                    before_manifest = (runtime / "backend.json").read_bytes()
                    with socket.socket() as occupant:
                        occupant.bind(("127.0.0.1", initial_port))
                        occupant.listen()
                        try:
                            rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC})
                        except RuntimeError as error:
                            assert "occupied" in str(error)
                        else:
                            raise AssertionError("A foreign listener was accepted")
                        assert rolling_runtime.state(root) == before
                        assert (runtime / "backend.json").read_bytes() == before_manifest

                    def fail_before_publish():
                        raise RuntimeError("synthetic account catalog changed")

                    try:
                        rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC}, before_publish=fail_before_publish)
                    except RuntimeError as error:
                        assert "synthetic account" in str(error)
                    else:
                        raise AssertionError("A changed account catalog was accepted")
                    assert rolling_runtime.state(root) == before
                    assert (runtime / "backend.json").read_bytes() == before_manifest
                    assert not launcher.listener_pids(initial_port)

                    def synthetic_start():
                        rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC})
                        return 0

                    with patch.object(launcher, "helper_output") as verify, \
                         patch.object(launcher, "bridge_start", side_effect=synthetic_start) as restart:
                        assert autostart.run_once(root) == 0
                    verify.assert_called_once_with("verify")
                    restart.assert_called_once_with()
                    assert json.loads((base_runtime / "autostart-status.json").read_text())["result"] == "recovered"
                    value = rolling_runtime.state(root)
                    assert value["port"] == initial_port
                    assert rolling_runtime.private_key(runtime / "front-key") == front
                    assert (runtime / "ca.pem").read_bytes() == ca
                    assert rolling_runtime.catalog_matches(root, value, {MODEL: SPEC})
                    payload = {"model": MODEL, "max_tokens": 100,
                               "messages": [{"role": "user", "content": "fixture"}]}
                    try:
                        origin_metadata = rolling_runtime.get_json(runtime, initial_port, "/bridge/origin/v1/models",
                                                                  rolling_runtime.private_key(runtime / "origin-guard-key"))
                        assert MODEL in {item["id"] for item in origin_metadata.get("data", [])}
                        blocks, _ = stream(initial_port, front, payload, ca_file=runtime / "ca.pem")
                    except Exception:
                        # Only fixed diagnostic labels leave the disposable fixture.
                        # Runtime logs can contain keys or requests, so never print them.
                        labels = ("unknown authority", "certificate", "tls handshake", "connection refused",
                                  "deadline", "timeout", "unauthorized", "forbidden", "502", "503", "eof")
                        row = value["generations"][value["current"]]
                        classes = {}
                        for name, path in (("ingress", runtime / "ingress.log"),
                                           ("backend", runtime / value["current"] / "proxy.log")):
                            text = path.read_text(errors="replace").lower() if path.is_file() else ""
                            classes[name] = [label for label in labels if label in text]
                        print(json.dumps({"synthetic_recovery_failed": True,
                            "front_owner_matches": launcher.listener_pids(value["port"]) == {value["pid"]},
                            "backend_owner_matches": launcher.listener_pids(row["port"]) == {row["pid"]},
                            "origin_owner_matches": launcher.listener_pids(base["port"]) == {base["pid"]},
                            "origin_requests": len(Upstream.requests), "log_classes": classes}), flush=True)
                        raise
                    assert "".join(block.get("text", "") for block in blocks) == "FIXTURE"
                    print(json.dumps({"synthetic_only": True, "same_port_ca_and_client_key": "pass",
                                      "new_origin_port": "pass", "occupied_port_refused": "pass",
                                      "catalog_failure_rolled_back": "pass", "old_backend_untouched": "pass",
                                      "periodic_recovery": "pass", "messages_after_recovery": "pass"}))
                finally:
                    Upstream.release.set()
                    current = rolling_runtime.state(root)
                    if current and launcher.listener_pids(current["port"]) == {current["pid"]}:
                        rolling_runtime.stop(root)
                    if launcher.listener_pids(old_backend["port"]) == {old_backend["pid"]}:
                        stop_fixture(old_backend)
    finally:
        for server, worker in origins:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)


if __name__ == "__main__":
    main()
