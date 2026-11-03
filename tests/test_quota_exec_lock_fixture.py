"""Observe lock inheritance across exec with temporary owned processes only.

This does not modify or start the installed quota monitor. Windows uses a
different locking mechanism and is deliberately not covered by this fixture.
"""

import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest


BEFORE_EXEC = r'''
import fcntl,os,sys
path,mode,replacement=sys.argv[1:]
fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
os.set_inheritable(fd,mode=="retain")
print("old-ready",flush=True)
if sys.stdin.readline()!="exec\n": raise SystemExit(2)
os.execve(sys.executable,[sys.executable,"-I","-S","-B","-c",replacement,
                         str(fd),mode,path,str(os.getpid())],dict(os.environ))
'''

AFTER_EXEC = r'''
import os,sys
fd,mode,path,original_pid=sys.argv[1:]
fd=int(fd)
if os.getpid()!=int(original_pid): raise SystemExit(3)
try:
    actual=os.fstat(fd);expected=os.stat(path)
    same=(actual.st_dev,actual.st_ino)==(expected.st_dev,expected.st_ino)
except OSError:
    same=False
if same!=(mode=="retain"): raise SystemExit(4)
print("new-retained" if same else "new-closed",flush=True)
if sys.stdin.readline()!="finish\n": raise SystemExit(5)
if same: os.close(fd)
'''

FAIL_EXEC = r'''
import fcntl,os,sys
lock,missing=sys.argv[1:]
fd=os.open(lock,os.O_CREAT|os.O_RDWR,0o600)
fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
os.set_inheritable(fd,True)
print("old-ready",flush=True)
if sys.stdin.readline()!="exec\n": raise SystemExit(2)
try:
    os.execve(missing,[missing],dict(os.environ))
except FileNotFoundError:
    print("exec-failed",flush=True)
else:
    raise SystemExit(3)
if sys.stdin.readline()!="finish\n": raise SystemExit(4)
os.close(fd)
'''

AFTER_CRASH = r'''
import os,sys
fd,mode,path,original_pid=sys.argv[1:]
fd=int(fd);actual=os.fstat(fd);expected=os.stat(path)
if mode!="retain" or os.getpid()!=int(original_pid) or (actual.st_dev,actual.st_ino)!=(expected.st_dev,expected.st_ino):
    raise SystemExit(3)
print("new-ready",flush=True)
if sys.stdin.readline()!="crash\n": raise SystemExit(4)
os._exit(23)
'''

CONTENDER = r'''
import fcntl,os,select,sys
fd=os.open(sys.argv[1],os.O_RDWR)
attempts=0
while True:
    attempts+=1
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        if attempts==1: print("contender-ready",flush=True)
    else:
        print("acquired",flush=True)
        fcntl.flock(fd,fcntl.LOCK_UN)
        break
    readable,_,_=select.select([sys.stdin],[],[],.002)
    if readable:
        if sys.stdin.readline()!="stop\n": raise SystemExit(6)
        print("blocked "+str(attempts),flush=True)
        break
os.close(fd)
'''


@unittest.skipUnless(os.name == "posix", "POSIX flock and exec fixture")
class OwnedExecLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="quota-exec-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()  # Only processes created by this fixture.
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def start(self, program, *args):
        environment = {"PATH": os.defpath, "HOME": str(self.root), "TMPDIR": str(self.root), "LANG": "C"}
        child = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", program, *map(str, args)],
                                 cwd=self.root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
        self.children.append(child)
        return child

    def line(self, child):
        ready, _, _ = select.select([child.stdout], [], [], 10)
        self.assertTrue(ready, "Synthetic child did not report in time")
        value = child.stdout.readline()
        self.assertTrue(value, "Synthetic child exited before reporting")
        return value.decode("ascii").strip()

    def observe_exec(self, mode):
        lock = self.root / "monitor.lock"
        owner = self.start(BEFORE_EXEC, lock, mode, AFTER_EXEC)
        self.assertEqual(self.line(owner), "old-ready")
        contender = self.start(CONTENDER, lock)
        self.assertEqual(self.line(contender), "contender-ready")
        owner.stdin.write(b"exec\n")
        owner.stdin.flush()
        self.assertEqual(self.line(owner), "new-retained" if mode == "retain" else "new-closed")
        if mode == "retain":
            # The competitor keeps trying across the transition, not merely at
            # one instant before and after it.
            time.sleep(.1)
            contender.stdin.write(b"stop\n")
            contender.stdin.flush()
            observed = self.line(contender).split()
            self.assertEqual(observed[0], "blocked")
            self.assertGreater(int(observed[1]), 1)
        else:
            # Closing the descriptor at exec must be detected by the same
            # contender while the replacement process is still alive.
            self.assertEqual(self.line(contender), "acquired")
            self.assertIsNone(owner.poll())
        self.assertEqual(contender.wait(timeout=5), 0)
        owner.stdin.write(b"finish\n")
        owner.stdin.flush()
        self.assertEqual(owner.wait(timeout=5), 0)

    def test_monitor_lock_remains_contended_across_owned_exec(self):
        self.observe_exec("retain")

    def test_cloexec_control_releases_lock_while_replacement_is_alive(self):
        self.observe_exec("close")

    def test_failed_exec_leaves_old_owned_process_holding_the_lock(self):
        lock = self.root / "monitor.lock"
        owner = self.start(FAIL_EXEC, lock, self.root / "nonexistent-executable")
        self.assertEqual(self.line(owner), "old-ready")
        contender = self.start(CONTENDER, lock)
        self.assertEqual(self.line(contender), "contender-ready")
        owner.stdin.write(b"exec\n")
        owner.stdin.flush()
        self.assertEqual(self.line(owner), "exec-failed")
        self.assertIsNone(owner.poll())
        contender.stdin.write(b"stop\n")
        contender.stdin.flush()
        self.assertEqual(self.line(contender).split()[0], "blocked")
        self.assertEqual(contender.wait(timeout=5), 0)
        owner.stdin.write(b"finish\n")
        owner.stdin.flush()
        self.assertEqual(owner.wait(timeout=5), 0)

    def test_crash_after_exec_releases_the_lock(self):
        lock = self.root / "monitor.lock"
        owner = self.start(BEFORE_EXEC, lock, "retain", AFTER_CRASH)
        self.assertEqual(self.line(owner), "old-ready")
        contender = self.start(CONTENDER, lock)
        self.assertEqual(self.line(contender), "contender-ready")
        owner.stdin.write(b"exec\n")
        owner.stdin.flush()
        self.assertEqual(self.line(owner), "new-ready")
        owner.stdin.write(b"crash\n")
        owner.stdin.flush()
        self.assertEqual(owner.wait(timeout=5), 23)
        self.assertEqual(self.line(contender), "acquired")
        self.assertEqual(contender.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
