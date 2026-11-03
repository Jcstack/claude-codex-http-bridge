"""Owned-process tests for a memory-only watcher bootstrap experiment."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import quota_memory_fixture
import quota_snapshot_fixture
import runtime_bundle


SOURCE = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == "posix", "Fixture requires inherited POSIX descriptors")
class MemoryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="quota-memory-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        runtime_bundle.write_file(self.root / ".quota-handoff-fixture", b"synthetic quota fixture\n", 0o600)
        self.source = self.root / "reviewed"
        self.source.mkdir(mode=0o700)
        for name in quota_snapshot_fixture.FILES:
            shutil.copyfile(SOURCE / name, self.source / name)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()  # Only children started and retained by this test.
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def manifest(self):
        return {name: hashlib.sha256((self.source / name).read_bytes()).hexdigest()
                for name in quota_snapshot_fixture.FILES}

    def spawn(self, frame, digest):
        child = quota_memory_fixture.spawn_fixture(self.root, frame, digest)
        self.children.append(child)
        return child

    def test_reviewed_memory_survives_source_replacement_and_ignores_pythonpath(self):
        frame, digest = quota_memory_fixture.prepare_fixture(self.root, self.source, self.manifest())
        marker = self.root / "unreviewed-loaded"
        payload = ("from pathlib import Path\n"
                   "Path.home().joinpath('unreviewed-loaded').write_text('synthetic')\n")
        for name in ("quota_recovery.py", "accounts.py", "launcher.py"):
            (self.source / name).write_text(payload, encoding="utf-8")
        injected = self.root / "injected"
        injected.mkdir()
        (injected / "sitecustomize.py").write_text(payload, encoding="utf-8")
        with patch.dict(os.environ, {"PYTHONPATH": str(injected), "OPENAI_API_KEY": "SYNTHETIC_PRIVATE"}):
            child = self.spawn(frame, digest)
        output, _ = child.communicate(input=b"go\n", timeout=15)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(output, b"ready\n")
        self.assertFalse(marker.exists())
        self.assertTrue((self.root / "runtime-quota" / "monitor.lock").is_file())

    def test_frame_tampering_is_rejected_before_watcher_loads(self):
        frame, digest = quota_memory_fixture.prepare_fixture(self.root, self.source, self.manifest())
        child = self.spawn(frame + b" ", digest)
        output, _ = child.communicate(input=b"go\n", timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertEqual(output, b"")
        self.assertFalse((self.root / "runtime-quota").exists())

    @unittest.skipUnless(sys.platform == "darwin", "Snapshot publisher requires macOS exclusive rename")
    def test_published_snapshot_swap_after_capture_does_not_execute_changed_source(self):
        expected = self.manifest()
        snapshot = quota_snapshot_fixture.publish_fixture(self.root, self.source, expected)
        self.addCleanup(snapshot.chmod, 0o700)
        frame, digest = quota_memory_fixture.prepare_fixture(self.root, snapshot, expected)
        entry = snapshot / "quota_recovery.py"
        original = entry.read_bytes()
        replacement = ("from pathlib import Path\n"
                       "Path.home().joinpath('snapshot-attack-loaded').write_text('synthetic')\n")
        snapshot.chmod(0o700)
        try:
            runtime_bundle.write_file(entry, replacement.encode(), 0o400)
            child = self.spawn(frame, digest)
            output, _ = child.communicate(input=b"go\n", timeout=15)
            self.assertEqual(child.returncode, 0)
            self.assertEqual(output, b"ready\n")
            self.assertFalse((self.root / "snapshot-attack-loaded").exists())
        finally:
            runtime_bundle.write_file(entry, original, 0o400)
            snapshot.chmod(0o500)

    def test_unknown_local_dependency_does_not_fall_back_to_fixture_directory(self):
        marker = self.root / "unreviewed-loaded"
        (self.root / "unreviewed_fixture_injection.py").write_text(
            "from pathlib import Path\nPath.home().joinpath('unreviewed-loaded').touch()\n", encoding="utf-8")
        entry = self.source / "quota_recovery.py"
        entry.write_bytes(entry.read_bytes() + b"\nimport unreviewed_fixture_injection\n")
        frame, digest = quota_memory_fixture.prepare_fixture(self.root, self.source, self.manifest())
        child = self.spawn(frame, digest)
        output, _ = child.communicate(input=b"go\n", timeout=10)
        self.assertNotEqual(child.returncode, 0)
        self.assertEqual(output, b"")
        self.assertFalse(marker.exists())

    def test_wrong_manifest_and_credential_shaped_root_are_rejected(self):
        expected = self.manifest()
        expected["accounts.py"] = "0" * 64
        with self.assertRaises(quota_memory_fixture.MemoryFixtureUnavailable):
            quota_memory_fixture.prepare_fixture(self.root, self.source, expected)
        frame, digest = quota_memory_fixture.prepare_fixture(self.root, self.source, self.manifest())
        (self.root / "auth").mkdir()
        with self.assertRaises(quota_memory_fixture.MemoryFixtureUnavailable):
            self.spawn(frame, digest)

    def test_in_memory_watcher_runs_synthetic_recovery_with_fake_account_services(self):
        # Shorten only the fixture's polling and quiet period. The real recovery
        # function runs against stubs that never make network requests.
        entry = self.source / "quota_recovery.py"
        entry.write_bytes(entry.read_bytes() + b"\nPOLL_SECONDS=0.01\nUSAGE_PROBE_SECONDS=0\n")
        (self.source / "launcher.py").write_text('''from pathlib import Path
def read_state(path):
    return None if (Path(path).parent / "synthetic-reset").exists() else {"pid": 1}
def state_is_live(_path, _state): return True
''', encoding="utf-8")
        (self.source / "codex_usage.py").write_text('''def active_bridge_auth(_root):
    return {"account_id": "synthetic-account", "access_token": "synthetic-access"}
''', encoding="utf-8")
        (self.source / "accounts.py").write_text('''from contextlib import contextmanager
from pathlib import Path
@contextmanager
def switch_lock(_root, wait_seconds=0): yield
def local_accounts(_root):
    return [{"name":"synthetic", "fingerprint":"aaaaaaaaaaaaaaaa", "record":
             {"account_id":"synthetic-account", "access_token":"synthetic-access"}}]
def account_subject(_record): return "synthetic-person"
def fingerprint(_record): return "aaaaaaaaaaaaaaaa"
class Management:
    def __init__(self, _root):
        self.state={"port": 12345}; self.key="synthetic-local-key"
def request_json(url, *, method="GET", headers=None, data=None, timeout=0):
    if url.endswith("/credentials"):
        return {"files":[{"name":"synthetic", "type":"codex", "auth_index":"synthetic-index",
                          "cooldowns":[{"scope":"credential", "reason":"credential_quota", "http_status":429}]}]}
    if url.endswith("/wham/usage"):
        return {"rate_limit":{"allowed":True, "limit_reached":False, "primary_window":{"used_percent":1}}}
    if url.endswith("/routing/cooldown/reset") and method=="POST":
        Path.home().joinpath("synthetic-reset").write_text("done",encoding="utf-8")
        return {"status":"ok"}
    raise RuntimeError("unexpected fixture route")
''', encoding="utf-8")
        frame, digest = quota_memory_fixture.prepare_fixture(self.root, self.source, self.manifest())
        child = self.spawn(frame, digest)
        output, _ = child.communicate(input=b"go\n", timeout=15)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(output, b"ready\n")
        self.assertTrue((self.root / "synthetic-reset").is_file())
        status = json.loads((self.root / "runtime-quota" / "status.json").read_text())
        self.assertEqual(status["status"], "recovered")
        self.assertEqual(status["monitor_protocol"], 2)
        self.assertTrue((self.root / "runtime-quota" / "probe-ledger.json").is_file())
        for value in ("synthetic-access", "synthetic-local-key", "synthetic-account"):
            self.assertNotIn(value, json.dumps(status))


if __name__ == "__main__":
    unittest.main()
