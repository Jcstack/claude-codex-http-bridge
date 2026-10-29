"""Lock Windows runtime data to the current user with native NTFS ACLs."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile


SID_PATTERN = re.compile(r"S-1-\d+(?:-\d+)+")


def current_user_sid() -> str:
    try:
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                capture_output=True, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Could not identify the current Windows user") from error
    match = SID_PATTERN.search(result.stdout.decode("utf-8", errors="replace"))
    if not match:
        raise RuntimeError("Could not read the current Windows user SID")
    return match.group()


def run_icacls(*arguments: str) -> None:
    try:
        subprocess.run(["icacls", *arguments], capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        detail = ""
        if os.environ.get("CLAUDE_CODEX_DEBUG_ACL") == "1" and isinstance(error, subprocess.CalledProcessError):
            detail = ": " + (error.stderr or b"").decode(errors="replace")[-500:]
        raise RuntimeError("Could not apply or verify private Windows ACLs (NTFS required)" + detail) from error


def read_sddl(path: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="claude-codex-acl-") as directory:
        snapshot = Path(directory) / "acl.txt"
        run_icacls(str(path), "/save", str(snapshot))
        data = snapshot.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        content = data.decode("utf-16")
    elif b"\x00" in data[:20]:
        content = data.decode("utf-16-le")
    else:
        content = data.decode("utf-8-sig", errors="replace")
    for line in content.splitlines():
        match = re.search(r"D:[A-Z]*\(", line)
        if match:
            return line[match.start():].split("S:", 1)[0]
    raise RuntimeError("Windows ACL snapshot did not contain a DACL")


def verify(paths: list[Path], protected: bool = False) -> None:
    if not paths:
        return
    sid = current_user_sid()
    for path in paths:
        if path.is_symlink() or not path.exists():
            raise RuntimeError("Windows runtime path is missing or symlinked")
        dacl = read_sddl(path)
        flags = dacl.split("(", 1)[0]
        if protected and "P" not in flags:
            raise RuntimeError("Windows runtime ACL still inherits permissions")
        entries = re.findall(r"\(([^()]*)\)", dacl)
        if not entries:
            raise RuntimeError("Windows runtime ACL has no access rules")
        for entry in entries:
            fields = entry.split(";")
            if len(fields) != 6 or fields[0] != "A" or fields[-1] != sid:
                raise RuntimeError("Windows runtime ACL grants another identity access")


def lock(paths: list[Path]) -> None:
    if not paths:
        return
    sid = current_user_sid()
    for path in paths:
        if path.is_symlink() or not path.exists():
            raise RuntimeError("Refusing missing or symlinked Windows runtime path")
        grant = f"*{sid}:(OI)(CI)F" if path.is_dir() else f"*{sid}:F"
        run_icacls(str(path), "/reset")
        run_icacls(str(path), "/grant:r", grant)
        run_icacls(str(path), "/inheritance:r")
    verify(paths, protected=True)
