#!/usr/bin/env python3
"""Correct gateway-specific status-line telemetry before a user's plugin sees it."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import codex_usage


MAX_STDIN_BYTES = 1024 * 1024


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
            corrected = corrected_payload(payload, windows, limits,
                                          expose_cache_metrics=profile.get("expose_cache_metrics") is True)
            raw = (json.dumps(corrected, separators=(",", ":")) + "\n").encode()
    except (UnicodeDecodeError, ValueError):
        pass
    sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
