"""Read local latency/compaction metadata without credentials or request bodies.

No network requests, service starts/stops, transcript writes or model calls.
Only an explicit output allowlist leaves this module; raw log lines never do.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics


LINE_LIMIT = 2 * 1024 * 1024
GIN = re.compile(r'^\[([^]]+)\].*?\]\s+(\d{3})\s*\|\s*([^|]+)\|.*?\|\s*(GET|POST|DELETE)\s+"([^"]+)"')
ERRORS = {"tls_handshake": "tls handshake", "eof": "eof",
          "timeout": "timeout", "cancelled": "context canceled"}
ENDPOINTS = {"/v1/messages", "/v1/responses", "/v1/models", "/v1/messages/count_tokens",
             "/v1/responses/compact", "/v1/images/generations", "/v1/images/edits"}


def lines(path: Path):
    """Bound memory use even when a transcript contains a huge blob record."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        while True:
            line = handle.readline(LINE_LIMIT + 1)
            if not line:
                return
            if len(line) > LINE_LIMIT:
                while line and not line.endswith("\n"):
                    line = handle.readline(LINE_LIMIT + 1)
                continue
            yield line


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc)
    except ValueError:
        return None


def number(value: object) -> int | float | None:
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def duration(value: str) -> float | None:
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:h|m|s|ms|µs|us|ns))+", value):
        return None
    scale = {"h": 3600, "m": 60, "s": 1, "ms": .001, "µs": .000001,
             "us": .000001, "ns": .000000001}
    return sum(float(n) * scale[unit] for n, unit in
               re.findall(r"(\d+(?:\.\d+)?)(ms|µs|us|ns|h|m|s)", value))


def proxy_summary(path: Path, since: datetime) -> dict:
    counts, errors = Counter(), Counter()
    latencies, slow, failed = [], [], []
    for line in lines(path):
        date = re.match(r"^\[([^]]+)\]", line)
        at = timestamp(date[1]) if date else None
        if at is None or at < since:
            continue
        match = GIN.search(line)
        if match:
            endpoint = match[5].split("?", 1)[0]
            endpoint = endpoint if endpoint in ENDPOINTS else "other"
            status = int(match[2])
            counts[str(status)] += 1
            seconds = duration(match[3].strip())
            if status >= 400:
                failed.append({"at": at.isoformat(), "status": status, "endpoint": endpoint,
                               "seconds": round(seconds, 3) if seconds is not None else None})
            if match[4] == "POST" and endpoint in ("/v1/messages", "/v1/responses") and seconds is not None:
                latencies.append(seconds)
                if seconds >= 120:
                    slow.append({"at": at.isoformat(), "status": status,
                                 "seconds": round(seconds, 3), "endpoint": endpoint})
        else:
            for label, fragment in ERRORS.items():
                if fragment in line.lower():
                    errors[label] += 1
    ordered = sorted(latencies)
    return {"completed_http_statuses": dict(counts), "transport_error_mentions": dict(errors),
            "inference_requests": len(ordered),
            "latency_seconds": {"median": round(statistics.median(ordered), 3) if ordered else None,
                                "p95": round(ordered[math.ceil(len(ordered) * .95) - 1], 3) if ordered else None,
                                "max": round(ordered[-1], 3) if ordered else None},
            "slow_requests": slow[-12:],
            "failed_requests": failed[-12:],
            "limits": "HTTP 200 can contain a later SSE error; in-flight requests are absent. Error mentions are not retry counts."}


def session_summary(path: Path, since: datetime) -> dict:
    compactions, last_request, last_activity = [], None, None
    api_errors = 0
    usage_by_message = {}
    usage_fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    for line in lines(path):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        at = timestamp(record.get("timestamp"))
        if at is None or at < since:
            continue
        last_activity = max(last_activity, at) if last_activity else at
        if record.get("type") == "api-request":
            params = record.get("params")
            if not isinstance(params, dict):
                continue
            config = params.get("output_config")
            effort = config.get("effort") if isinstance(config, dict) else None
            model = params.get("model")
            model = model if isinstance(model, str) and re.fullmatch(r"(?:gpt|claude)-[a-z0-9.-]{1,70}", model) else "custom"
            last_request = {"at": at.isoformat(), "model": model,
                            "effort": effort if effort in ("none", "low", "medium", "high", "xhigh", "max") else None}
        elif record.get("type") == "system" and record.get("subtype") == "compact_boundary":
            meta = record.get("compactMetadata")
            if not isinstance(meta, dict):
                continue
            ms = number(meta.get("durationMs"))
            compactions.append({"at": at.isoformat(),
                                "trigger": meta.get("trigger") if meta.get("trigger") in ("auto", "manual") else "unknown",
                                "seconds": round(ms / 1000, 3) if ms is not None else None,
                                "before_tokens": number(meta.get("preTokens")),
                                "after_tokens": number(meta.get("postTokens"))})
        elif record.get("type") == "assistant" and record.get("isApiErrorMessage") is True:
            api_errors += 1
        elif record.get("type") == "assistant":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            usage, message_id = message.get("usage"), message.get("id")
            if not isinstance(usage, dict) or not isinstance(message_id, str) or not message_id:
                continue
            # Multiple transcript blocks can repeat cumulative usage for one
            # response. Deduplicate before computing a session cache ratio.
            counts = usage_by_message.setdefault(message_id, dict.fromkeys(usage_fields, 0))
            for field in usage_fields:
                value = usage.get(field)
                if type(value) is int and value >= 0:
                    counts[field] = max(counts[field], value)
    values = [item["seconds"] for item in compactions if item["seconds"] is not None]
    totals = {field: sum(row[field] for row in usage_by_message.values()) for field in usage_fields}
    total_input = sum(totals.values())
    cache = {**totals, "total_input_tokens": total_input, "responses_with_usage": len(usage_by_message),
             "cache_read_fraction": round(totals["cache_read_input_tokens"] / total_input, 6) if total_input else None}
    return {"session_tag": hashlib.sha256(path.stem.encode()).hexdigest()[:12],
            "last_activity": last_activity.isoformat() if last_activity else None,
            "last_main_request": last_request, "api_error_messages": api_errors,
            "compaction_count": len(compactions),
            "cache_usage": cache,
            "compaction_seconds_median": round(statistics.median(values), 3) if values else None,
            "recent_compactions": compactions[-6:],
            "limits": "Main-request effort is not proof of compaction effort. Missing compact_boundary does not prove a hang. Cache totals include only readable assistant usage records; commands, child sessions and skipped blobs may be absent. Cache reads still occupy context."}


def collect(root: Path, route: str, claude_roots: list[Path], since: datetime,
            session: str | None = None) -> dict:
    # Import lazily: no key file is opened and ensure_service is never called.
    import launcher
    runtime = root / ("runtime-api" if route == "api" else "runtime")
    state = launcher.read_state(runtime)
    result = {"read_only": True, "since": since.isoformat(), "route": route,
              "service": {"recorded": state is not None}, "sessions": []}
    if state:
        _, _, _, log = launcher.state_paths(runtime, state)
        # Avoid emitting paths, PIDs or raw state fields in a shareable report.
        try:
            result["service"]["listener_matches_state"] = launcher.listener_pids(state["port"]) == {state["pid"]}
        except (OSError, RuntimeError):
            result["service"]["listener_matches_state"] = None
        if log.is_file() and not log.is_symlink() and not log.parent.is_symlink():
            result["proxy"] = proxy_summary(log, since)
    if route == "subscription":
        try:
            import rolling_runtime
            managed = rolling_runtime.state(root)
            if managed:
                detail = {"recorded": True, "listener_matches_state": launcher.listener_pids(managed["port"]) == {managed["pid"]},
                          "backends": [], "limits": "Layers may record the same inference; counts are not additive. Only the 32 most recent private backend logs are inspected."}
                managed_root = root / "runtime-managed"
                if managed_root.is_symlink(): raise RuntimeError("Unsafe managed log root")
                logs = [p for p in managed_root.glob("g-*/proxy.log") if p.is_file() and not p.is_symlink()
                        and not p.parent.is_symlink() and re.fullmatch(r"g-[a-f0-9]{24}", p.parent.name)]
                logs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for log in logs[:32]:
                    summary = proxy_summary(log, since)
                    if summary["completed_http_statuses"] or summary["transport_error_mentions"]:
                        detail["backends"].append({"generation_tag": hashlib.sha256(log.parent.name.encode()).hexdigest()[:12],
                                                  "current": log.parent.name == managed["current"], **summary})
                result["managed"] = detail
        except (OSError, RuntimeError, ValueError, ImportError):
            result["managed"] = {"metadata_unavailable": True}
    paths = []
    for directory in claude_roots:
        projects = directory / "projects"
        if not projects.is_dir():
            continue
        for path in projects.glob("*/*.jsonl"):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            if session is None or path.stem == session:
                paths.append(path)
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:3 if session is None else 1]:
        result["sessions"].append(session_summary(path, since))
    return result


def main(arguments: list[str] | None = None, *, root: Path | None = None,
         route: str = "subscription") -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root or Path(__file__).resolve().parent)
    parser.add_argument("--session", help="Exact local session UUID; no transcript content is printed")
    parser.add_argument("--hours", type=float, default=2)
    args = parser.parse_args(arguments)
    if not math.isfinite(args.hours) or not 0 < args.hours <= 168:
        parser.error("--hours must be greater than zero and at most 168")
    if args.session and not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", args.session):
        parser.error("--session must be a UUID")
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    roots = [Path(value) for value in configured.split(os.pathsep) if value] if configured else [Path.home() / ".claude"]
    report = collect(args.root.expanduser(), route, roots,
                     datetime.now(timezone.utc) - timedelta(hours=args.hours), args.session)
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
