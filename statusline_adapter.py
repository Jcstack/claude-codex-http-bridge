#!/usr/bin/env python3
"""Correct gateway-specific status-line telemetry before a user's plugin sees it."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

import codex_usage


MAX_STDIN_BYTES = 1024 * 1024
ROOT = Path(__file__).resolve().parent
CACHE_PREFERENCE = "statusline-cache-metrics.json"


def cache_metrics_preference(root: Path) -> bool:
    """Read the local opt-in without following symlinks or accepting partial data."""
    path = root / CACHE_PREFERENCE
    try:
        if root.is_symlink() or not stat.S_ISREG(path.lstat().st_mode): return False
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return False
            if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
                return False
            value = json.loads(handle.read(4097))
        return (isinstance(value, dict) and set(value) == {"version", "enabled"} and
                type(value["version"]) is int and value["version"] == 1 and value["enabled"] is True)
    except (OSError, ValueError, TypeError):
        return False


def cache_metrics_enabled(profile: dict, root: Path | None = None) -> bool:
    """A per-session choice wins over the subscription-wide local opt-in."""
    selected = profile.get("expose_cache_metrics")
    if type(selected) is bool:
        return selected
    # Older launchers recorded an explicit opt-in but not an explicit opt-out.
    # A status-line subprocess can still inherit that session's original choice.
    inherited = os.environ.get("CLAUDE_CODEX_STATUSLINE_CACHE_METRICS")
    if inherited in ("0", "1"):
        return inherited == "1"
    return profile.get("route", "subscription") == "subscription" and cache_metrics_preference(root or ROOT)


def cache_metrics_command(root: Path, arguments: list[str]) -> int:
    actions = ("enable", "disable", "status", "observe", "observation", "stop-observation")
    if len(arguments) != 2 or arguments[0] != "telemetry" or arguments[1] not in actions:
        raise ValueError("Usage: claude-codex cache telemetry enable | disable | status | observe | observation | stop-observation")
    action = arguments[1]
    if action in ("observe", "observation", "stop-observation"):
        import cache_observation
        if action == "observe":
            cache_observation.begin(root)
            print("One local, tracked-session cache-field observation is enabled for five minutes.")
        elif action == "observation":
            print(json.dumps(cache_observation.status(root), separators=(",", ":")))
        else:
            cache_observation.stop(root)
            print("Local cache-field observation stopped and its snapshot removed.")
        return 0
    if action != "status":
        import api_profile
        api_profile.write_private(root / CACHE_PREFERENCE,
                                  json.dumps({"version": 1, "enabled": action == "enable"}) + "\n")
    enabled = cache_metrics_preference(root)
    if action != "status" and enabled != (action == "enable"):
        raise RuntimeError("Could not verify the cache telemetry preference")
    print("Reported main-session cache telemetry: " + ("enabled" if enabled else "disabled") +
          " by local preference; explicit per-session settings take precedence.")
    return 0


def corrected_payload(payload: dict, context_windows: dict[str, int], codex_limits: dict | None = None,
                      *, expose_cache_metrics: bool = False) -> dict:
    result = dict(payload)
    model = result.get("model")
    model_id = model.get("id") if isinstance(model, dict) else None
    window = context_windows.get(model_id) if isinstance(model_id, str) else None
    if type(window) is int and window > 0:
        context = result.get("context_window")
        context = dict(context) if isinstance(context, dict) else {}
        usage = context.get("current_usage")
        names = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        values = [usage.get(name) for name in names] if isinstance(usage, dict) else []
        if len(values) == len(names) and all(type(value) is int and value >= 0 for value in values):
            tokens = sum(values)
            # Integer arithmetic avoids overflowing a float on malformed input.
            percent = min(100, (tokens * 200 + window) // (window * 2))
        else:
            # Claude can clear current_usage after /compact or before its first
            # response. An unknown usage must not be reported as zero.
            percent = None
        context.update({
            "context_window_size": window,
            "used_percentage": percent,
            "remaining_percentage": 100 - percent if percent is not None else None,
        })
        result["context_window"] = context
    elif isinstance(result.get("context_window"), dict):
        context = dict(result["context_window"])
        for name in ("context_window_size", "used_percentage", "remaining_percentage"):
            context.pop(name, None)
        result["context_window"] = context
    cost = result.get("cost")
    if isinstance(cost, dict):
        cost = dict(cost)
        cost.pop("total_cost_usd", None)
        result["cost"] = cost
    # Claude derives these counters from reported input/cache tokens. Its cache
    # lifetime, warmth and miss diagnoses assume provider-specific semantics.
    cache = result.pop("prompt_cache", None)
    if expose_cache_metrics and isinstance(cache, dict) and "claude_codex_bridge" not in result:
        safe_cache = {}
        observed, requests, ratio = cache.get("caching_observed"), cache.get("requests"), cache.get("hit_ratio")
        if type(observed) is bool: safe_cache["caching_observed"] = observed
        if type(requests) is int and requests >= 0: safe_cache["requests"] = requests
        if type(ratio) in (int, float) and 0 <= ratio <= 1: safe_cache["hit_ratio"] = ratio
        if safe_cache: result["claude_codex_bridge"] = {"reported_main_cache": safe_cache}
    # Rate limits here describe Anthropic billing, not this Codex route.
    result.pop("rate_limits", None)
    if codex_limits:
        result["rate_limits"] = codex_limits
    return result


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    profile = json.loads(Path(sys.argv[1]).read_text())
    windows = profile.get("context_windows")
    if not isinstance(windows, dict):
        return 2
    limits = None
    cache_path = profile.get("usage_cache")
    if isinstance(cache_path, str) and cache_path:
        cache = Path(cache_path)
        try:
            limits = codex_usage.read_cache(cache)
            if limits is None:
                codex_usage.start_refresh(cache)
        except (OSError, RuntimeError, ValueError):
            pass
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        return 1
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            if profile.get("remember_model"):
                try:
                    import session_models
                    session_models.record(Path(__file__).resolve().parent, payload, windows, profile.get("route", "subscription"))
                except (OSError, RuntimeError, ValueError):
                    pass
            enabled = cache_metrics_enabled(profile)
            corrected = corrected_payload(payload, windows, limits, expose_cache_metrics=enabled)
            try:
                if profile.get("route", "subscription") == "subscription" and (ROOT / "runtime-audit" / "cache-observation-control.json").exists():
                    import cache_observation
                    cache_observation.capture(ROOT, payload, corrected, enabled=enabled)
            except Exception:
                # A local, optional diagnostic must never break the status line.
                pass
            raw = (json.dumps(corrected, separators=(",", ":")) + "\n").encode()
    except (UnicodeDecodeError, ValueError):
        pass
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
