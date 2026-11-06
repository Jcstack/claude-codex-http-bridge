"""Read local latency/compaction metadata without credentials or request bodies.

No network requests, service starts/stops, transcript writes or model calls.
Only an explicit output allowlist leaves this module; raw log lines never do.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import urllib.parse


LINE_LIMIT = 2 * 1024 * 1024
INCOMPLETE_STREAM_SENTINEL = "stream error: stream disconnected before completion: stream closed before response.completed"
GIN = re.compile(r'^\[([^]]+)\].*?\]\s+(\d{3})\s*\|\s*([^|]+)\|.*?\|\s*(GET|POST|DELETE)\s+"([^"]+)"')
ERRORS = {"tls_handshake": "tls handshake", "eof": "eof",
          "timeout": "timeout", "cancelled": "context canceled"}
ENDPOINTS = {"/v1/messages", "/v1/responses", "/v1/models", "/v1/messages/count_tokens",
             "/v1/responses/compact", "/v1/images/generations", "/v1/images/edits",
             "/v1/live", "/v1/live/sessions", "/v1/realtime", "/v1/realtime/calls",
             "/v1/realtime/client_secrets"}
STREAM_OBSERVATION_EVENTS = frozenset({"none", "other", "response.created", "response.in_progress",
    "response.output_item.added", "response.output_item.done", "response.output_text.delta",
    "response.reasoning_summary_text.delta", "response.reasoning_summary_part.added",
    "response.function_call_arguments.delta", "response.completed", "response.done", "response.incomplete", "response.failed",
    "error", "codex.rate_limits", "codex.response.metadata", "response.metadata", "keepalive", "ping"})
STREAM_FAILURE_KINDS = frozenset({"none", "other", "reported_incomplete", "reported_overload",
    "reported_rate_limit", "reported_auth", "reported_context_limit", "reported_content_filter"})
STREAM_OBSERVATION = re.compile(
    r"codex stream observation: phase=(bootstrap|stream|other) "
    r"outcome=(completion_event|terminal_event|incomplete_event|read_error|eof|other) "
    r"last_event=([a-z0-9_.-]{1,64}) (?:prior_event=([a-z0-9_.-]{1,64}) )?data_items=(\d{1,7}) "
    r"elapsed_ms=(\d{1,8}) since_read_ms=(\d{1,8}) (?:event_gap_ms=(\d{1,8}) max_read_gap_ms=(\d{1,8}) )?"
    r"read_error=(none|unexpected_eof|cancelled|timeout|other)(?: failure_kind=([a-z_]{1,40}))?$")
BUFFERED_OBSERVATION = re.compile(
    r"codex stream observation: phase=buffered "
    r"outcome=(completion_event|terminal_event|incomplete_event|read_error|eof|other) "
    r"last_event=([a-z0-9_.-]{1,64}) data_items=(\d{1,7}) elapsed_ms=(\d{1,8}) "
    r"read_error=(none|unexpected_eof|cancelled|timeout|other) failure_kind=([a-z_]{1,40})$")


def endpoint_label(raw: str) -> str:
    """Keep native media phases distinct without exporting opaque call IDs."""
    path = raw.split("?", 1)[0]
    if path in ENDPOINTS:
        return path
    for pattern, label in (
            (r"/v1/live/[A-Za-z0-9_-]{1,128}", "/v1/live/:call_id"),
            (r"/v1/live/sessions/[A-Za-z0-9_-]{1,128}", "/v1/live/sessions/:session_id"),
            (r"/v1/realtime/calls/[A-Za-z0-9_-]{1,128}/hangup", "/v1/realtime/calls/:call_id/hangup"),
            (r"/v1/realtime/calls/[A-Za-z0-9_-]{1,128}", "/v1/realtime/calls/:call_id")):
        if re.fullmatch(pattern, path):
            return label
    return "other"


def mentioned_url_host_category(line: str) -> str:
    """Classify URL host mentions without returning the URL, path or credentials.

    An error can be forwarded through another proxy, so a mentioned host does
    not establish which process opened a connection or whether it reached it.
    """
    categories = set()
    for raw in re.findall(r'https?://[^\s"\'<>\\]+', line, flags=re.IGNORECASE):
        try:
            host = urllib.parse.urlsplit(raw).hostname
        except ValueError:
            host = None
        if host is None:
            continue
        host = host.lower().rstrip(".")
        if host in ("chatgpt.com", "chat.openai.com"):
            categories.add("chatgpt")
        elif host == "api.openai.com":
            categories.add("openai_api")
        elif host in ("127.0.0.1", "localhost", "::1"):
            categories.add("loopback")
        else:
            categories.add("other")
    return "none" if not categories else next(iter(categories)) if len(categories) == 1 else "mixed"


def lines(path: Path, coverage: dict | None = None):
    """Bound memory use even when a transcript contains a huge blob record."""
    if coverage is not None:
        coverage.update(max_line_characters=LINE_LIMIT, lines_seen=0, oversized_lines=0)
    with path.open(encoding="utf-8", errors="replace") as handle:
        while True:
            line = handle.readline(LINE_LIMIT + 1)
            if not line:
                return
            if coverage is not None:
                coverage["lines_seen"] += 1
            if len(line) > LINE_LIMIT:
                if coverage is not None:
                    coverage["oversized_lines"] += 1
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


def incomplete_stream_shape(line: str) -> str:
    """Classify an error record without returning its body or unknown fields."""
    _, separator, tail = line.partition("err=")
    if not separator:
        return "unclassified"
    tail = tail.strip()
    if tail.startswith("stream error: stream disconnected before completion"):
        # A sanitized upstream error can have the same text as a locally
        # synthesized error, so the message alone cannot establish provenance.
        return "plain_or_redacted_incomplete"
    try:
        payload = json.loads(tail)
    except (ValueError, UnicodeError, RecursionError):
        return "unclassified"
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return "unclassified"
    if error.get("type") == "server_error" and error.get("code") == "request_timeout":
        if error.get("message") == INCOMPLETE_STREAM_SENTINEL:
            return "reported_incomplete_stream_sentinel"
        return "reported_server_request_timeout"
    return "other_error_payload"


def stream_observation(line: str) -> dict | None:
    """Read the experimental observer's fixed vocabulary without returning raw input."""
    match = STREAM_OBSERVATION.search(line.rstrip("\r\n"))
    if match:
        phase, outcome, event, prior, items, elapsed, since_read, event_gap, max_read_gap, read_error, failure_kind = match.groups()
        since_read = int(since_read)
    else:
        match = BUFFERED_OBSERVATION.search(line.rstrip("\r\n"))
        if not match:
            return None
        outcome, event, items, elapsed, read_error, failure_kind = match.groups()
        phase, prior, since_read, event_gap, max_read_gap = "buffered", None, None, None, None
    items, elapsed = map(int, (items, elapsed))
    if (prior is None) != (event_gap is None):
        return None
    event_gap = int(event_gap) if event_gap is not None else None
    max_read_gap = int(max_read_gap) if max_read_gap is not None else None
    timings = tuple(value for value in (elapsed, since_read, event_gap, max_read_gap) if value is not None)
    if (event not in STREAM_OBSERVATION_EVENTS or (prior is not None and prior not in STREAM_OBSERVATION_EVENTS) or
            (failure_kind is not None and failure_kind not in STREAM_FAILURE_KINDS) or
            items > 1_000_000 or any(value > 86_400_000 for value in timings)):
        return None
    if outcome == "completion_event" and (event not in ("response.completed", "response.done") or
                                          read_error != "none" or failure_kind not in (None, "none")):
        return None
    return {"phase": phase, "outcome": outcome, "last_event": event, "prior_event": prior, "data_items": items,
            "elapsed_ms": elapsed, "since_read_ms": since_read, "event_gap_ms": event_gap,
            "max_read_gap_ms": max_read_gap, "read_error": read_error, "failure_kind": failure_kind}


def proxy_summary(path: Path, since: datetime) -> dict:
    counts, errors = Counter(), Counter()
    background_events, background_last = Counter(), {}
    preconnect = Counter()
    preconnect_hosts = {}
    preconnect_requests = {}
    preconnect_last = {}
    inband, inband_requests, inband_last = Counter(), {}, {}
    shapes, shape_requests, shape_last = Counter(), {}, {}
    recent_inband = deque(maxlen=12)
    recent_preconnect = deque(maxlen=12)
    stream_observation_outcomes, recent_stream_observations = Counter(), deque(maxlen=12)
    recent_stream_noncompletion = deque(maxlen=12)
    stream_read_gaps = {}
    latencies, slow, failed = [], [], []
    coverage = {}
    for line in lines(path, coverage):
        date = re.match(r"^\[([^]]+)\]", line)
        at = timestamp(date[1]) if date else None
        if at is None or at < since:
            continue
        match = GIN.search(line)
        if match:
            endpoint = endpoint_label(match[5])
            status = int(match[2])
            counts[str(status)] += 1
            seconds = number(duration(match[3].strip()))
            if status >= 400:
                failed.append({"at": at.isoformat(), "status": status, "endpoint": endpoint,
                               "seconds": round(seconds, 3) if seconds is not None else None})
            if match[4] == "POST" and endpoint in ("/v1/messages", "/v1/responses") and seconds is not None:
                latencies.append(seconds)
                if seconds >= 120:
                    slow.append({"at": at.isoformat(), "status": status,
                                 "seconds": round(seconds, 3), "endpoint": endpoint})
        else:
            lower = line.lower()
            observation = stream_observation(line)
            if observation is not None:
                stream_observation_outcomes[observation["outcome"]] += 1
                gap = observation["max_read_gap_ms"]
                if gap is not None:
                    bucket = next((label for bound, label in (
                        (1_000, "under_1s"), (10_000, "1_to_under_10s"),
                        (30_000, "10_to_under_30s"), (60_000, "30_to_under_60s"))
                        if gap < bound), "60s_or_more")
                    stream_read_gaps.setdefault(observation["outcome"], Counter())[bucket] += 1
                request = re.match(r'^\[[^]]+\]\s+\[([a-f0-9]{8})\]', line)
                observation_record = (request[1] if request else None, at, observation)
                recent_stream_observations.append(observation_record)
                if observation["outcome"] != "completion_event":
                    recent_stream_noncompletion.append(observation_record)
            if "failed to refresh antigravity version" in lower:
                # This exact upstream log marker is distinct from an inference
                # request; it still only establishes a log mention. Do not
                # export the error, URL, request tags, or proxy credentials.
                timed_out = any(fragment in lower for fragment in
                                ("timeout", "timed out", "deadline exceeded"))
                category = ("antigravity_version_refresh_timeout" if timed_out
                            else "antigravity_version_refresh_other")
                background_events[category] += 1
                if category not in background_last or at > background_last[category]:
                    background_last[category] = at
            if "stream disconnected before completion" in lower or "stream closed before response.completed" in lower:
                category = "incomplete_native_stream"
                inband[category] += 1
                request = re.match(r'^\[[^]]+\]\s+\[([a-f0-9]{8})\]', line)
                if request:
                    inband_requests.setdefault(category, set()).add(request[1])
                inband_last[category] = at.isoformat()
                shape = incomplete_stream_shape(line)
                shapes[shape] += 1
                if request:
                    shape_requests.setdefault(shape, set()).add(request[1])
                shape_last[shape] = at.isoformat()
                recent_inband.append((request[1] if request else None, at, shape))
            if "upstream execution failed:" in lower and "tls handshake:" in lower:
                category = "tls_handshake_eof" if re.search(r'\beof\b', lower) else "tls_handshake_other"
                preconnect[category] += 1
                preconnect_hosts.setdefault(category, Counter())[mentioned_url_host_category(line)] += 1
                request = re.match(r'^\[[^]]+\]\s+\[([a-f0-9]{8})\]', line)
                if request:
                    preconnect_requests.setdefault(category, set()).add(request[1])
                preconnect_last[category] = at.isoformat()
                recent_preconnect.append((request[1] if request else None, at, category))
            for label, fragment in ERRORS.items():
                if fragment in lower:
                    errors[label] += 1
    # Match only within this one log. Stream failures use a five-second window;
    # handshake failures may precede completion by up to 30 minutes. Never
    # return tags; collisions or multiple matches stay ambiguous. Neither is
    # a cross-layer or transcript correlation, and 200 does not prove SSE end.
    matches = [[] for _ in recent_inband]
    preconnect_matches = [[] for _ in recent_preconnect]
    retained_stream_observations = list(recent_stream_observations) + list(recent_stream_noncompletion)
    stream_observation_matches = [[] for _ in retained_stream_observations]
    if recent_inband or recent_preconnect or recent_stream_observations:
        try:
            for line in lines(path):
                tag = re.match(r'^\[([^]]+)\]\s+\[([a-f0-9]{8})\]', line)
                row = GIN.search(line) if tag else None
                if not row or row[4] != "POST" or endpoint_label(row[5]) not in ("/v1/messages", "/v1/responses"):
                    continue
                at = timestamp(tag[1])
                if at is None or at < since:
                    continue
                for index, (failure_tag, failure_at, _shape) in enumerate(recent_inband):
                    if (failure_tag == tag[2] and abs((failure_at - at).total_seconds()) <= 5
                            and len(matches[index]) < 2):
                        matches[index].append({"at": at.isoformat(), "http_status": int(row[2]),
                            "seconds": duration(row[3].strip())})
                for index, (failure_tag, failure_at, _category) in enumerate(recent_preconnect):
                    delay = (at - failure_at).total_seconds()
                    if (failure_tag == tag[2] and 0 <= delay <= 1800
                            and len(preconnect_matches[index]) < 2):
                        preconnect_matches[index].append({"http_status": int(row[2]),
                            "seconds": duration(row[3].strip()), "delay": delay})
                for index, (failure_tag, failure_at, _observation) in enumerate(retained_stream_observations):
                    if (failure_tag == tag[2] and abs((failure_at - at).total_seconds()) <= 5
                            and len(stream_observation_matches[index]) < 2):
                        stream_observation_matches[index].append({"http_status": int(row[2]),
                            "seconds": duration(row[3].strip())})
        except OSError:
            matches = [[] for _ in recent_inband]
            preconnect_matches = [[] for _ in recent_preconnect]
            stream_observation_matches = [[] for _ in retained_stream_observations]
    observations = []
    for (failure_tag, at, shape), rows in zip(recent_inband, matches):
        unique = len(rows) == 1
        observations.append({"at": at.isoformat(), "shape": shape,
            "same_log_match": "unique" if unique else "ambiguous" if rows else "unmatched" if failure_tag else "untagged",
            "http_status": rows[0]["http_status"] if unique else None,
            "http_seconds": round(rows[0]["seconds"], 3) if unique and rows[0]["seconds"] is not None else None})
    preconnect_observations = []
    for (failure_tag, at, category), rows in zip(recent_preconnect, preconnect_matches):
        unique = len(rows) == 1
        preconnect_observations.append({"at": at.isoformat(), "category": category,
            "same_log_match": "unique" if unique else "ambiguous" if rows else "unmatched" if failure_tag else "untagged",
            "http_status": rows[0]["http_status"] if unique else None,
            "http_seconds": round(rows[0]["seconds"], 3) if unique and rows[0]["seconds"] is not None else None,
            "seconds_after_failure": round(rows[0]["delay"], 3) if unique else None})
    stream_observations = []
    for (failure_tag, at, observation), rows in zip(retained_stream_observations, stream_observation_matches):
        unique = len(rows) == 1
        stream_observations.append({"at": at.isoformat(), **observation,
            "same_log_match": "unique" if unique else "ambiguous" if rows else "unmatched" if failure_tag else "untagged",
            "http_status": rows[0]["http_status"] if unique else None,
            "http_seconds": round(rows[0]["seconds"], 3) if unique and rows[0]["seconds"] is not None else None})
    ordered = sorted(latencies)
    return {"completed_http_statuses": dict(counts), "transport_error_mentions": dict(errors),
            "background_event_mentions": {category: {"mentions": count, "last_at": background_last[category].isoformat()}
                                          for category, count in background_events.items()},
            "upstream_preconnection_failures": {category: {
                "attempt_mentions": count, "tagged_requests": len(preconnect_requests.get(category, set())),
                "last_at": preconnect_last[category],
                "url_host_mentions": dict(preconnect_hosts.get(category, {}))} for category, count in preconnect.items()},
            "recent_preconnection_http": preconnect_observations,
            "stream_failure_mentions": {category: {
                "mentions": count, "tagged_requests": len(inband_requests.get(category, set())),
                "last_at": inband_last[category]} for category, count in inband.items()},
            "stream_failure_shapes": {category: {
                "mentions": count, "tagged_requests": len(shape_requests.get(category, set())),
                "last_at": shape_last[category]} for category, count in shapes.items()},
            "recent_stream_failure_http": observations,
            "stream_termination_observations": {"outcome_mentions": dict(stream_observation_outcomes),
                                                 "max_read_gap_buckets_by_outcome": {
                                                     outcome: dict(buckets) for outcome, buckets in stream_read_gaps.items()},
                                                 "recent": stream_observations[:len(recent_stream_observations)],
                                                 "recent_noncompletion": stream_observations[len(recent_stream_observations):],
                                                 "noncompletion_omitted_at_limit": max(0, sum(
                                                     count for outcome, count in stream_observation_outcomes.items()
                                                     if outcome != "completion_event") - len(recent_stream_noncompletion))},
            "inference_requests": len(ordered),
            "latency_seconds": {"median": round(statistics.median(ordered), 3) if ordered else None,
                                "p95": round(ordered[math.ceil(len(ordered) * .95) - 1], 3) if ordered else None,
                                "max": round(ordered[-1], 3) if ordered else None},
            "slow_requests": slow[-12:],
            "failed_requests": failed[-12:],
            "coverage": coverage,
            "limits": "HTTP 200 can contain a later SSE error; in-flight requests are absent. Error and background-event mentions are not retry or inference-failure counts. Recent observation and noncompletion lists can overlap and must not be added together. Same-log tag matches are not cross-layer or transcript correlations. Logged error shapes do not establish which upstream component generated them. Observer timing starts after response headers and includes downstream processing; it does not prove upstream network silence."}


def correlate_stream_endings(origin: Path, backends: list[dict], since: datetime) -> dict:
    """Bound timing comparisons without asserting a cross-layer request identity."""
    failures = []
    for backend in backends:
        for row in backend.get("recent_stream_failure_http", []):
            at = timestamp(row.get("at"))
            if at is not None and at >= since:
                failures.append((at, row))
    failures.sort(key=lambda item: item[0])
    omitted = max(0, len(failures) - 12)
    failures = failures[-12:]
    candidates = [[] for _ in failures]
    coverage = {}
    if failures:
        for line in lines(origin, coverage):
            match = GIN.search(line)
            at = timestamp(match[1]) if match else None
            if (at is None or at < since or match[4] != "POST" or
                    endpoint_label(match[5]) != "/v1/responses"):
                continue
            seconds = number(duration(match[3].strip()))
            if seconds is None:
                continue
            for index, (failure_at, row) in enumerate(failures):
                managed_seconds = number(row.get("http_seconds"))
                if (row.get("same_log_match") == "unique" and managed_seconds is not None and
                        abs((at - failure_at).total_seconds()) <= 2 and
                        abs(seconds - managed_seconds) <= 2 and len(candidates[index]) < 2):
                    candidates[index].append({"status": int(match[2]), "seconds": round(seconds, 3),
                        "time_delta_seconds": round((at - failure_at).total_seconds(), 3),
                        "duration_delta_seconds": round(seconds - managed_seconds, 3)})
    result = []
    for (at, row), matches in zip(failures, candidates):
        managed_seconds = number(row.get("http_seconds"))
        comparable = row.get("same_log_match") == "unique" and managed_seconds is not None
        status = "unavailable" if not comparable else "unique_candidate" if len(matches) == 1 else "ambiguous" if matches else "unmatched"
        detail = matches[0] if status == "unique_candidate" else None
        managed_status = row.get("http_status")
        managed_status = managed_status if type(managed_status) is int and 100 <= managed_status <= 599 else None
        result.append({"at": at.isoformat(), "managed_http_status": managed_status if comparable else None,
                       "managed_http_seconds": managed_seconds if comparable else None,
                       "origin_timing_match": status, "origin_candidate": detail})
    return {"recent": result, "omitted_at_limit": omitted, "origin_scan_coverage": coverage,
            "limits": "Only timestamp and duration are compared; a unique candidate is not a cross-layer identity, proof of SSE completion, or a root-cause diagnosis. Records may refer to the same failure. Oversized lines and in-flight requests may be absent."}


def session_summary(path: Path, since: datetime) -> dict:
    compactions, last_request, last_activity = [], None, None
    api_errors = 0
    usage_by_message = {}
    usage_fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    coverage = {"malformed_lines": 0, "nonobject_records": 0, "records_missing_timestamp": 0}
    for line in lines(path, coverage):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            coverage["malformed_lines"] += 1
            continue
        if not isinstance(record, dict):
            coverage["nonobject_records"] += 1
            continue
        at = timestamp(record.get("timestamp"))
        if at is None:
            coverage["records_missing_timestamp"] += 1
            continue
        if at < since:
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
            "coverage": coverage,
            "limits": "Main-request effort is not proof of compaction effort. Missing compact_boundary does not prove a hang. Cache totals include only readable assistant usage records; commands, child sessions and skipped blobs may be absent. Cache reads still occupy context."}


def collect(root: Path, route: str, claude_roots: list[Path], since: datetime,
            session: str | None = None, *, include_sessions: bool = True) -> dict:
    # Import lazily: no key file is opened and ensure_service is never called.
    import launcher
    runtime = root / ("runtime-api" if route == "api" else "runtime")
    state = launcher.read_state(runtime)
    origin_log = None
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
            origin_log = log
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
                    # A stream can fail before an access record is written, and
                    # its sentinel need not contain any transport-error keyword.
                    # Oversized lines also limit what can be concluded, even if
                    # no recognizable in-window records were seen.
                    if (any(summary[key] for key in ("completed_http_statuses", "transport_error_mentions", "background_event_mentions",
                                                     "upstream_preconnection_failures", "stream_failure_mentions"))
                            or summary["stream_termination_observations"]["outcome_mentions"]
                            or summary["coverage"]["oversized_lines"]):
                        detail["backends"].append({"generation_tag": hashlib.sha256(log.parent.name.encode()).hexdigest()[:12],
                                                  "current": log.parent.name == managed["current"], **summary})
                result["managed"] = detail
                if origin_log is not None:
                    try:
                        result["stream_timing_correlations"] = correlate_stream_endings(origin_log, detail["backends"], since)
                    except (OSError, ValueError, OverflowError):
                        result["stream_timing_correlations"] = {"metadata_unavailable": True}
        except (OSError, RuntimeError, ValueError, ImportError):
            result["managed"] = {"metadata_unavailable": True}
    if not include_sessions:
        return result
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
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--session", help="Exact local session UUID; no transcript content is printed")
    selection.add_argument("--tracked", action="store_true", help="Use the previously recorded private audit target")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--hours", type=float)
    window.add_argument("--lifetime", action="store_true", help="Inspect the full available session history")
    parser.add_argument("--include-children", action="store_true", help="Include bounded child-session metadata")
    args = parser.parse_args(arguments)
    if args.hours is not None and (not math.isfinite(args.hours) or not 0 < args.hours <= 168):
        parser.error("--hours must be greater than zero and at most 168")
    if args.session and not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", args.session):
        parser.error("--session must be a UUID")
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    roots = [Path(value) for value in configured.split(os.pathsep) if value] if configured else [Path.home() / ".claude"]
    since = None if args.lifetime else datetime.now(timezone.utc) - timedelta(hours=args.hours or 2)
    detailed = args.lifetime or args.include_children or args.tracked
    root = args.root.expanduser()
    report = collect(root, route, roots, since or datetime.min.replace(tzinfo=timezone.utc),
                     args.session, include_sessions=not detailed)
    if detailed:
        import session_audit
        try:
            report["session_audit"] = session_audit.collect(roots, since=since, session=args.session,
                session_tag=session_audit.tracked_tag(root) if args.tracked else None,
                include_children=args.include_children)
        except (OSError, RuntimeError, ValueError):
            print(json.dumps({"read_only": True, "error": "Session audit unavailable; raw state withheld"}))
            return 1
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
