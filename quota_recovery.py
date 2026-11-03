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
import re
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
PROBE_LEDGER_LIMIT = 128
PROBE_LEDGER_BYTES = 32768
GLOBAL_PROBE_KEY = "*"
MONITOR_PROTOCOL = 2
MONITOR_RESULTS = frozenset({
    "recovered", "no_single_selected_account", "account_identity_unknown", "account_catalog_changed",
    "no_stale_credential_quota", "probe_throttled", "usage_unavailable", "upstream_not_available",
    "account_changed", "owner_changed", "cooldown_changed", "missing_auth_index", "check_unavailable",
    "probe_state_unavailable",
})


class ProbeLedgerError(RuntimeError):
    """Automatic recovery cannot safely establish its previous probe time."""


def _unique_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate quota probe state field")
        value[key] = item
    return value


def _probe_time(value):
    if type(value) not in (int, float) or value < 0:
        raise ValueError("Invalid quota probe clock")
    try:
        if not math.isfinite(value):
            raise ValueError("Invalid quota probe clock")
    except OverflowError:
        raise ValueError("Invalid quota probe clock") from None
    return value


def _probe_ledger(root: Path) -> Path:
    directory = root / "runtime-quota"
    runtime_bundle.private_dir(directory)
    return directory / "probe-ledger.json"


def _private_text(path: Path, limit: int) -> str:
    runtime_bundle.safe_file(path, limit=limit)
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Quota state exceeds its size limit")
    return data.decode("utf-8")


def _read_probe_ledger(path: Path) -> dict[str, float]:
    value = json.loads(_private_text(path, PROBE_LEDGER_BYTES), object_pairs_hook=_unique_json_pairs)
    if (not isinstance(value, dict) or set(value) != {"version", "entries"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["entries"], dict) or len(value["entries"]) > PROBE_LEDGER_LIMIT):
        raise ValueError("Invalid quota probe state")
    for identity, recorded in value["entries"].items():
        if not isinstance(identity, str) or (identity != GLOBAL_PROBE_KEY and not re.fullmatch(r"[a-f0-9]{16}", identity)):
            raise ValueError("Invalid quota probe identity")
        _probe_time(recorded)
    return value["entries"]


def _write_probe_ledger(path: Path, entries: dict[str, float]) -> None:
    runtime_bundle.write_file(path, runtime_bundle.canonical({"version": 1, "entries": entries}), 0o600)
    if os.name == "nt":
        import windows_acl
        windows_acl.lock([path])


def _ensure_probe_ledger(root: Path, observed: float) -> tuple[Path, dict[str, float]]:
    _probe_time(observed)
    path = _probe_ledger(root)
    if path.is_symlink():
        raise RuntimeError("Quota probe state must not be symlinked")
    if path.exists():
        return path, _read_probe_ledger(path)
    status = path.parent / "status.json"
    if status.is_symlink():
        raise RuntimeError("Quota monitor status must not be symlinked")
    # Absence of a report does not prove that a previous monitor made no
    # request: it could have exited before writing its first report.
    entries = {GLOBAL_PROBE_KEY: observed}
    _write_probe_ledger(path, entries)
    return path, entries


def initialize_probe_ledger(root: Path, observed: float) -> Path:
    """Create durable throttle state before a new monitor can make a probe.

    The caller must hold both the monitor and account switch locks. On every
    start, wait a full interval globally: a legacy process may have left no
    report, and monotonic clock values are not portable across system boots.
    The global interval makes older per-account timestamps unnecessary.
    """
    path, entries = _ensure_probe_ledger(root, observed)
    initial = {GLOBAL_PROBE_KEY: observed}
    if entries != initial:
        _write_probe_ledger(path, initial)
    return path


def reserve_automatic_probe(root: Path, identity: str, observed: float) -> bool:
    """Persist an automatic attempt before network access; caller holds the lock."""
    _probe_time(observed)
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{16}", identity):
        raise ValueError("Invalid quota probe identity")
    path, entries = _ensure_probe_ledger(root, observed)
    original = dict(entries)
    for key, recorded in list(entries.items()):
        # After reboot or a clock anomaly, wait a full interval rather than
        # carrying a future monotonic timestamp indefinitely.
        if recorded > observed:
            entries[key] = observed
        elif observed - recorded >= USAGE_PROBE_SECONDS:
            del entries[key]
    if GLOBAL_PROBE_KEY in entries or identity in entries:
        if entries != original:
            _write_probe_ledger(path, entries)
        return False
    if len(entries) >= PROBE_LEDGER_LIMIT:
        raise RuntimeError("Quota probe state is at capacity")
    entries[identity] = observed
    _write_probe_ledger(path, entries)
    return True


def _reserve_monitor_probe(root: Path, identity: str, observed: float) -> bool:
    try:
        return reserve_automatic_probe(root, identity, observed)
    except (OSError, RuntimeError, ValueError):
        raise ProbeLedgerError("Quota probe state unavailable") from None


def usage_allows_requests(value: dict) -> bool:
    """Unknown, inconsistent, model-specific or exhausted limits fail closed."""
    def available(limit):
        if not isinstance(limit, dict) or limit.get("allowed") is not True or limit.get("limit_reached") is not False:
            return False
        windows = [limit.get(name) for name in ("primary_window", "secondary_window") if limit.get(name) is not None]
        if not windows:
            return False
        return all(isinstance(window, dict) and type(window.get("used_percent")) in (int, float)
                   and 0 <= window["used_percent"] < 100 and math.isfinite(window["used_percent"]) for window in windows)
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
        if not accounts.account_subject(selected["record"]):
            return {"status": "account_identity_unknown"}
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
            previous = attempts.get(identity)
            if previous is not None:
                if observed < previous:
                    # Persist a conservative new starting point as well as
                    # updating memory, so a successor cannot probe early.
                    _reserve_monitor_probe(root, identity, observed)
                    attempts[identity] = observed
                    return {"status": "probe_throttled"}
                if observed - previous < USAGE_PROBE_SECONDS:
                    return {"status": "probe_throttled"}
            if not _reserve_monitor_probe(root, identity, observed):
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


def watch(root: Path, *, wait=None) -> int:
    directory = root / "runtime-quota"
    if wait is None:
        wait = threading.Event().wait
    with monitor_lock(directory) as acquired:
        if not acquired:
            return 0
        attempts = {}
        ledger_ready = False
        while True:
            state = launcher.read_state(root / "runtime")
            if state is None or not launcher.state_is_live(root / "runtime", state):
                return 0
            try:
                if not ledger_ready:
                    with accounts.switch_lock(root):
                        try:
                            initialize_probe_ledger(root, time.monotonic())
                        except (OSError, RuntimeError, ValueError):
                            raise ProbeLedgerError("Quota probe state unavailable") from None
                    ledger_ready = True
                result = recover_once(root, attempts=attempts)
            except ProbeLedgerError:
                result = {"status": "probe_state_unavailable"}
            except (OSError, RuntimeError, ValueError):
                result = {"status": "check_unavailable"}
            runtime_bundle.write_file(directory / "status.json", runtime_bundle.canonical(
                {**result, "checked_at": time.time(), "monitor_pid": os.getpid(), "monitor_protocol": MONITOR_PROTOCOL}), 0o600)
            wait(POLL_SECONDS)


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


def monitor_status(root: Path) -> dict:
    """Read only allowlisted status; do not claim that the process is alive."""
    result = {"last_status": None, "checked_at": None, "monitor_protocol": None,
              "liveness": "unverified", "probe_ledger": "absent"}
    directory = root / "runtime-quota"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        return {**result, "probe_ledger": "unavailable"}
    status = directory / "status.json"
    try:
        if status.exists() or status.is_symlink():
            value = json.loads(_private_text(status, 4096), object_pairs_hook=_unique_json_pairs)
            if isinstance(value, dict) and value.get("status") in MONITOR_RESULTS:
                result["last_status"] = value["status"]
                result["checked_at"] = _probe_time(value.get("checked_at"))
                if type(value.get("monitor_protocol")) is int and value["monitor_protocol"] == MONITOR_PROTOCOL:
                    result["monitor_protocol"] = MONITOR_PROTOCOL
    except (OSError, RuntimeError, ValueError, TypeError):
        result.update(last_status=None, checked_at=None, monitor_protocol=None)
    ledger = directory / "probe-ledger.json"
    try:
        if ledger.exists() or ledger.is_symlink():
            _read_probe_ledger(ledger)
            result["probe_ledger"] = "valid"
    except (OSError, RuntimeError, ValueError, TypeError):
        result["probe_ledger"] = "unavailable"
    return result


def main(arguments: list[str], *, root: Path) -> int:
    parser = argparse.ArgumentParser(prog="claude-codex quota")
    parser.add_argument("action", choices=("recover", "status"))
    options = parser.parse_args(arguments)
    if options.action == "status":
        print(json.dumps(monitor_status(root)))
        return 0
    try: result = recover_once(root)
    except (OSError, RuntimeError, ValueError): result = {"status": "check_unavailable"}
    print(json.dumps(result))
    return 0 if result["status"] in ("recovered", "no_stale_credential_quota") else 1


if __name__ == "__main__":
    # -I omits the script directory; all imports must come from this private root.
    if len(sys.argv) == 4 and sys.argv[1:3] == ["watch", "--root"]:
        raise SystemExit(watch(Path(sys.argv[3]).resolve()))
    raise SystemExit(2)
