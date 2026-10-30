#!/usr/bin/env python3
"""Read only the selected bridge account's actual Codex quota windows."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import accounts


ROOT = Path(__file__).resolve().parent
CACHE_SECONDS = 300
RPC_TIMEOUT_SECONDS = 12
WINDOW_NAMES = {300: "five_hour", 10080: "seven_day"}


def _private_json(path: Path) -> dict | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if platform.system() != "Windows" and path.stat().st_mode & 0o077:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def active_bridge_auth(root: Path = ROOT) -> dict | None:
    """Disabled backups do not determine which account the bridge now uses."""
    auth_dir = root / "auth"
    try:
        if auth_dir.is_symlink() or not auth_dir.is_dir():
            return None
        if platform.system() != "Windows" and auth_dir.stat().st_mode & 0o077:
            return None
        files = list(auth_dir.glob("*.json"))
    except OSError:
        return None
    active = []
    for path in files:
        value = _private_json(path)
        if value is None:
            return None
        if value.get("type") not in (None, "codex") or value.get("disabled") is True:
            continue
        active.append(value)
    if len(active) != 1:
        return None  # Never label a mixed pool as one account's quota.
    return active[0] if isinstance(active[0].get("account_id"), str) else None


def selected_account_id(root: Path = ROOT) -> str | None:
    bridge = active_bridge_auth(root)
    return bridge.get("account_id") if bridge else None


def selected_fingerprint(root: Path = ROOT) -> str | None:
    bridge = active_bridge_auth(root)
    try:
        return accounts.identity_fingerprint(bridge) if bridge else None
    except RuntimeError:
        return None


def matching_account_id(root: Path = ROOT) -> str | None:
    """Native app-server quota is usable only for the same selected account."""
    bridge = active_bridge_auth(root)
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    native = _private_json(codex_home / "auth.json")
    if bridge is None or native is None:
        return None
    bridge_id = bridge.get("account_id")
    tokens = native.get("tokens")
    native_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    if (not isinstance(bridge_id, str) or not isinstance(native_id, str)
            or not bridge_id or not hmac.compare_digest(bridge_id, native_id)):
        return None
    if accounts.account_subject(bridge) != accounts.account_subject(tokens):
        return None
    return bridge_id


def _bridge_rate_limits(root: Path = ROOT) -> dict | None:
    """Read the selected account's fixed ChatGPT usage endpoint, without proxies."""
    bridge = active_bridge_auth(root)
    if bridge is None:
        return None
    token, account = bridge.get("access_token"), bridge.get("account_id")
    identity = accounts.identity_fingerprint(bridge)
    if (not isinstance(token, str) or not token or not isinstance(account, str) or not account
            or any(char in token + account for char in ("\r", "\n"))):
        return None
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise RuntimeError("Usage endpoint redirected unexpectedly")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request("https://chatgpt.com/backend-api/wham/usage", headers={
        "Authorization": "Bearer " + token, "Chatgpt-Account-Id": account,
        "User-Agent": "claude-codex-http-bridge"})
    try:
        with opener.open(request, timeout=RPC_TIMEOUT_SECONDS) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            return None
        value = json.loads(raw)
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        return None
    if not isinstance(value, dict) or selected_fingerprint(root) != identity:
        return None
    limit = value.get("rate_limit")
    if not isinstance(limit, dict):
        return None
    bucket = {}
    for source, target in (("primary_window", "primary"), ("secondary_window", "secondary")):
        window = limit.get(source)
        if not isinstance(window, dict):
            continue
        seconds = window.get("limit_window_seconds")
        if type(seconds) is not int or seconds % 60:
            continue
        bucket[target] = {"windowDurationMins": seconds // 60, "usedPercent": window.get("used_percent"),
                          "resetsAt": window.get("reset_at")}
    return {"accountId": account, "rateLimitsByLimitId": {"codex": bucket}}


def selected_rate_limits(root: Path = ROOT) -> dict | None:
    # The native CLI keeps its own account. Do not log it out or replace its auth
    # merely to read the independently selected HTTP bridge account's usage.
    if matching_account_id(root):
        return _rpc_rate_limits()
    return _bridge_rate_limits(root)


def _rpc_rate_limits() -> dict | None:
    codex = shutil.which("codex")
    if codex is None:
        return None
    try:
        proc = subprocess.Popen(
            [codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
    except OSError:
        return None
    messages: queue.Queue[dict | None] = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                messages.put(value)
        messages.put(None)

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + RPC_TIMEOUT_SECONDS

    def send(message: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def receive(request_id: int) -> dict | None:
        while time.monotonic() < deadline:
            try:
                message = messages.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if message is None:
                return None
            if message.get("id") == request_id:
                return message.get("result") if isinstance(message.get("result"), dict) else None
        return None

    try:
        send({"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "claude_codex_http_bridge", "title": "Claude Codex HTTP Bridge", "version": "0.1.0"},
        }})
        if receive(1) is None:
            return None
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 2})
        return receive(2)
    except (BrokenPipeError, OSError):
        return None
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def windows_from_response(result: dict) -> dict:
    """Map only windows whose durations actually match HUD's named periods."""
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and by_id:
        bucket = by_id.get("codex")
    else:
        bucket = result.get("rateLimits")
    if not isinstance(bucket, dict):
        return {}
    windows: dict[str, dict] = {}
    for slot in ("primary", "secondary"):
        row = bucket.get(slot)
        if not isinstance(row, dict):
            continue
        duration = row.get("windowDurationMins")
        name = WINDOW_NAMES.get(duration) if type(duration) is int else None
        percent = row.get("usedPercent")
        resets = row.get("resetsAt")
        if (name is None or type(percent) not in (int, float) or
                not 0 <= percent <= 100 or type(resets) is not int or resets <= 0):
            continue
        windows[name] = {"used_percentage": percent, "resets_at": resets}
    return windows


def account_fingerprint(account_id: str) -> str:
    return hashlib.sha256(account_id.encode()).hexdigest()


def read_cache(path: Path, root: Path = ROOT) -> dict | None:
    identity = selected_fingerprint(root)
    if not identity:
        return None
    value = _private_json(path)
    if value is None:
        return None
    fetched = value.get("fetched_at")
    if (type(fetched) not in (int, float) or not 0 <= time.time() - fetched < CACHE_SECONDS or
            value.get("account_fingerprint") != identity):
        return None
    windows = value.get("windows")
    return windows if isinstance(windows, dict) else None


def refresh_cache(path: Path, root: Path = ROOT) -> bool:
    account_id = selected_account_id(root)
    identity = selected_fingerprint(root)
    if account_id is None or path.is_symlink() or path.parent.is_symlink():
        return False
    result = selected_rate_limits(root)
    if (result is None or result.get("accountId") != account_id
            or selected_fingerprint(root) != identity):
        return False
    windows = windows_from_response(result)
    snapshot = {
        "fetched_at": time.time(), "account_fingerprint": identity,
        "windows": windows,
    }
    descriptor, temporary = tempfile.mkstemp(prefix="codex-usage-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(snapshot, handle, separators=(",", ":"))
            handle.write("\n")
        os.chmod(temporary, 0o600)
        if platform.system() == "Windows":
            import windows_acl
            windows_acl.lock([Path(temporary)])
        os.replace(temporary, path)
        if platform.system() == "Windows":
            windows_acl.lock([path])
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def start_refresh(path: Path) -> None:
    """Refresh after a HUD render without delaying Claude Code's status line."""
    if read_cache(path) is not None or selected_account_id() is None:
        return
    lock = path.with_suffix(".lock")
    created = False
    try:
        if lock.is_symlink():
            return
        if lock.exists() and time.time() - lock.stat().st_mtime > RPC_TIMEOUT_SECONDS + 5:
            lock.unlink()
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        created = True
        if platform.system() == "Windows":
            import windows_acl
            windows_acl.lock([lock])
    except FileExistsError:
        return
    except (OSError, RuntimeError):
        if created:
            lock.unlink(missing_ok=True)
        return
    try:
        options = ({"creationflags": subprocess.CREATE_NO_WINDOW} if platform.system() == "Windows"
                   else {"start_new_session": True})
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "refresh", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **options,
        )
    except OSError:
        lock.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "refresh":
        path = Path(sys.argv[2])
        try:
            return 0 if refresh_cache(path) else 1
        finally:
            path.with_suffix(".lock").unlink(missing_ok=True)
    if len(sys.argv) == 2 and sys.argv[1] in ("show", "usage"):
        account_id = selected_account_id()
        if account_id is None:
            print("Codex quota unavailable: the bridge must have one selected ChatGPT account.")
            return 1
        result = selected_rate_limits()
        if result is None or result.get("accountId") != account_id:
            print("Codex quota unavailable for the selected account.")
            return 1
        print("Codex subscription quota (selected bridge account):")
        windows = windows_from_response(result)
        for name, label in (("five_hour", "5-hour"), ("seven_day", "7-day")):
            window = windows.get(name)
            if window:
                reset = datetime.fromtimestamp(window["resets_at"], timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
                print(f"  {label}: {window['used_percentage']}% used; resets {reset}")
            else:
                print(f"  {label}: not reported for this account")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
