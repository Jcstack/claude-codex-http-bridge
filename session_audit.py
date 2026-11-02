"""Bounded, read-only metadata audit of a Claude session and its child logs.

No network, credentials, process control, transcript output or report files.
All labels in the report are allowlisted; identifiers are hashed locally.
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import statistics

from diagnostics import number, timestamp


LINE_LIMIT = 16 * 1024 * 1024
IDENTITY_LIMIT = 100_000
CHILD_LIMIT = 256
MODE_LIMIT = 128
TOOL_ERROR_TEXT_LIMIT = 8192
INPUT_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
USAGE_FIELDS = (*INPUT_FIELDS, "output_tokens")
EFFORTS = {"none", "low", "medium", "high", "xhigh", "max", "ultra"}
TOOLS = {"Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep", "NotebookEdit",
         "WebSearch", "WebFetch", "ToolSearch", "Agent", "Task", "Skill", "EnterPlanMode",
         "ExitPlanMode", "AskUserQuestion", "TaskCreate", "TaskUpdate", "TaskGet",
         "TaskList", "TaskOutput", "TaskStop", "SendMessage", "TeamCreate", "TeamDelete"}
COMMANDS = {"model", "effort", "compact", "context", "usage", "plan", "resume", "fork",
            "rewind", "clear", "cost", "status", "mcp", "permissions", "hooks", "plugin"}
LIMITS = ("Snapshot metadata only; no inference, credentials or transcript content. "
          "Usage is deduplicated by response identity, including across child logs. "
          "Cache fraction describes reported input tokens, not request hit rate, quota or pricing. "
          "Cache reads occupy context; cumulative input is not context size. "
          "Tool errors and API-error records are not unique bridge defects or requests. "
          "Tool-error categories are text patterns, not proof of cause or current task state. "
          "Child durations overlap; command mentions do not certify execution or support. "
          "Main-request effort does not identify compaction effort. "
          "Undated metadata is included only for lifetime scans. "
          "Missing, oversized, malformed or growing records limit coverage.")


def tag(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def identity(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 512:
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def safe_path(path: Path, boundary: Path) -> bool:
    """Reject symlinks from the explicitly selected config root downwards."""
    if path != boundary and boundary not in path.parents:
        return False
    for current in (path, *path.parents):
        if current.is_symlink():
            return False
        if current == boundary:
            return True
    return False


def tracked_tag(root: Path) -> str:
    path = root / "runtime-audit" / "target.json"
    if not safe_path(path, root) or not path.is_file() or path.stat().st_size > 4096:
        raise RuntimeError("Private audit target is unavailable")
    if os.name == "nt":
        import windows_acl
        windows_acl.verify([path], protected=True)
    elif path.stat().st_mode & 0o077:
        raise RuntimeError("Audit target must have private permissions")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        value = json.loads(handle.read(4097))
    result = value.get("session_tag") if isinstance(value, dict) and type(value.get("format")) is int and value["format"] == 1 else None
    if not isinstance(result, str) or not re.fullmatch(r"[a-f0-9]{12}", result):
        raise RuntimeError("Private audit target is invalid")
    return result


def records(path: Path, coverage: dict):
    """Read exactly the initial file extent; drain oversized lines in chunks."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        remaining = before.st_size
        coverage["snapshot_bytes"] = remaining
        while remaining:
            line = handle.readline(min(remaining, LINE_LIMIT + 1))
            if not line:
                coverage["short_snapshot"] = True
                break
            remaining -= len(line)
            coverage["bytes_read"] += len(line)
            coverage["lines_seen"] += 1
            if len(line) > LINE_LIMIT:
                coverage["oversized_lines"] += 1
                while line and not line.endswith(b"\n") and remaining:
                    line = handle.readline(min(remaining, LINE_LIMIT + 1))
                    remaining -= len(line)
                    coverage["bytes_read"] += len(line)
                if not line.endswith(b"\n"):
                    coverage["unterminated_tail"] = True
                continue
            if not line.endswith(b"\n"):
                coverage["unterminated_tail"] = True
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                coverage["malformed_lines"] += 1
                continue
            if not isinstance(record, dict):
                coverage["nonobject_records"] += 1
                continue
            coverage["records_parsed"] += 1
            yield record
        after = os.fstat(handle.fileno())
        coverage["changed_during_scan"] = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)


class Usage:
    def __init__(self):
        self.rows = {}
        self.omitted_records = 0
        self.invalid_identity_records = 0

    def add(self, message: dict):
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        key = identity(message.get("id"))
        if key is None:
            self.invalid_identity_records += 1
            return
        if key not in self.rows and len(self.rows) >= IDENTITY_LIMIT:
            self.omitted_records += 1
            return
        row = self.rows.setdefault(key, {})
        for field in USAGE_FIELDS:
            value = usage.get(field)
            if type(value) is int and 0 <= value <= 10**12:
                row[field] = max(row.get(field, 0), value)

    def report(self):
        rows = list(self.rows.values())
        totals = {field: sum(row.get(field, 0) for row in rows) for field in USAGE_FIELDS}
        denominator = sum(totals[field] for field in INPUT_FIELDS)
        return {"responses_with_usage": len(rows), "counter_merge_policy": "maximum_valid_value_per_response_identity", **totals,
                "reported_total_input_tokens": denominator,
                "cache_read_fraction_of_reported_input": round(totals["cache_read_input_tokens"] / denominator, 6) if denominator else None,
                "responses_missing_counter": {field: sum(field not in row for row in rows) for field in USAGE_FIELDS},
                "omitted_usage_records_at_limit": self.omitted_records,
                "usage_records_without_valid_identity": self.invalid_identity_records}


def tool_label(value: object) -> str:
    if isinstance(value, str):
        if value in TOOLS:
            return value
        if value.startswith("mcp__"):
            return "mcp"
    return "other"


def error_labels(text: str) -> list[str]:
    lower = text.lower()
    labels = []
    for label, needles in (
            ("quota_or_rate_limit", ("usage limit", "usage_limit", "rate limit", "rate_limit", "model_cooldown")),
            ("connection_closed", ("connection closed",)),
            ("incomplete_stream", ("stream disconnected before completion", "stream closed before response.completed")),
            ("incomplete_tool_call", ("upstream ended before a client tool call was complete",)),
            ("tls_handshake", ("tls handshake",)),
            ("eof", ("eof",)),
            ("timeout", ("timed out", "timeout")),
            ("cancelled", ("request aborted", "context canceled", "cancelled", "canceled", "interrupted")),
            ("output_token_limit", ("response exceeded", "output token maximum", "maximum output tokens", "output token limit")),
            ("context_size", ("prompt is too long", "context length", "context window", "context_length_exceeded"))):
        if any(needle in lower for needle in needles):
            labels.append(label)
    return labels or ["unclassified"]


def task_stop_error_category(content: object) -> tuple[str, bool]:
    """Classify only a bounded error shape; never return the error text."""
    if isinstance(content, str):
        chunks = [content]
    elif isinstance(content, list):
        chunks = (part.get("text") for part in content if isinstance(part, dict)
                  and isinstance(part.get("text"), str))
    else:
        chunks = []
    text, remaining, truncated = [], TOOL_ERROR_TEXT_LIMIT, False
    for chunk in chunks:
        if len(chunk) > remaining:
            truncated = True
        if remaining:
            text.append(chunk[:remaining])
            remaining -= min(len(chunk), remaining)
        if truncated:
            break
    matched = re.search(r"\bowned\b.{0,256}\bagent\b.{0,256}\bcannot stop\b", "".join(text), re.I | re.S)
    return ("task_stop_ownership_denial" if matched else "task_stop_other_or_unclassified"), truncated


def scan(path: Path, since: datetime | None, aggregate: Usage) -> dict:
    coverage = dict.fromkeys(("snapshot_bytes", "bytes_read", "lines_seen", "records_parsed",
        "oversized_lines", "malformed_lines", "nonobject_records", "records_missing_timestamp",
        "in_scope_records", "tool_identity_limit_records", "compaction_records_at_limit", "request_modes_merged_at_limit",
        "tool_error_text_truncated"), 0)
    coverage.update(unterminated_tail=False, short_snapshot=False, changed_during_scan=False, read_failed=False)
    usage, modes, tools, results, errors, commands, statuses = Usage(), Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    calls, completed_tools, compactions = {}, set(), []
    recent_errors, error_categories, tool_error_categories = deque(maxlen=12), Counter(), Counter()
    first = last = last_request = None
    api_errors = unpaired_results = 0
    try:
        for record in records(path, coverage):
            at = timestamp(record.get("timestamp"))
            if at is None:
                coverage["records_missing_timestamp"] += 1
                if since is not None:
                    continue
            if since is not None and at is not None and at < since:
                continue
            coverage["in_scope_records"] += 1
            if at is not None:
                first = min(first, at) if first else at
                last = max(last, at) if last else at
            kind = record.get("type")
            if kind == "api-request":
                params = record.get("params")
                if not isinstance(params, dict):
                    continue
                model = params.get("model")
                model = model if isinstance(model, str) and re.fullmatch(r"(?:gpt|claude)-[a-z0-9.-]{1,70}", model) else "custom"
                config = params.get("output_config")
                effort = config.get("effort") if isinstance(config, dict) else None
                effort = effort if isinstance(effort, str) and effort in EFFORTS else "unknown"
                mode = (model, effort)
                if mode not in modes and len(modes) >= MODE_LIMIT:
                    mode = ("custom", "unknown")
                    coverage["request_modes_merged_at_limit"] += 1
                modes[mode] += 1
                if at is not None and (last_request is None or at.isoformat() >= last_request["at"]):
                    last_request = {"at": at.isoformat(), "model": model, "effort": effort}
            if kind == "system" and record.get("subtype") == "compact_boundary":
                meta = record.get("compactMetadata")
                if isinstance(meta, dict):
                    if len(compactions) >= IDENTITY_LIMIT:
                        coverage["compaction_records_at_limit"] += 1
                    else:
                        ms = number(meta.get("durationMs"))
                        compactions.append({"at": at.isoformat() if at else None, "seconds": round(ms / 1000, 3) if ms is not None else None,
                            "trigger": meta.get("trigger") if meta.get("trigger") in ("auto", "manual") else "unknown",
                            "before_tokens": number(meta.get("preTokens")), "after_tokens": number(meta.get("postTokens"))})
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            if kind == "assistant" and record.get("isApiErrorMessage") is True:
                api_errors += 1
                content = message.get("content")
                texts = [content] if isinstance(content, str) else [part.get("text", "") for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)] if isinstance(content, list) else []
                labels, error_statuses = set(), set()
                for text in texts:
                    for status in re.findall(r"\bAPI Error:\s*([45]\d{2})\b", text):
                        statuses[status] += 1
                        error_statuses.add(status)
                    labels.update(error_labels(text))
                if len(labels) > 1:
                    labels.discard("unclassified")
                labels = sorted(labels or {"unclassified"})
                error_categories.update(labels)
                recent_errors.append({"at": at.isoformat() if at else None, "status_mentions": sorted(error_statuses), "categories": labels})
            elif kind == "assistant":
                usage.add(message)
                aggregate.add(message)
            content = message.get("content")
            if kind == "user":
                texts = [content] if isinstance(content, str) else [part.get("text", "") for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)] if isinstance(content, list) else []
                for text in texts:
                    for command in COMMANDS:
                        if re.search(r"<command-name>/" + command + r"</command-name>|\A/" + command + r"(?:\s|$)", text):
                            commands[command] += 1
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if kind == "assistant" and part.get("type") == "tool_use":
                    key = identity(part.get("id"))
                    if key is None or key in calls:
                        continue
                    if len(calls) >= IDENTITY_LIMIT:
                        coverage["tool_identity_limit_records"] += 1
                        continue
                    label = tool_label(part.get("name"))
                    calls[key] = label
                    tools[label] += 1
                elif kind == "user" and part.get("type") == "tool_result":
                    key = identity(part.get("tool_use_id"))
                    if key is None or key not in calls:
                        unpaired_results += 1
                    elif key not in completed_tools:
                        completed_tools.add(key)
                        label = calls[key]
                        results[label] += 1
                        if part.get("is_error") is True:
                            errors[label] += 1
                            if label == "TaskStop":
                                category, truncated = task_stop_error_category(part.get("content"))
                                tool_error_categories[category] += 1
                                coverage["tool_error_text_truncated"] += int(truncated)
    except (OSError, ValueError, OverflowError):
        coverage["read_failed"] = True
    seconds = [item["seconds"] for item in compactions if item["seconds"] is not None]
    return {"session_tag": tag(path.stem), "first_activity": first.isoformat() if first else None,
        "last_activity": last.isoformat() if last else None, "last_request": last_request,
        "request_modes": [{"model": model, "effort": effort, "records": count} for (model, effort), count in sorted(modes.items())],
        "api_error_records": api_errors, "api_error_status_mentions": dict(statuses),
        "api_error_category_mentions": dict(error_categories), "last_api_error_records": list(recent_errors),
        "command_record_mentions": dict(commands), "tool_calls": dict(tools), "tool_results": dict(results),
        "tool_error_results": dict(errors), "tool_error_categories": dict(tool_error_categories),
        "unpaired_tool_results": unpaired_results,
        "cache_usage": usage.report(), "compaction_count": len(compactions) + coverage["compaction_records_at_limit"],
        "compaction_seconds_median": round(statistics.median(seconds), 3) if seconds else None,
        "compaction_seconds_max": max(seconds) if seconds else None,
        "recent_compactions": sorted(compactions, key=lambda item: item["at"] or "")[-12:], "coverage": coverage}


def collect(claude_roots: list[Path], *, since: datetime | None = None,
            session: str | None = None, session_tag: str | None = None, include_children: bool = False) -> dict:
    candidates, unsafe = [], 0
    for root in dict.fromkeys(path.expanduser().absolute() for path in claude_roots):
        if not safe_path(root / "projects", root):
            unsafe += 1
            continue
        for path in (root / "projects").glob("*/*.jsonl"):
            if not safe_path(path, root):
                unsafe += 1
                continue
            if session is not None and path.stem != session or session_tag is not None and tag(path.stem) != session_tag:
                continue
            if path.is_file():
                candidates.append((path, root))
    aggregate = Usage()
    result = {"read_only": True, "time_scope": "lifetime" if since is None else "window",
        "matching_main_logs": len(candidates), "unsafe_paths_skipped": unsafe, "sessions": [], "limits": LIMITS}
    result["parser_limits"] = {"line_bytes": LINE_LIMIT, "usage_and_tool_identities_per_log": IDENTITY_LIMIT,
        "combined_usage_identities": IDENTITY_LIMIT, "compaction_details_per_log": IDENTITY_LIMIT,
        "request_modes_per_log": MODE_LIMIT + 1, "child_logs_per_main": CHILD_LIMIT,
        "tool_error_text_characters": TOOL_ERROR_TEXT_LIMIT, "default_main_logs": 3}
    if (session is not None or session_tag is not None) and len(candidates) != 1:
        result["selection_status"] = "target_missing" if not candidates else "target_ambiguous"
        return result
    candidates.sort(key=lambda item: item[0].stat().st_mtime_ns, reverse=True)
    result["main_logs_omitted_at_limit"] = max(0, len(candidates) - 3)
    result["selection_status"] = "selected"
    for path, root in candidates[:3]:
        main = scan(path, since, aggregate)
        children = []
        skipped = 0
        if include_children:
            directory = path.parent / path.stem / "subagents"
            if safe_path(directory, root):
                for child in directory.glob("*.jsonl"):
                    if safe_path(child, root) and child.is_file():
                        children.append(child)
                    else:
                        skipped += 1
            else:
                skipped += 1
        children.sort(key=lambda child: child.stat().st_mtime_ns, reverse=True)
        result["sessions"].append({"main": main, "children_requested": include_children,
            "child_logs_discovered": len(children), "child_logs_omitted_at_limit": max(0, len(children) - CHILD_LIMIT),
            "unsafe_child_paths_skipped": skipped,
            "children": [scan(child, since, aggregate) for child in children[:CHILD_LIMIT]]})
    result["combined_cache_usage"] = aggregate.report()
    return result
