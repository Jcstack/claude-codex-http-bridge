"""Explore identity-bound handoff with owned synthetic processes only.

This is not a production handoff: matching a real monitor to its lock, safely
starting a successor, and recovery from handoff failures need separate work.
"""

import ctypes
from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import accounts
import process_identity
import quota_monitor_identity
import quota_monitor_handoff
import quota_recovery
import quota_snapshot_fixture
import runtime_bundle


SOURCE = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform == "darwin", "Requires macOS audit-token signaling")
class OwnedDarwinHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="quota-handoff-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.children = []
        self.addCleanup(self.restore_snapshot_directories)
        self.addCleanup(self.stop_children)
        self.library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.library.proc_pidinfo.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
        self.library.proc_pidinfo.restype = ctypes.c_int
        native = getattr(self.library, "proc_signal_with_audittoken", None)
        if native is None:
            self.skipTest("The system does not expose audit-token signaling")
        native.argtypes = (ctypes.POINTER(ctypes.c_uint32), ctypes.c_int)
        native.restype = ctypes.c_int
        self.native = native

    def start(self, program, *arguments):
        allowed = ("PATH", "TMPDIR", "LANG")
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        environment["HOME"] = str(self.root)
        child = subprocess.Popen([sys.executable, "-I", "-c", program, *map(str, arguments)],
                                 cwd=self.root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        self.children.append(child)
        return child

    def start_monitor_script(self, *extra):
        program = r'''
import json,sys,time
from pathlib import Path
sys.path.insert(0,SOURCE)
import quota_recovery,runtime_bundle
root=Path(sys.argv[3])
with quota_recovery.monitor_lock(root/'runtime-quota') as acquired:
    assert acquired
    payload={'status':'no_stale_credential_quota','checked_at':time.time(),'monitor_pid':__import__('os').getpid()}
    runtime_bundle.write_file(root/'runtime-quota'/'status.json',json.dumps(payload).encode(),0o600)
    print('ready',flush=True)
    sys.stdin.readline()
'''.replace("SOURCE", repr(str(SOURCE)))
        path = self.root / "quota_recovery.py"
        path.write_text(program, encoding="utf-8")
        path.chmod(0o700)
        allowed = ("PATH", "TMPDIR", "LANG")
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        environment["HOME"] = str(self.root)
        child = subprocess.Popen([sys.executable, "-I", str(path), "watch", "--root", str(self.root), *extra],
                                 cwd=self.root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        self.children.append(child)
        self.assertEqual(self.read_line(child), "ready")
        return child

    def start_synthetic_runtime(self):
        runtime_bundle.write_file(self.root / ".quota-handoff-fixture", b"synthetic quota fixture\n", 0o600)
        for name in ("quota_recovery.py", "runtime_bundle.py", "api_profile.py", "process_identity.py"):
            target = self.root / name
            shutil.copyfile(SOURCE / name, target)
            target.chmod(0o700)
        (self.root / "codex_usage.py").write_text("# no real account access\n", encoding="utf-8")
        (self.root / "launcher.py").write_text(r'''
from pathlib import Path
def read_state(path):
    return {'fixture':True} if (Path(path).parent/'service-live').exists() else None
def state_is_live(_path,_state):
    return True
''', encoding="utf-8")
        (self.root / "accounts.py").write_text(r'''
from contextlib import contextmanager
from pathlib import Path
import fcntl,os,time
@contextmanager
def switch_lock(root,wait_seconds=0):
    directory=Path(root)/'accounts'
    directory.mkdir(mode=0o700,exist_ok=True)
    fd=os.open(directory/'.switch.lock',os.O_CREAT|os.O_RDWR,0o600)
    deadline=time.monotonic()+wait_seconds
    try:
        while True:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic()>=deadline:
                    raise RuntimeError('Synthetic lock busy')
                time.sleep(.02)
        try:
            yield directory
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN)
    finally:
        os.close(fd)
def local_accounts(_root):
    return []
''', encoding="utf-8")
        (self.root / "service-live").touch()
        environment = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
        environment["HOME"] = str(self.root)
        child = subprocess.Popen([sys.executable, "-I", str(self.root / "quota_recovery.py"),
                                  "watch", "--root", str(self.root)], cwd=self.root, env=environment,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        self.children.append(child)
        deadline = time.monotonic() + 10
        status = self.root / "runtime-quota" / "status.json"
        while time.monotonic() < deadline:
            if status.is_file():
                return child
            self.assertIsNone(child.poll(), "Synthetic monitor exited before reporting")
            time.sleep(.05)
        self.fail("Synthetic monitor did not report")

    def stop_children(self):
        for child in self.children:
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            if child.poll() is None:
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # These are unreaped children created by this test.
                    child.terminate()
                    child.wait(timeout=3)
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()

    def restore_snapshot_directories(self):
        parent = self.root / "runtime-quota" / "source-snapshots"
        if parent.is_dir() and not parent.is_symlink():
            for path in parent.iterdir():
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)

    def read_line(self, child):
        ready, _, _ = select.select([child.stdout], [], [], 10)
        self.assertTrue(ready, "Synthetic child did not reach the expected state")
        line = child.stdout.readline()
        self.assertTrue(line, "Synthetic child exited before the expected state")
        return line.decode().strip()

    def identity(self, child):
        # XNU's private combined flavor reads BSD birth and pidversion together.
        # Check its full size and the public birth reader before using the data.
        data = ctypes.create_string_buffer(192)
        self.assertEqual(self.library.proc_pidinfo(child.pid, 18, 0, data, len(data)), len(data))
        self.assertEqual(int.from_bytes(data.raw[12:16], sys.byteorder), child.pid)
        self.assertEqual(int.from_bytes(data.raw[20:24], sys.byteorder), os.getuid())
        self.assertEqual(data.raw[120:136].hex(), process_identity.capture(child.pid)[0])
        return int.from_bytes(data.raw[168:172], sys.byteorder)

    def send(self, child, version):
        token = (ctypes.c_uint32 * 8)()
        token[5], token[7] = child.pid, version
        return self.native(token, signal.SIGTERM)

    def fixture_manifest(self):
        return {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                for name in quota_snapshot_fixture.FILES}

    def invoke_handoff(self):
        expected = hashlib.sha256((SOURCE / "quota_recovery.py").read_bytes()).hexdigest()
        return quota_monitor_handoff.handoff(self.root, expected_source_sha256=expected,
                                             fixture_source=self.root, expected_manifest=self.fixture_manifest())

    def test_an_open_lock_file_alone_does_not_identify_its_exclusive_owner(self):
        import fcntl

        owner_code = r'''
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import quota_recovery
with quota_recovery.monitor_lock(Path(sys.argv[2])/'runtime-quota') as acquired:
    assert acquired
    print('locked',flush=True)
    sys.stdin.readline()
'''
        reader_code = r'''
import sys
with open(sys.argv[1],'rb'):
    print('open',flush=True)
    sys.stdin.readline()
'''
        owner = self.start(owner_code, SOURCE, self.root)
        self.assertEqual(self.read_line(owner), "locked")
        path = self.root / "runtime-quota" / "monitor.lock"
        before = path.stat()
        observed = quota_recovery.monitor_status(self.root)
        self.assertEqual((observed["lock_observation"], observed["liveness"]), ("conflict", "unverified"))
        self.assertEqual((path.stat().st_mtime_ns, path.stat().st_size), (before.st_mtime_ns, before.st_size))
        reader = self.start(reader_code, path)
        self.assertEqual(self.read_line(reader), "open")
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"}
        result = subprocess.run(["/usr/sbin/lsof", "-nP", "-Fp", "--", str(path)],
                                env=environment, cwd=self.root, capture_output=True, text=True,
                                timeout=10, check=True)
        openers = {int(line[1:]) for line in result.stdout.splitlines() if line.startswith("p")}
        self.assertTrue({owner.pid, reader.pid}.issubset(openers))
        # Even a reader that has not locked the file appears in the openers.
        # A separate nonblocking lock attempt only establishes that some
        # process holds it, and must not be used on the live service here.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self.assertEqual((os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino),
                             (path.stat().st_dev, path.stat().st_ino))
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        self.assertIsNone(owner.poll())
        self.assertIsNone(reader.poll())
        owner.stdin.write(b"done\n")
        owner.stdin.flush()
        self.assertEqual(owner.wait(timeout=5), 0)
        self.assertEqual(quota_recovery.monitor_status(self.root)["lock_observation"], "no_conflict")
        self.assertIsNone(reader.poll())

    def test_read_only_lock_observation_refuses_symlinks(self):
        directory = self.root / "runtime-quota"
        directory.mkdir(mode=0o700)
        outside = self.root / "outside"
        outside.write_bytes(b"synthetic")
        (directory / "monitor.lock").symlink_to(outside)
        self.assertEqual(quota_recovery.monitor_status(self.root)["lock_observation"], "unavailable")
        self.assertEqual(outside.read_bytes(), b"synthetic")

    def test_read_only_lock_observation_refuses_broad_permissions(self):
        directory = self.root / "runtime-quota"
        directory.mkdir(mode=0o700)
        path = directory / "monitor.lock"
        path.touch(mode=0o600)
        path.chmod(0o644)
        self.assertEqual(quota_recovery.monitor_status(self.root)["lock_observation"], "unavailable")

    def test_read_only_monitor_evidence_rejects_ambiguous_openers_and_stale_reports(self):
        owner = self.start_monitor_script()
        observed = quota_monitor_identity.observe(self.root)
        self.assertEqual(observed.pid, owner.pid)
        self.assertIsNone(owner.poll())
        path = self.root / "runtime-quota" / "monitor.lock"
        reader = self.start("import sys; f=open(sys.argv[1],'rb'); print('open',flush=True); sys.stdin.readline()", path)
        self.assertEqual(self.read_line(reader), "open")
        with self.assertRaises(quota_monitor_identity.MonitorEvidenceUnavailable):
            quota_monitor_identity.observe(self.root)
        self.assertIsNone(owner.poll())
        self.assertIsNone(reader.poll())
        reader.stdin.write(b"done\n")
        reader.stdin.flush()
        self.assertEqual(reader.wait(timeout=5), 0)
        status = self.root / "runtime-quota" / "status.json"
        runtime_bundle.write_file(status, json.dumps({"status": "no_stale_credential_quota", "checked_at": 1,
                                                       "monitor_pid": owner.pid}).encode(), 0o600)
        with self.assertRaises(quota_monitor_identity.MonitorEvidenceUnavailable):
            quota_monitor_identity.observe(self.root)
        self.assertIsNone(owner.poll())

    def test_read_only_monitor_evidence_rejects_unexpected_command_arguments(self):
        owner = self.start_monitor_script("--synthetic-extra")
        with self.assertRaises(quota_monitor_identity.MonitorEvidenceUnavailable):
            quota_monitor_identity.observe(self.root)
        self.assertIsNone(owner.poll())

    def test_read_only_monitor_evidence_rejects_broad_script_permissions(self):
        owner = self.start_monitor_script()
        (self.root / "quota_recovery.py").chmod(0o755)
        with self.assertRaises(quota_monitor_identity.MonitorEvidenceUnavailable):
            quota_monitor_identity.observe(self.root)
        self.assertIsNone(owner.poll())

    def test_read_only_monitor_evidence_rejects_a_changed_generation(self):
        owner = self.start_monitor_script()
        birth, unique, generation = quota_monitor_identity._snapshot(owner.pid)
        with patch.object(quota_monitor_identity, "_snapshot",
                          side_effect=[(birth, unique, generation), (birth, unique, generation ^ 1)]):
            with self.assertRaises(quota_monitor_identity.MonitorEvidenceUnavailable):
                quota_monitor_identity.observe(self.root)
        self.assertIsNone(owner.poll())

    def test_candidate_handoff_replaces_only_an_owned_synthetic_watcher(self):
        old = self.start_synthetic_runtime()
        sentinel = self.start("import sys; print('sentinel-ready',flush=True); sys.stdin.readline()")
        self.assertEqual(self.read_line(sentinel), "sentinel-ready")
        spawn = quota_monitor_handoff._spawn_successor
        successors = []

        def capture(root, script):
            child = spawn(root, script)
            self.children.append(child)
            successors.append(child)
            return child

        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=capture):
            first = self.invoke_handoff()
            self.assertEqual(first, {"status": "replacement_reported", "monitor_protocol": 2})
            self.assertEqual(old.wait(timeout=5), -signal.SIGTERM)
            self.assertEqual(len(successors), 1)
            snapshots = list((self.root / "runtime-quota" / "source-snapshots").iterdir())
            self.assertEqual(len(snapshots), 1)
            allowed_script = snapshots[0] / "quota_recovery.py"
            self.assertEqual(quota_monitor_identity.observe(self.root, allowed_script=allowed_script).pid, successors[0].pid)
            second = self.invoke_handoff()
        self.assertEqual(second, {"status": "replacement_reported", "monitor_protocol": 2})
        self.assertEqual(successors[0].wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(len(successors), 2)
        successor = successors[1]
        self.assertIsNone(successor.poll())
        self.assertEqual(quota_monitor_identity.observe(self.root, allowed_script=allowed_script).pid, successor.pid)
        self.assertIsNone(sentinel.poll())
        successor.terminate()  # Only the test-owned replacement process.
        successor.wait(timeout=5)

    def test_candidate_handoff_keeps_old_watcher_when_successor_cannot_start(self):
        old = self.start_synthetic_runtime()
        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=OSError("synthetic failure")):
            with self.assertRaises(quota_monitor_handoff.HandoffUnavailable):
                self.invoke_handoff()
        self.assertIsNone(old.poll())
        self.assertEqual(quota_monitor_identity.observe(self.root).pid, old.pid)
        old.terminate()  # Only the test-owned monitor.
        old.wait(timeout=5)

    def test_candidate_handoff_refuses_changed_evidence_before_any_signal(self):
        old = self.start_synthetic_runtime()
        original = quota_monitor_identity.observe(self.root)
        successors = []
        spawn = quota_monitor_handoff._spawn_successor

        def capture(root, script):
            child = spawn(root, script)
            self.children.append(child)
            successors.append(child)
            return child

        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=capture), \
             patch.object(quota_monitor_identity, "observe", side_effect=[original, replace(original, generation=original.generation ^ 1)]), \
             patch.object(quota_monitor_handoff, "_signal") as send:
            with self.assertRaises(quota_monitor_handoff.HandoffUnavailable):
                self.invoke_handoff()
        send.assert_not_called()
        self.assertIsNone(old.poll())
        self.assertEqual(len(successors), 1)
        self.assertEqual(successors[0].wait(timeout=5), 0)
        old.terminate()  # Only the test-owned monitor.
        old.wait(timeout=5)

    def test_candidate_handoff_refuses_a_source_replaced_after_successor_is_ready(self):
        old = self.start_synthetic_runtime()
        ready = quota_monitor_handoff._ready
        successors = []
        spawn = quota_monitor_handoff._spawn_successor

        def capture(root, script):
            child = spawn(root, script)
            self.children.append(child)
            successors.append(child)
            return child

        def replace_source(child):
            result = ready(child)
            self.assertTrue(result)
            runtime_bundle.write_file(self.root / "quota_recovery.py", b"# synthetic replacement\n", 0o700)
            return result

        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=capture), \
             patch.object(quota_monitor_handoff, "_ready", side_effect=replace_source), \
             patch.object(quota_monitor_handoff, "_signal") as send:
            with self.assertRaises(quota_monitor_handoff.HandoffUnavailable):
                self.invoke_handoff()
        send.assert_not_called()
        self.assertIsNone(old.poll())
        self.assertEqual(successors[0].wait(timeout=5), 0)
        old.terminate()  # Only the test-owned monitor.
        old.wait(timeout=5)

    def test_candidate_handoff_refuses_unreviewed_replacement_source(self):
        old = self.start_synthetic_runtime()
        runtime_bundle.write_file(self.root / "quota_recovery.py", b"# synthetic replacement\n", 0o700)
        with patch.object(quota_monitor_handoff, "_spawn_successor") as spawn, \
             patch.object(quota_monitor_handoff, "_signal") as send:
            with self.assertRaises(quota_monitor_handoff.HandoffUnavailable):
                self.invoke_handoff()
        spawn.assert_not_called()
        send.assert_not_called()
        self.assertIsNone(old.poll())
        old.terminate()  # Only the test-owned monitor.
        old.wait(timeout=5)

    def test_candidate_handoff_requires_a_valid_reviewed_source_hash(self):
        with self.assertRaises(quota_monitor_handoff.HandoffUnavailable):
            quota_monitor_handoff.handoff(self.root, expected_source_sha256="not-a-hash",
                                          fixture_source=self.root, expected_manifest={})
        self.assertEqual(list(self.root.iterdir()), [])
        expected = hashlib.sha256((SOURCE / "quota_recovery.py").read_bytes()).hexdigest()
        with self.assertRaises(quota_monitor_handoff.HandoffUnavailable):
            quota_monitor_handoff.handoff(self.root, expected_source_sha256=expected,
                                          fixture_source=self.root, expected_manifest={})
        self.assertEqual(list(self.root.iterdir()), [])
        old = self.start_synthetic_runtime()
        with patch.object(quota_monitor_handoff, "_spawn_successor") as spawn:
            with self.assertRaises(quota_monitor_handoff.HandoffUnavailable):
                quota_monitor_handoff.handoff(self.root, expected_source_sha256="a" * 64,
                                              fixture_source=self.root, expected_manifest=self.fixture_manifest())
        spawn.assert_not_called()
        self.assertIsNone(old.poll())
        old.terminate()  # Only the test-owned monitor.
        old.wait(timeout=5)

    def test_snapshot_child_ignores_an_aba_swap_of_top_level_source(self):
        """The prior unversioned coordinator armed this synthetic child."""
        old = self.start_synthetic_runtime()
        entry = self.root / "quota_recovery.py"
        saved = self.root / "saved-reviewed-source.py"
        loaded, armed = self.root / "synthetic-loaded", self.root / "synthetic-armed"
        program = r'''
import sys,time
from pathlib import Path
root=Path(sys.argv[3])
(root/'synthetic-loaded').touch()
print('ready',flush=True)
if sys.stdin.readline()=='go\n':
    (root/'synthetic-armed').touch()
time.sleep(.3)
'''.encode()
        spawn = quota_monitor_handoff._spawn_successor
        children = []

        def swap_during_spawn(root, script):
            os.replace(entry, saved)
            try:
                runtime_bundle.write_file(entry, program, 0o700)
                child = spawn(root, script)
                self.children.append(child)
                children.append(child)
                readable, _, _ = select.select([child.stdout], [], [], 5)
                self.assertTrue(readable, "Snapshot successor did not become ready")
            finally:
                os.replace(saved, entry)
            return child

        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=swap_during_spawn), \
             patch.object(quota_monitor_handoff, "_signal", return_value=errno.EPERM):
            result = self.invoke_handoff()
        self.assertEqual(result, {"status": "replacement_unverified"})
        self.assertFalse(loaded.exists())
        self.assertFalse(armed.exists())
        children[0].terminate()  # Only the test-owned waiting child.
        children[0].wait(timeout=5)
        self.assertIsNone(old.poll())
        old.terminate()  # Only the test-owned monitor.
        old.wait(timeout=5)

    def test_unversioned_spawn_negative_control_remains_vulnerable_to_aba(self):
        old = self.start_synthetic_runtime()
        entry = self.root / "quota_recovery.py"
        saved = self.root / "saved-reviewed-source.py"
        loaded, armed = self.root / "synthetic-loaded", self.root / "synthetic-armed"
        program = r'''
import sys,time
from pathlib import Path
root=Path(sys.argv[3])
(root/'synthetic-loaded').touch()
print('ready',flush=True)
if sys.stdin.readline()=='go\n':
    (root/'synthetic-armed').touch()
time.sleep(.3)
'''.encode()
        spawn = quota_monitor_handoff._spawn_successor
        children = []

        def unsafe_spawn(root, _snapshot_script):
            os.replace(entry, saved)
            try:
                runtime_bundle.write_file(entry, program, 0o700)
                child = spawn(root, entry)  # Deliberately emulate the old unversioned launch.
                self.children.append(child)
                children.append(child)
                deadline = time.monotonic() + 5
                while not loaded.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(loaded.exists(), "Synthetic negative control did not load")
            finally:
                os.replace(saved, entry)
            return child

        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=unsafe_spawn), \
             patch.object(quota_monitor_handoff, "_signal", return_value=errno.EPERM):
            result = self.invoke_handoff()
        self.assertEqual(result, {"status": "replacement_unverified"})
        self.assertEqual(children[0].wait(timeout=5), 0)
        self.assertTrue(armed.exists())
        self.assertIsNone(old.poll())
        old.terminate()  # Only the test-owned monitor.
        old.wait(timeout=5)

    def test_same_user_snapshot_tampering_negative_control_is_not_a_sandbox(self):
        old = self.start_synthetic_runtime()
        loaded, armed = self.root / "synthetic-loaded", self.root / "synthetic-armed"
        program = r'''
import sys,time
from pathlib import Path
root=Path(sys.argv[3])
(root/'synthetic-loaded').touch()
print('ready',flush=True)
if sys.stdin.readline()=='go\n':
    (root/'synthetic-armed').touch()
time.sleep(.3)
'''.encode()
        spawn = quota_monitor_handoff._spawn_successor
        children = []

        def tamper_snapshot(root, script):
            directory = script.parent
            saved = directory / "synthetic-saved-source.py"
            directory.chmod(0o700)
            os.replace(script, saved)
            try:
                runtime_bundle.write_file(script, program, 0o400)
                child = spawn(root, script)
                self.children.append(child)
                children.append(child)
                deadline = time.monotonic() + 5
                while not loaded.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(loaded.exists(), "Synthetic snapshot tampering did not load")
            finally:
                os.replace(saved, script)
                directory.chmod(0o500)
            return child

        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=tamper_snapshot), \
             patch.object(quota_monitor_handoff, "_signal", return_value=errno.EPERM):
            result = self.invoke_handoff()
        self.assertEqual(result, {"status": "replacement_unverified"})
        self.assertEqual(children[0].wait(timeout=5), 0)
        self.assertTrue(armed.exists())
        self.assertIsNone(old.poll())
        old.terminate()  # Only the test-owned monitor.
        old.wait(timeout=5)

    def test_owned_source_snapshot_binds_watcher_to_the_separate_runtime_root(self):
        runtime_bundle.write_file(self.root / ".quota-handoff-fixture", b"synthetic quota fixture\n", 0o600)
        expected = {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in quota_snapshot_fixture.FILES}
        snapshot = quota_snapshot_fixture.publish_fixture(self.root, SOURCE, expected)
        before = {path.name: path.read_bytes() for path in snapshot.iterdir()}
        self.addCleanup(snapshot.chmod, 0o700)
        # A different top-level source must never be imported by this child.
        (self.root / "quota_recovery.py").write_text(
            "from pathlib import Path\nPath(__file__).with_name('unreviewed-used').touch()\n", encoding="utf-8")
        program = r'''
import os,sys
from pathlib import Path
from types import SimpleNamespace
root,snapshot=Path(sys.argv[1]),Path(sys.argv[2])
sys.path.insert(0,str(snapshot))
import accounts,api_profile,codex_usage,launcher,process_identity,quota_recovery as q,runtime_bundle
for module in (accounts,api_profile,codex_usage,launcher,process_identity,q,runtime_bundle):
    assert Path(module.__file__).resolve().parent==snapshot.resolve()
assert os.path.samefile(launcher.ROOT,root)
record={'account_id':'fixture','email':'fixture@example.invalid','access_token':'synthetic'}
selected={'name':'fixture.json','record':record,'fingerprint':accounts.fingerprint(record)}
row={'name':'fixture.json','provider':'codex','auth_index':'synthetic-index',
     'cooldowns':[{'scope':'credential','reason':'credential_quota'}]}
owner=type('Owner',(),{'state':{'pid':10,'port':12345},'key':'synthetic-management'})()
calls=[]
def request(url,**_options):
    if url.endswith('/credentials'):
        return {'files':[row]}
    if url==q.USAGE_URL:
        calls.append('usage')
        return {'rate_limit':{'allowed':True,'limit_reached':False,'primary_window':{'used_percent':1}}}
    if url.endswith('/routing/cooldown/reset'):
        calls.append('reset')
        return {'status':'ok'}
    raise AssertionError('Unexpected synthetic endpoint')
def active(candidate):
    assert os.path.samefile(candidate,root)
    return record
states=iter([{'fixture':True}]*3+[None])
clock={'value':0}
q.time=SimpleNamespace(monotonic=lambda:clock['value'],time=lambda:1000+clock['value'])
launcher.read_state=lambda _path:next(states)
launcher.state_is_live=lambda _path,_state:True
accounts.local_accounts=lambda _root:[selected]
accounts.Management=lambda _root:owner
accounts.request_json=request
codex_usage.active_bridge_auth=active
def wait(seconds):
    clock['value']+=seconds
assert q.watch(root,wait=wait)==0
assert calls==['usage','reset']
assert not (root/'unreviewed-used').exists()
print('snapshot-context-ok',flush=True)
'''
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.root),
                       "TMPDIR": str(self.root), "LANG": "C", "CLAUDE_CODEX_BRIDGE_HOME": str(self.root)}
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", program, str(self.root), str(snapshot)],
                                 cwd=self.root, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
        self.children.append(child)
        self.assertEqual(self.read_line(child), "snapshot-context-ok")
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertFalse((self.root / "unreviewed-used").exists())
        self.assertEqual(before, {path.name: path.read_bytes() for path in snapshot.iterdir()})
        self.assertTrue((self.root / "runtime-quota" / "probe-ledger.json").is_file())

    def test_candidate_handoff_waits_for_an_inflight_account_operation(self):
        old = self.start_synthetic_runtime()
        holder_code = r'''
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import accounts
with accounts.switch_lock(Path(sys.argv[2]),wait_seconds=5):
    print('in-flight',flush=True)
    sys.stdin.readline()
'''
        holder = self.start(holder_code, SOURCE, self.root)
        self.assertEqual(self.read_line(holder), "in-flight")
        entered = threading.Event()
        result = []
        successors = []
        spawn = quota_monitor_handoff._spawn_successor
        actual_switch_lock = accounts.switch_lock

        def capture(root, script):
            child = spawn(root, script)
            self.children.append(child)
            successors.append(child)
            return child

        def observed_lock(root, **options):
            entered.set()
            return actual_switch_lock(root, **options)

        def run():
            try:
                result.append(self.invoke_handoff())
            except Exception as error:
                result.append(error)

        worker = threading.Thread(target=run)
        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=capture), \
             patch.object(accounts, "switch_lock", side_effect=observed_lock):
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                time.sleep(.2)
                self.assertTrue(worker.is_alive())
                self.assertIsNone(old.poll())
            finally:
                holder.stdin.write(b"complete\n")
                holder.stdin.flush()
                holder.wait(timeout=5)
                worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [{"status": "replacement_reported", "monitor_protocol": 2}])
        self.assertEqual(old.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(len(successors), 1)
        successors[0].terminate()  # Only the test-owned replacement.
        successors[0].wait(timeout=5)

    def test_candidate_reports_unverified_and_requests_fallback_if_successor_exits(self):
        old = self.start_synthetic_runtime()
        program = r'''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import quota_recovery
root=Path(sys.argv[2])
print('ready',flush=True)
if sys.stdin.readline()!='go\n':
    raise SystemExit(0)
deadline=time.monotonic()+5
while time.monotonic()<deadline:
    if quota_recovery.monitor_lock_observation(root/'runtime-quota')=='no_conflict':
        raise SystemExit(0)
    time.sleep(.02)
raise SystemExit(1)
'''
        successors = []

        def failing_successor(_root, _script):
            child = self.start(program, SOURCE, self.root)
            successors.append(child)
            return child

        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=failing_successor), \
             patch.object(quota_recovery, "ensure_watch") as fallback:
            result = self.invoke_handoff()
        self.assertEqual(result, {"status": "replacement_unverified"})
        fallback.assert_called_once_with(self.root.absolute())
        self.assertEqual(old.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(successors[0].wait(timeout=5), 0)

    def test_candidate_preserves_old_watcher_if_generation_bound_signal_is_refused(self):
        old = self.start_synthetic_runtime()
        successors = []
        spawn = quota_monitor_handoff._spawn_successor

        def capture(root, script):
            child = spawn(root, script)
            self.children.append(child)
            successors.append(child)
            return child

        with patch.object(quota_monitor_handoff, "_spawn_successor", side_effect=capture), \
             patch.object(quota_monitor_handoff, "_signal", return_value=errno.EPERM):
            self.assertEqual(self.invoke_handoff(), {"status": "replacement_unverified"})
        self.assertIsNone(old.poll())
        self.assertEqual(len(successors), 1)
        successors[0].terminate()  # Only the test-owned waiting child.
        successors[0].wait(timeout=5)
        old.terminate()  # Only the test-owned monitor.
        old.wait(timeout=5)

    def test_successor_readiness_has_a_bounded_partial_line(self):
        child = self.start("import sys; sys.stdout.write('ready'); sys.stdout.flush(); sys.stdin.readline()")
        started = time.monotonic()
        self.assertFalse(quota_monitor_handoff._ready(child, timeout=.2))
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(child.poll())

    def test_fixture_successor_environment_excludes_unrelated_credentials_and_proxies(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SYNTHETIC_PRIVATE", "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_PRIVATE",
                                     "HTTP_PROXY": "http://synthetic.invalid"}), \
             patch.object(quota_monitor_handoff.subprocess, "Popen") as spawn:
            quota_monitor_handoff._spawn_successor(self.root, self.root / "synthetic-source.py")
        environment = spawn.call_args.kwargs["env"]
        self.assertEqual(environment["HOME"], str(self.root))
        self.assertEqual(environment["CODEX_HOME"], str(self.root / "codex-fixture"))
        for key in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HTTP_PROXY"):
            self.assertNotIn(key, environment)

    def test_query_drains_before_owned_monitor_is_signaled_and_successor_acquires_locks(self):
        old_code = r'''
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import accounts,quota_recovery
root=Path(sys.argv[2])
with quota_recovery.monitor_lock(root/'runtime-quota') as acquired:
    assert acquired
    with accounts.switch_lock(root):
        print('usage-in-flight',flush=True)
        sys.stdin.readline()
    print('usage-finished',flush=True)
    sys.stdin.readline()
'''
        next_code = r'''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import accounts,quota_recovery
root=Path(sys.argv[2])
print('successor-ready',flush=True)
deadline=time.monotonic()+10
while time.monotonic()<deadline:
    with quota_recovery.monitor_lock(root/'runtime-quota') as acquired:
        if acquired:
            print('monitor-acquired',flush=True)
            break
    time.sleep(.05)
else:
    raise RuntimeError('Synthetic monitor lock timeout')
with accounts.switch_lock(root,wait_seconds=5):
    print('account-acquired',flush=True)
states=iter([{'fixture':True},None])
quota_recovery.launcher.read_state=lambda _root:next(states)
quota_recovery.launcher.state_is_live=lambda _root,_state:True
quota_recovery.recover_once=lambda _root,attempts:{'status':'no_stale_credential_quota'}
assert quota_recovery.watch(root,wait=lambda _seconds:None)==0
print('watch-finished',flush=True)
'''
        old = self.start(old_code, SOURCE, self.root)
        self.assertEqual(self.read_line(old), "usage-in-flight")
        successor = self.start(next_code, SOURCE, self.root)
        self.assertEqual(self.read_line(successor), "successor-ready")
        sentinel = self.start("import sys; print('sentinel-ready',flush=True); sys.stdin.readline()")
        self.assertEqual(self.read_line(sentinel), "sentinel-ready")
        with self.assertRaises(RuntimeError):
            with accounts.switch_lock(self.root, wait_seconds=.1):
                self.fail("The synthetic usage request still owns the account lock")
        self.assertIsNone(old.poll())
        old.stdin.write(b"complete\n")
        old.stdin.flush()
        self.assertEqual(self.read_line(old), "usage-finished")
        with accounts.switch_lock(self.root, wait_seconds=5):
            version = self.identity(old)
            self.assertEqual(self.send(old, version ^ 1), errno.ESRCH)
            self.assertIsNone(old.poll())
            self.assertEqual(self.send(old, version), 0)
            self.assertEqual(old.wait(timeout=5), -signal.SIGTERM)
            self.assertEqual(self.read_line(successor), "monitor-acquired")
            self.assertIsNone(sentinel.poll())
        self.assertEqual(self.read_line(successor), "account-acquired")
        self.assertEqual(self.read_line(successor), "watch-finished")
        self.assertEqual(successor.wait(timeout=5), 0)
        self.assertIsNone(sentinel.poll())
        status = json.loads((self.root / "runtime-quota" / "status.json").read_text())
        self.assertEqual((status["status"], status["monitor_protocol"]), ("no_stale_credential_quota", 2))
        self.assertTrue((self.root / "runtime-quota" / "probe-ledger.json").is_file())

    def test_exec_changes_generation_and_old_audit_token_cannot_signal_new_image(self):
        code = r'''
import os,sys
print('ready-'+sys.argv[1],flush=True)
sys.stdin.readline()
if sys.argv[1]=='before':
    os.execv(sys.executable,[sys.executable,'-I','-c',sys.argv[2],'after',sys.argv[2]])
sys.stdin.readline()
'''
        child = self.start(code, "before", code)
        self.assertEqual(self.read_line(child), "ready-before")
        previous = self.identity(child)
        child.stdin.write(b"exec\n")
        child.stdin.flush()
        self.assertEqual(self.read_line(child), "ready-after")
        current = self.identity(child)
        self.assertNotEqual(previous, current)
        self.assertEqual(self.send(child, previous), errno.ESRCH)
        self.assertIsNone(child.poll())
        self.assertEqual(self.send(child, current), 0)
        self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)


@unittest.skipIf(sys.platform == "darwin", "Exercises the unsupported-platform refusal")
class UnsupportedMonitorIdentityTest(unittest.TestCase):
    def test_refuses_without_creating_state(self):
        with tempfile.TemporaryDirectory(prefix="quota-identity-unsupported-") as directory:
            root = Path(directory)
            with self.assertRaises(quota_monitor_identity.MonitorEvidenceUnavailable):
                quota_monitor_identity.observe(root)
            with self.assertRaises(quota_monitor_handoff.HandoffUnavailable):
                quota_monitor_handoff.handoff(root, expected_source_sha256="a" * 64,
                                              fixture_source=root, expected_manifest={})
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
