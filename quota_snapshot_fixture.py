"""Fixture-only source-snapshot publisher; not a production installer.

The publisher does not establish an OS sandbox, power-loss durability, or
protection against another process running as the same user.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time

import quota_monitor_handoff
import runtime_bundle


FILES = ("quota_recovery.py", "accounts.py", "api_profile.py", "codex_usage.py",
         "launcher.py", "runtime_bundle.py", "process_identity.py")
LIMIT = 2 * 1024 * 1024


class SnapshotUnavailable(RuntimeError):
    """A synthetic source snapshot cannot be safely created or verified."""


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise SnapshotUnavailable("Snapshot source is not a regular file")
        content = handle.read(LIMIT + 1)
    if len(content) > LIMIT:
        raise SnapshotUnavailable("Snapshot source exceeds its bound")
    return content


def _manifest(expected: dict) -> tuple[bytes, str]:
    if (not isinstance(expected, dict) or set(expected) != set(FILES)
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                   for value in expected.values())):
        raise SnapshotUnavailable("Snapshot manifest is invalid")
    content = runtime_bundle.canonical({"version": 1, "files": expected})
    return content, hashlib.sha256(content).hexdigest()


def _publish_exclusive(stage: Path, final: Path) -> None:
    """On Darwin, publish atomically without replacing an existing entry."""
    library = ctypes.CDLL(None, use_errno=True)
    function = library.renamex_np
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(os.fsencode(stage), os.fsencode(final), 0x00000004) != 0:  # RENAME_EXCL
        code = ctypes.get_errno()
        raise OSError(code, "Exclusive snapshot publication failed")


def verify_fixture(root: Path, snapshot: Path, expected: dict) -> None:
    """Verify one private fixture snapshot without modifying it."""
    if sys.platform != "darwin":
        raise SnapshotUnavailable("Snapshot experiment is unsupported on this platform")
    try:
        quota_monitor_handoff._fixture_root(root)
        content, digest = _manifest(expected)
        runtime = root / "runtime-quota"
        parent = runtime / "source-snapshots"
        for path in (runtime, parent):
            details = path.lstat()
            if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid()
                    or details.st_mode & 0o777 != 0o700):
                raise SnapshotUnavailable("Snapshot parent is unsafe")
        if snapshot != parent / digest or parent.is_symlink() or snapshot.is_symlink():
            raise SnapshotUnavailable("Snapshot location is invalid")
        info = snapshot.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o777 != 0o500:
            raise SnapshotUnavailable("Snapshot directory is unsafe")
        if {entry.name for entry in snapshot.iterdir()} != set(FILES) | {"manifest.json"}:
            raise SnapshotUnavailable("Snapshot file set is invalid")
        for name in (*FILES, "manifest.json"):
            path = snapshot / name
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid() or details.st_mode & 0o777 != 0o400:
                raise SnapshotUnavailable("Snapshot file is unsafe")
            value = _read(path)
            if name == "manifest.json":
                if value != content:
                    raise SnapshotUnavailable("Snapshot manifest changed")
            elif hashlib.sha256(value).hexdigest() != expected[name]:
                raise SnapshotUnavailable("Snapshot source changed")
    except SnapshotUnavailable:
        raise
    except (OSError, RuntimeError, ValueError, AttributeError):
        raise SnapshotUnavailable("Snapshot is unavailable") from None


def _verify_or_wait(root: Path, final: Path, expected: dict) -> None:
    """Allow a cooperating publisher a short time to seal its new directory."""
    deadline = time.monotonic() + 2
    while True:
        try:
            verify_fixture(root, final, expected)
            return
        except SnapshotUnavailable:
            try:
                info = final.lstat()
                pending = (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                           and info.st_mode & 0o777 == 0o700)
            except OSError:
                pending = False
            if not pending or time.monotonic() >= deadline:
                raise
            time.sleep(.02)


def _seal_published(final: Path, device: int, inode: int) -> None:
    """Seal only the directory that was just published, never a replacement."""
    descriptor = os.open(final, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or (info.st_dev, info.st_ino) != (device, inode)):
            raise SnapshotUnavailable("Published snapshot identity changed")
        os.fchmod(descriptor, 0o500)
    finally:
        os.close(descriptor)


def publish_fixture(root: Path, source: Path, expected: dict) -> Path:
    """Atomically publish reviewed source into an owned temporary fixture."""
    if sys.platform != "darwin":
        raise SnapshotUnavailable("Snapshot experiment is unsupported on this platform")
    stage = None
    try:
        root, source = Path(root).absolute(), Path(source).absolute()
        quota_monitor_handoff._fixture_root(root)
        content, digest = _manifest(expected)
        values = {}
        for name in FILES:
            data = _read(source / name)
            if hashlib.sha256(data).hexdigest() != expected[name]:
                raise SnapshotUnavailable("Reviewed source does not match the manifest")
            values[name] = data
        runtime_bundle.private_dir(root / "runtime-quota")
        parent = root / "runtime-quota" / "source-snapshots"
        runtime_bundle.private_dir(parent)
        final = parent / digest
        if final.exists() or final.is_symlink():
            _verify_or_wait(root, final, expected)
            return final
        stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
        for name, data in values.items():
            runtime_bundle.write_file(stage / name, data, 0o400)
        runtime_bundle.write_file(stage / "manifest.json", content, 0o400)
        stage_info = stage.lstat()
        try:
            _publish_exclusive(stage, final)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
        else:
            stage = None
            _seal_published(final, stage_info.st_dev, stage_info.st_ino)
        _verify_or_wait(root, final, expected)
        return final
    except SnapshotUnavailable:
        raise
    except (OSError, RuntimeError, ValueError, AttributeError):
        raise SnapshotUnavailable("Snapshot publication is unavailable") from None
    finally:
        if stage is not None:
            try:
                if stage.is_symlink():
                    stage.unlink()
                elif stage.exists():
                    stage.chmod(0o700)
                    shutil.rmtree(stage)
            except OSError:
                raise SnapshotUnavailable("Snapshot staging cleanup failed") from None
