"""Read-only macOS monitor evidence; this module never sends a signal.

These observations are time-limited, do not establish exclusive lock
ownership, and are not by themselves a safe process-handoff authorization.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import time

import process_identity
import quota_recovery


class MonitorEvidenceUnavailable(RuntimeError):
    """The evidence cannot safely identify the reported monitor."""


@dataclass(frozen=True, repr=False)
class MonitorEvidence:
    # Internal metadata only: callers must not print this object.
    pid: int
    birth: bytes
    unique: bytes
    generation: int
    lock_device: int
    lock_inode: int


def _require(condition: bool) -> None:
    if not condition:
        raise MonitorEvidenceUnavailable("Monitor evidence is incomplete")


def _snapshot(pid: int) -> tuple[bytes, bytes, int]:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    data = ctypes.create_string_buffer(192)
    _require(library.proc_pidinfo(pid, 18, 0, data, len(data)) == len(data))
    _require(int.from_bytes(data.raw[12:16], sys.byteorder) == pid)
    _require(int.from_bytes(data.raw[20:24], sys.byteorder) == os.geteuid())
    birth, unique = data.raw[120:136], data.raw[152:168]
    generation = int.from_bytes(data.raw[168:172], sys.byteorder)
    _require(bool(any(birth)) and bool(any(unique)))
    return birth, unique, generation


def observe(root: Path) -> MonitorEvidence:
    """Return limited, non-atomic evidence; no network, locks or writes occur."""
    if sys.platform != "darwin":
        raise MonitorEvidenceUnavailable("Monitor evidence is unsupported on this platform")
    try:
        root = Path(root).absolute()
        directory = root / "runtime-quota"
        for path in (root, directory):
            details = path.lstat()
            _require(stat.S_ISDIR(details.st_mode) and details.st_uid == os.geteuid() and not details.st_mode & 0o077)
        status = json.loads(quota_recovery._private_text(directory / "status.json", 4096),
                            object_pairs_hook=quota_recovery._unique_json_pairs)
        _require(isinstance(status, dict))
        pid = status.get("monitor_pid")
        _require(type(pid) is int and 1 < pid < 2**31 and pid != os.getpid())
        checked = quota_recovery._probe_time(status.get("checked_at"))
        _require(0 <= time.time() - checked <= 90)
        lock = directory / "monitor.lock"
        metadata = lock.lstat()
        _require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid() and not metadata.st_mode & 0o077)
        script = root / "quota_recovery.py"
        script_metadata = script.lstat()
        _require(stat.S_ISREG(script_metadata.st_mode) and script_metadata.st_uid == os.geteuid()
                 and not script_metadata.st_mode & 0o077)
        _require(quota_recovery.monitor_lock_observation(directory) == "conflict")
        first = _snapshot(pid)
        birth, executable = process_identity.capture(pid)
        _require(birth == first[0].hex())
        own_executable = process_identity.capture(os.getpid())[1]
        _require(os.path.samefile(executable, own_executable))
        environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"}
        command = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            env=environment, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        arguments = shlex.split(command)
        _require(len(arguments) == 6 and arguments[1] == "-I" and arguments[3:5] == ["watch", "--root"])
        _require(os.path.samefile(arguments[0], executable))
        _require(os.path.samefile(arguments[2], script) and os.path.samefile(arguments[5], root))
        output = subprocess.run(["/usr/sbin/lsof", "-nP", "-Fp", "--", str(lock)],
            env=environment, capture_output=True, text=True, timeout=5, check=True).stdout.splitlines()
        openers = {int(line[1:]) for line in output if line.startswith("p")}
        _require(openers == {pid})
        after = lock.lstat()
        _require((metadata.st_dev, metadata.st_ino) == (after.st_dev, after.st_ino))
        _require(quota_recovery.monitor_lock_observation(directory) == "conflict" and _snapshot(pid) == first)
        return MonitorEvidence(pid, first[0], first[1], first[2], metadata.st_dev, metadata.st_ino)
    except MonitorEvidenceUnavailable:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        raise MonitorEvidenceUnavailable("Monitor evidence is unavailable") from None
