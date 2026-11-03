"""Explore identity-bound handoff with owned synthetic processes only.

This is not a production handoff: matching a real monitor to its lock, safely
starting a successor, and recovery from handoff failures need separate work.
"""

import ctypes
import errno
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import accounts
import process_identity
import quota_monitor_identity
import quota_recovery
import runtime_bundle


SOURCE = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform == "darwin", "Requires macOS audit-token signaling")
class OwnedDarwinHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="quota-handoff-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.children = []
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
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
