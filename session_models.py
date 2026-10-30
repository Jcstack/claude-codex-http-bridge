"""Restore model selection using local metadata, never conversation text."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import time

import api_profile


UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\Z")
MODEL = re.compile(r"gpt-[a-z0-9.-]{1,160}\Z")
TAIL_BYTES = 4 * 1024 * 1024
LINE_BYTES = 2 * 1024 * 1024


def config_roots() -> list[Path]:
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return [Path(p).expanduser().absolute() for p in value.split(os.pathsep) if p] if value else [Path.home() / ".claude"]


def path_tag(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.expanduser().resolve())).encode()).hexdigest()


def ledger_directory(root: Path, config: Path, route: str) -> Path:
    if route not in ("subscription", "api"):
        raise ValueError("Invalid model history route")
    return root / "runtime-session-models" / (route + "-" + path_tag(config)[:24])


def record(root: Path, payload: dict, models: dict, route: str) -> None:
    session = payload.get("session_id")
    model = payload.get("model")
    model = model.get("id") if isinstance(model, dict) else None
    workspace = payload.get("workspace")
    workspace = workspace if isinstance(workspace, dict) else {}
    cwd = payload.get("cwd") or workspace.get("current_dir") or workspace.get("project_dir")
    if (not isinstance(session, str) or not UUID.fullmatch(session) or not isinstance(model, str)
            or model not in models or not MODEL.fullmatch(model) or not isinstance(cwd, str) or not cwd):
        return
    config = config_roots()[0]
    directory = ledger_directory(root, config, route)
    path = directory / (session.lower() + ".json")
    value = {"model": model, "cwd_tag": path_tag(Path(cwd)), "observed_at": time.time()}
    # An unchanged observation writes nothing. This fast path does not expose
    # cached values and avoids subprocess-based NTFS ACL work on every redraw.
    try:
        if not any(p.is_symlink() for p in (directory.parent, directory, path)) and path.is_file() and path.stat().st_size <= 4096:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and previous.get("model") == model and previous.get("cwd_tag") == value["cwd_tag"]:
                return
    except (OSError, ValueError):
        pass
    for parent in (directory.parent, directory):
        if parent.is_symlink():
            return
        parent.mkdir(mode=0o700, exist_ok=True)
        if platform.system() == "Windows":
            import windows_acl
            windows_acl.lock([parent])
        else:
            parent.chmod(0o700)
    api_profile.write_private(path, json.dumps(value) + "\n")


def read_ledger(root: Path, config: Path, session: str, route: str) -> dict | None:
    if not UUID.fullmatch(session):
        return None
    directory = ledger_directory(root, config, route)
    path = directory / (session.lower() + ".json")
    try:
        if any(p.is_symlink() for p in (directory.parent, directory, path)) or not path.is_file():
            return None
        if path.stat().st_size > 4096:
            return None
        if platform.system() == "Windows":
            import windows_acl
            windows_acl.verify([directory.parent, directory, path], protected=True)
        elif any(p.stat().st_mode & 0o077 for p in (directory.parent, directory, path)):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not MODEL.fullmatch(str(value.get("model", ""))):
            return None
        value["observed_at"] = path.stat().st_mtime
        return value
    except (OSError, ValueError, RuntimeError):
        return None


def transcript_metadata(path: Path) -> dict:
    """Bounded tail, explicit metadata fields only; no user/tool text escapes."""
    result = {}
    try:
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            return result
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - TAIL_BYTES)
            handle.seek(start)
            if start:
                handle.readline(LINE_BYTES + 1)  # discard a potentially partial first record
            while True:
                line = handle.readline(LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > LINE_BYTES:
                    while line and not line.endswith(b"\n"):
                        line = handle.readline(LINE_BYTES + 1)
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(row, dict) or row.get("isSidechain"):
                    continue
                if isinstance(row.get("cwd"), str):
                    try:
                        result["cwd_tag"] = path_tag(Path(row["cwd"]))
                    except (OSError, ValueError):
                        pass
                if row.get("type") == "custom-title" and isinstance(row.get("customTitle"), str):
                    result["title"] = row["customTitle"]
                message = row.get("message")
                model = message.get("model") if isinstance(message, dict) else None
                if row.get("type") == "assistant" and isinstance(model, str) and MODEL.fullmatch(model):
                    result["model"] = model
                    stamp = row.get("timestamp")
                    try:
                        result["model_at"] = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                    except (AttributeError, ValueError):
                        result["model_at"] = path.stat().st_mtime
        return result
    except OSError:
        return {}


def resume_selector(arguments: list[str]) -> tuple[str | None, str | None]:
    for index, argument in enumerate(arguments):
        if argument == "--":
            break
        if argument in ("--continue", "-c"):
            return "continue", None
        if argument.startswith("--resume="):
            return "resume", argument.partition("=")[2] or None
        if argument in ("--resume", "-r"):
            following = arguments[index + 1] if index + 1 < len(arguments) else None
            return "resume", following if following and not following.startswith("-") else None
    return None, None


def restore(root: Path, arguments: list[str], cwd: Path, route: str) -> tuple[bool, str | None]:
    mode, selector = resume_selector(arguments)
    if mode is None:
        return False, None
    if mode == "resume" and selector is None:
        return True, None  # Leave the native picker free to choose its session/model.
    expected = path_tag(cwd)
    candidates = []
    for config in config_roots():
        projects = config / "projects"
        if config.is_symlink() or projects.is_symlink() or not projects.is_dir():
            continue
        project_key = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))
        local = projects / project_key
        if mode == "continue" and local.is_dir() and not local.is_symlink():
            paths = list(local.glob("*.jsonl"))
        else:
            pattern = f"*/{selector.lower()}.jsonl" if selector and UUID.fullmatch(selector) else "*/*.jsonl"
            paths = list(projects.glob(pattern))
        paths = [p for p in paths if UUID.fullmatch(p.stem) and
                 p.is_file() and not p.is_symlink() and not p.parent.is_symlink()]
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in paths[:512]:
            info = transcript_metadata(path)
            if mode == "continue" and info.get("cwd_tag") != expected:
                continue
            if mode == "resume" and selector and not UUID.fullmatch(selector) and info.get("title") != selector:
                continue
            saved = read_ledger(root, config, path.stem, route)
            model = info.get("model")
            if saved and saved.get("observed_at", 0) >= info.get("model_at", 0):
                model = saved["model"]
            candidates.append((path.stat().st_mtime, model))
            if mode == "continue":
                break
    if not candidates:
        return True, None
    if mode == "resume" and len(candidates) != 1:
        return True, None  # Never guess when a name/UUID is ambiguous across roots.
    return True, max(candidates, key=lambda item: item[0])[1]
