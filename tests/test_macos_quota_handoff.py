"""Explore identity-bound handoff with owned synthetic processes only.

This is not a production handoff: matching a real monitor to its lock, safely
starting a successor, and recovery from handoff failures need separate work.
"""

import ctypes
import errno
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import accounts
import process_identity


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
            with accounts.switch_lock(root,wait_seconds=5):
                print('account-acquired',flush=True)
            sys.stdin.readline()
            break
    time.sleep(.05)
else:
    raise RuntimeError('Synthetic monitor lock timeout')
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
        successor.stdin.write(b"done\n")
        successor.stdin.flush()
        self.assertEqual(successor.wait(timeout=5), 0)
        self.assertIsNone(sentinel.poll())

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


if __name__ == "__main__":
    unittest.main()
