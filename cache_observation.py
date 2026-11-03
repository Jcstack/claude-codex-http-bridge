"""One short, opt-in, local-only observation of status-line cache field shapes.

No transcript, model, path, session ID, token count or cache ratio is stored.
The existing tracked-session tag selects the intended status-line input.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time


CONTROL = "cache-observation-control.json"
RESULT = "cache-observation-result.json"
WINDOW_SECONDS = 300
SUMMARY_FIELDS = frozenset({
    "telemetry_enabled", "source_fields_inspected", "source_cache_object", "source_observed_boolean",
    "source_positive_requests", "source_valid_ratio", "preexisting_namespace",
    "output_cache_object", "output_observed_boolean", "output_positive_requests", "output_valid_ratio",
})
QUOTA_FIELDS = frozenset({"quota_cache_configured", "quota_snapshot_fresh", "quota_five_hour_reported",
                          "quota_seven_day_reported", "quota_five_hour_prepared", "quota_seven_day_prepared"})


class ObservationUnavailable(RuntimeError):
    """Observation is unavailable without exposing local input."""


def _directory(root: Path) -> Path:
    directory = root / "runtime-audit"
    for path in (root, directory):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or path.is_symlink()
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ObservationUnavailable("Private observation directory is unavailable")
        if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ObservationUnavailable("Private observation directory is unavailable")
    return directory


def _open_private(path: Path, *, writable: bool = False):
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_nlink != 1
            or getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ObservationUnavailable("Private observation file is unavailable")
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1 or after.st_size > 4096
                or (os.name != "nt" and (after.st_uid != os.geteuid() or after.st_mode & 0o077))):
            raise ObservationUnavailable("Private observation file is unavailable")
        return os.fdopen(descriptor, "r+b" if writable else "rb", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate observation field")
        result[key] = value
    return result


def _read(handle) -> dict:
    handle.seek(0)
    raw = handle.read(4097)
    if len(raw) > 4096:
        raise ObservationUnavailable("Observation is unavailable")
    value = json.loads(raw, object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise ObservationUnavailable("Observation is unavailable")
    return value


def _control(directory: Path) -> dict:
    with _open_private(directory / CONTROL) as handle:
        value = _read(handle)
    base = {"version", "nonce", "session_tag", "expires_at"}
    if (set(value) not in (base, base | {"include_quota"})
            or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("nonce"), str) or not re.fullmatch(r"[a-f0-9]{32}", value["nonce"])
            or not isinstance(value.get("session_tag"), str) or not re.fullmatch(r"[a-f0-9]{12}", value["session_tag"])
            or type(value.get("expires_at")) is not int or not 0 <= value["expires_at"] <= 2**53
            or ("include_quota" in value and type(value["include_quota"]) is not bool)):
        raise ObservationUnavailable("Observation is unavailable")
    return value


def begin(root: Path, *, include_quota: bool = False) -> None:
    """Enable one bounded observation of the existing tracked session."""
    if type(include_quota) is not bool:
        raise ValueError("Invalid observation scope")
    directory = _directory(root)
    import api_profile
    import session_audit
    if os.name == "nt":
        import windows_acl
        windows_acl.verify([root, directory], protected=True)
    tag = session_audit.tracked_tag(root)
    nonce = secrets.token_hex(16)
    waiting = {"version": 1, "nonce": nonce, "state": "waiting"}
    control = {"version": 1, "nonce": nonce, "session_tag": tag, "expires_at": int(time.time()) + WINDOW_SECONDS}
    if include_quota:
        control["include_quota"] = True
    # A changed nonce makes any in-flight observer for a previous run harmless.
    api_profile.write_private(directory / RESULT, json.dumps(waiting, separators=(",", ":")) + "\n")
    api_profile.write_private(directory / CONTROL, json.dumps(control, separators=(",", ":")) + "\n")


def _cache_shape(value: object, prefix: str) -> dict[str, bool]:
    cache = value if isinstance(value, dict) else {}
    ratio, requests = cache.get("hit_ratio"), cache.get("requests")
    return {prefix + "_cache_object": isinstance(value, dict),
            prefix + "_observed_boolean": type(cache.get("caching_observed")) is bool,
            prefix + "_positive_requests": type(requests) is int and requests > 0,
            prefix + "_valid_ratio": type(ratio) in (int, float) and 0 <= ratio <= 1}


def _window_shape(value: object, *, unexpired: bool) -> bool:
    if not isinstance(value, dict):
        return False
    percent, reset = value.get("used_percentage"), value.get("resets_at")
    return (type(percent) in (int, float) and 0 <= percent <= 100 and type(reset) is int and reset > 0
            and (not unexpired or reset > time.time()))


def capture(root: Path, payload: dict, corrected: dict, *, enabled: bool,
            quota_configured: bool = False, quota_snapshot: object = None) -> None:
    """Capture the first matching field shape; never block on another writer."""
    directory = _directory(root)
    control = _control(directory)
    now = time.time()
    if not 0 <= control["expires_at"] - now <= WINDOW_SECONDS:
        return
    session = payload.get("session_id")
    if not isinstance(session, str) or not 0 < len(session) <= 512:
        return
    tag = hashlib.sha256(session.encode("utf-8")).hexdigest()[:12]
    if not hmac.compare_digest(tag, control["session_tag"]):
        return
    # A per-session opt-out also prevents this diagnostic from inspecting that
    # session's cache fields. False placeholders are explicitly marked unknown.
    inspecting = enabled is True
    original = payload.get("prompt_cache") if inspecting else None
    namespace = corrected.get("claude_codex_bridge") if inspecting else None
    output = namespace.get("reported_main_cache") if isinstance(namespace, dict) else None
    summary = {"telemetry_enabled": inspecting, "source_fields_inspected": inspecting,
               "preexisting_namespace": inspecting and "claude_codex_bridge" in payload,
               **_cache_shape(original, "source"), **_cache_shape(output, "output")}
    quota = None
    if control.get("include_quota") is True:
        snapshot = quota_snapshot if isinstance(quota_snapshot, dict) else {}
        prepared = corrected.get("rate_limits")
        prepared = prepared if isinstance(prepared, dict) else {}
        quota = {"quota_cache_configured": quota_configured is True,
                 "quota_snapshot_fresh": isinstance(quota_snapshot, dict),
                 "quota_five_hour_reported": _window_shape(snapshot.get("five_hour"), unexpired=False),
                 "quota_seven_day_reported": _window_shape(snapshot.get("seven_day"), unexpired=False),
                 "quota_five_hour_prepared": _window_shape(prepared.get("five_hour"), unexpired=True),
                 "quota_seven_day_prepared": _window_shape(prepared.get("seven_day"), unexpired=True)}
    path = directory / RESULT
    with _open_private(path, writable=True) as handle:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return
        try:
            previous = _read(handle)
            if type(previous.get("version")) is not int or previous != {"version": 1, "nonce": control["nonce"], "state": "waiting"}:
                return
            if _control(directory) != control or not 0 <= control["expires_at"] - time.time() <= WINDOW_SECONDS:
                return
            result = {"version": 1, "nonce": control["nonce"], "state": "observed", "summary": summary}
            if quota is not None:
                result["quota"] = quota
            data = (json.dumps(result, separators=(",", ":")) + "\n").encode("utf-8")
            handle.seek(0)
            handle.write(data)
            handle.truncate()
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def status(root: Path) -> dict:
    """Return fixed categories only; never output the tag, nonce or payload."""
    try:
        directory = _directory(root)
        try:
            control = _control(directory)
        except FileNotFoundError:
            return {"state": "inactive"}
        with _open_private(directory / RESULT) as handle:
            result = _read(handle)
        if (type(result.get("version")) is not int or result["version"] != 1 or result.get("nonce") != control["nonce"]
                or result.get("state") not in ("waiting", "observed")):
            raise ObservationUnavailable("Observation is unavailable")
        if result["state"] == "observed":
            summary = result.get("summary")
            required = {"version", "nonce", "state", "summary"}
            if control.get("include_quota") is True:
                required.add("quota")
            if (set(result) != required or not isinstance(summary, dict)
                    or set(summary) != SUMMARY_FIELDS or any(type(value) is not bool for value in summary.values())):
                raise ObservationUnavailable("Observation is unavailable")
            quota = {}
            if control.get("include_quota") is True:
                quota = result.get("quota")
                if (not isinstance(quota, dict) or set(quota) != QUOTA_FIELDS
                        or any(type(value) is not bool for value in quota.values())):
                    raise ObservationUnavailable("Observation is unavailable")
            return {"state": "observed", **summary, **quota}
        if set(result) != {"version", "nonce", "state"}:
            raise ObservationUnavailable("Observation is unavailable")
        if control["expires_at"] - time.time() > WINDOW_SECONDS:
            raise ObservationUnavailable("Observation is unavailable")
        return {"state": "expired" if time.time() > control["expires_at"] else "waiting"}
    except (OSError, RuntimeError, ValueError, TypeError):
        return {"state": "unavailable"}


def stop(root: Path) -> None:
    directory = _directory(root)
    for name in (CONTROL, RESULT):
        path = directory / name
        if path.is_symlink():
            raise ObservationUnavailable("Observation is unavailable")
        path.unlink(missing_ok=True)
