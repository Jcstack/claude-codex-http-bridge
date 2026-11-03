"""Fixture-only macOS quota-monitor handoff; never targets a real service.

An ABA source-swap counterexample makes this candidate unsafe for live use.
Only a marked, credential-free temporary root and a direct child are allowed.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
import re

import accounts
import quota_monitor_identity
import quota_recovery


class HandoffUnavailable(RuntimeError):
    """A monitor handoff cannot be established safely."""


def _fixture_root(root: Path) -> None:
    base = Path(tempfile.gettempdir()).resolve()
    resolved = root.resolve()
    if root.is_symlink() or resolved == base or not resolved.is_relative_to(base):
        raise HandoffUnavailable("Monitor handoff is limited to an isolated fixture")
    try:
        info = root.lstat()
        marker_info = (root / ".quota-handoff-fixture").lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != os.geteuid()
                or (root / "auth").exists() or (root / "auth").is_symlink()):
            raise HandoffUnavailable("Monitor handoff is limited to an isolated fixture")
        marker = quota_recovery._private_text(root / ".quota-handoff-fixture", 128)
    except (OSError, RuntimeError, ValueError):
        raise HandoffUnavailable("Monitor handoff is limited to an isolated fixture") from None
    if marker != "synthetic quota fixture\n":
        raise HandoffUnavailable("Monitor handoff is limited to an isolated fixture")


def _fixture_child(evidence: quota_monitor_identity.MonitorEvidence) -> None:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    data = ctypes.create_string_buffer(192)
    if (library.proc_pidinfo(evidence.pid, 18, 0, data, len(data)) != len(data)
            or int.from_bytes(data.raw[12:16], sys.byteorder) != evidence.pid
            or int.from_bytes(data.raw[16:20], sys.byteorder) != os.getpid()
            or data.raw[120:136] != evidence.birth or data.raw[152:168] != evidence.unique
            or int.from_bytes(data.raw[168:172], sys.byteorder) != evidence.generation):
        raise HandoffUnavailable("Monitor handoff requires an owned fixture child")


def _source_bytes(path: Path) -> bytes:
    """Read a bounded regular source file without following a final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise HandoffUnavailable("Replacement monitor source is unavailable")
        content = handle.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise HandoffUnavailable("Replacement monitor source is unavailable")
    return content


def _spawn_successor(root: Path) -> subprocess.Popen:
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root), "TMPDIR": str(root),
                   "LANG": "C", "CODEX_HOME": str(root / "codex-fixture"),
                   "XDG_CONFIG_HOME": str(root / "xdg-fixture")}
    return subprocess.Popen([sys.executable, "-I", str(root / "quota_recovery.py"), "watch-wait", "--root", str(root)], cwd=root,
        env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, bufsize=0)


def _ready(child: subprocess.Popen, *, timeout: float = 8) -> bool:
    if child.stdout is None or child.stdin is None:
        return False
    deadline = time.monotonic() + timeout
    received = bytearray()
    while len(received) < 16:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        readable, _, _ = select.select([child.stdout], [], [], remaining)
        if not readable:
            return False
        chunk = os.read(child.stdout.fileno(), 16 - len(received))
        if not chunk:
            return False
        received.extend(chunk)
        if b"\n" in received:
            return received == b"ready\n" and child.poll() is None
    return False


def _signal(evidence: quota_monitor_identity.MonitorEvidence) -> int:
    """Signal only the generation the kernel finds for this audit token."""
    _fixture_child(evidence)
    current = quota_monitor_identity._snapshot(evidence.pid)
    if current != (evidence.birth, evidence.unique, evidence.generation):
        raise HandoffUnavailable("Monitor generation changed")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    native = library.proc_signal_with_audittoken
    native.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
    native.restype = ctypes.c_int
    token = (ctypes.c_uint32 * 8)()
    token[5], token[7] = evidence.pid, evidence.generation
    return native(token, signal.SIGTERM)


def _confirmed(root: Path, pid: int, *, after: float) -> bool:
    try:
        status = json.loads(quota_recovery._private_text(root / "runtime-quota" / "status.json", 4096),
                            object_pairs_hook=quota_recovery._unique_json_pairs)
        view = quota_recovery.monitor_status(root)
        return (isinstance(status, dict) and status.get("monitor_pid") == pid
                and type(status.get("monitor_protocol")) is int
                and status["monitor_protocol"] == quota_recovery.MONITOR_PROTOCOL
                and after <= quota_recovery._probe_time(status.get("checked_at")) <= time.time() + 1
                and view["probe_ledger"] == "valid" and view["lock_observation"] == "conflict")
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


def handoff(root: Path, *, expected_source_sha256: str) -> dict:
    """Attempt a bounded transition; no raw process or account metadata is returned.

    This source candidate only accepts owned temporary fixtures, not a live root.
    """
    if sys.platform != "darwin":
        raise HandoffUnavailable("Monitor handoff is unsupported on this platform")
    if not isinstance(expected_source_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_source_sha256):
        raise HandoffUnavailable("A reviewed replacement source hash is required")
    root = Path(root).absolute()
    _fixture_root(root)
    child = None
    armed = False
    started_at = time.time()
    try:
        # The legacy monitor holds this same lock throughout a usage read/reset.
        with accounts.switch_lock(root, wait_seconds=15):
            first = quota_monitor_identity.observe(root)
            _fixture_child(first)
            installed = root / "quota_recovery.py"
            reviewed = Path(quota_recovery.__file__).resolve()
            installed_hash = hashlib.sha256(_source_bytes(installed)).hexdigest()
            reviewed_hash = hashlib.sha256(_source_bytes(reviewed)).hexdigest()
            if (not hmac.compare_digest(installed_hash, expected_source_sha256)
                    or not hmac.compare_digest(reviewed_hash, expected_source_sha256)):
                raise HandoffUnavailable("Replacement monitor source differs from the reviewed source")
            child = _spawn_successor(root)
            if not _ready(child):
                raise HandoffUnavailable("Replacement monitor did not become ready")
            second = quota_monitor_identity.observe(root)
            if second != first:
                raise HandoffUnavailable("Monitor evidence changed")
            child.stdin.write(b"go\n")
            child.stdin.flush()
            child.stdin.close()
            armed = True
            if child.poll() is not None:
                raise HandoffUnavailable("Replacement monitor exited before handoff")
            result = _signal(first)
            if result not in (0, errno.ESRCH):
                raise HandoffUnavailable("Generation-bound monitor signal was refused")
    except HandoffUnavailable:
        if armed:
            return {"status": "replacement_unverified"}
        raise
    except (OSError, RuntimeError, ValueError, AttributeError, subprocess.SubprocessError):
        if armed:
            return {"status": "replacement_unverified"}
        raise HandoffUnavailable("Monitor handoff is unavailable") from None
    finally:
        if child is not None and child.stdout is not None:
            child.stdout.close()
        if child is not None and not armed and child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
        if child is not None and not armed:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    child.terminate()  # This is the unreaped child created above.
                    child.wait(timeout=2)
                except (OSError, subprocess.SubprocessError):
                    pass
            except (OSError, subprocess.SubprocessError):
                pass

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if child.poll() is None and _confirmed(root, child.pid, after=started_at) and child.poll() is None:
            return {"status": "replacement_reported", "monitor_protocol": quota_recovery.MONITOR_PROTOCOL}
        if child.poll() is not None:
            break
        time.sleep(.1)
    # A concurrent launcher may have claimed the lock or the child may have
    # failed. This best-effort path cannot itself prove a replacement is alive.
    try:
        quota_recovery.ensure_watch(root)
    except (OSError, RuntimeError, ValueError):
        pass
    return {"status": "replacement_unverified"}
