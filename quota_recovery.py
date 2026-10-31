"""Reconcile stale local quota gates with fresh selected-account availability.

Never renews credentials, changes accounts, runs inference, or restarts services.
The origin remains responsible for enforcing actual provider rate limits.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import codex_usage
import launcher
import runtime_bundle


USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
POLL_SECONDS = 30
USAGE_PROBE_SECONDS = 60


def usage_allows_requests(value: dict) -> bool:
    """Unknown, inconsistent, model-specific or exhausted limits fail closed."""
    def available(limit):
        if not isinstance(limit, dict) or limit.get("allowed") is not True or limit.get("limit_reached") is not False:
            return False
        windows = [limit.get(name) for name in ("primary_window", "secondary_window") if limit.get(name) is not None]
        if not windows:
            return False
        return all(isinstance(window, dict) and type(window.get("used_percent")) in (int, float)
                   and math.isfinite(window["used_percent"]) and 0 <= window["used_percent"] < 100 for window in windows)
    if not isinstance(value, dict) or not available(value.get("rate_limit")):
        return False
    additional = value.get("additional_rate_limits")
    if additional is None:
        return True
    return isinstance(additional, list) and all(isinstance(row, dict) and available(row.get("rate_limit")) for row in additional)


def quota_only(row: dict) -> bool:
    views = row.get("cooldowns")
    return (isinstance(views, list) and bool(views)
            and all(isinstance(view, dict) and view.get("reason") in ("quota", "credential_quota")
                    and view.get("http_status", 429) in (None, 0, 429) for view in views)
            and any(view.get("scope") == "credential" and view.get("reason") == "credential_quota" for view in views))


def recover_once(root: Path, *, attempts: dict | None = None, now: float | None = None) -> dict:
    with accounts.switch_lock(root):
        active = [row for row in accounts.local_accounts(root) if not row["record"].get("disabled")]
        if len(active) != 1:
            return {"status": "no_single_selected_account"}
        selected = active[0]; identity = selected["fingerprint"]
        owner = accounts.Management(root)
        base = f'http://127.0.0.1:{owner.state["port"]}/v8/management/'
        headers = {"Authorization": "Bearer " + owner.key, "Content-Type": "application/json"}

        def files():
            response = accounts.request_json(base + "credentials", headers=headers, timeout=8)
            rows = response.get("files")
            if not isinstance(rows, list):
                raise RuntimeError("Invalid local quota catalog")
            return [row for row in rows if isinstance(row, dict) and
                    (row.get("type") == "codex" or row.get("provider") == "codex") and not row.get("disabled")]

        rows = files()
        if len(rows) != 1 or rows[0].get("name") != selected["name"]:
            return {"status": "account_catalog_changed"}
        row = rows[0]
        if not quota_only(row):
            return {"status": "no_stale_credential_quota"}
        observed = time.monotonic() if now is None else now
        if attempts is not None:
            if identity in attempts and observed - attempts[identity] < USAGE_PROBE_SECONDS:
                return {"status": "probe_throttled"}
            attempts[identity] = observed
        record = selected["record"]
        token, account = record.get("access_token"), record.get("account_id")
        if not isinstance(token, str) or not token or not isinstance(account, str) or not account:
            return {"status": "usage_unavailable"}
        usage = accounts.request_json(USAGE_URL, headers={"Authorization": "Bearer " + token,
            "Chatgpt-Account-Id": account, "User-Agent": "claude-codex-http-bridge",
            "Cache-Control": "no-cache", "Pragma": "no-cache"}, timeout=12)
        if not usage_allows_requests(usage):
            return {"status": "upstream_not_available"}
        # Revalidate identity and the PID-owned listener after the network read.
        current = codex_usage.active_bridge_auth(root)
        if current is None or accounts.fingerprint(current) != identity:
            return {"status": "account_changed"}
        after = accounts.Management(root)
        if after.state != owner.state or after.key != owner.key:
            return {"status": "owner_changed"}
        rows = files()
        if (len(rows) != 1 or rows[0].get("auth_index") != row.get("auth_index") or
                rows[0].get("name") != selected["name"] or not quota_only(rows[0])):
            return {"status": "cooldown_changed"}
        if not isinstance(row.get("auth_index"), str) or not row["auth_index"]:
            return {"status": "missing_auth_index"}
        result = accounts.request_json(base + "routing/cooldown/reset", method="POST", headers=headers,
            data=json.dumps({"auth_index": row["auth_index"]}).encode(), timeout=8)
        if result.get("status") != "ok":
            raise RuntimeError("Local quota reconciliation was not acknowledged")
        return {"status": "recovered"}


@contextmanager
def monitor_lock(directory: Path):
    runtime_bundle.private_dir(directory)
    path = directory / "monitor.lock"
    if path.is_symlink():
        raise RuntimeError("Quota monitor lock must not be symlinked")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        if platform.system() == "Windows":
            import windows_acl
            import msvcrt
            windows_acl.lock([path])
            try: msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError: yield False; return
        else:
            import fcntl
            try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: yield False; return
        try:
            yield True
        finally:
            if platform.system() == "Windows":
                handle.seek(0); msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)


def watch(root: Path) -> int:
    directory = root / "runtime-quota"
    with monitor_lock(directory) as acquired:
        if not acquired:
            return 0
        attempts = {}
        while True:
            state = launcher.read_state(root / "runtime")
            if state is None or not launcher.state_is_live(root / "runtime", state):
                return 0
            try:
                result = recover_once(root, attempts=attempts)
            except (OSError, RuntimeError, ValueError):
                result = {"status": "check_unavailable"}
            runtime_bundle.write_file(directory / "status.json", runtime_bundle.canonical(
                {**result, "checked_at": time.time(), "monitor_pid": os.getpid()}), 0o600)
            threading.Event().wait(POLL_SECONDS)


def ensure_watch(root: Path) -> None:
    entry = root / "quota_recovery.py"
    if not entry.is_file() or entry.is_symlink():
        return
    try:
        if codex_usage.active_bridge_auth(root) is None:
            return
        accounts.Management(root)
    except (OSError, RuntimeError, ValueError):
        return
    environment = {name: os.environ[name] for name in ("PATH", "HOME", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TMPDIR", "TEMP", "TMP", "LANG") if name in os.environ}
    options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
               if platform.system() == "Windows" else {"start_new_session": True})
    subprocess.Popen([sys.executable, "-I", str(entry), "watch", "--root", str(root)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=environment, cwd=root, **options)


def main(arguments: list[str], *, root: Path) -> int:
    parser = argparse.ArgumentParser(prog="claude-codex quota")
    parser.add_argument("action", choices=("recover",))
    parser.parse_args(arguments)
    try: result = recover_once(root)
    except (OSError, RuntimeError, ValueError): result = {"status": "check_unavailable"}
    print(json.dumps(result))
    return 0 if result["status"] in ("recovered", "no_stale_credential_quota") else 1


if __name__ == "__main__":
    # -I omits the script directory; all imports must come from this private root.
    if len(sys.argv) == 4 and sys.argv[1:3] == ["watch", "--root"]:
        raise SystemExit(watch(Path(sys.argv[3]).resolve()))
    raise SystemExit(2)
