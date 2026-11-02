#!/usr/bin/env python3
"""Experimental, local-only Claude hook for an ownership-shaped stop denial.

The hook performs no task control, logging, or network access. It returns only
a fixed advisory, never the hook input. It is not installed or enabled by default.
"""

from __future__ import annotations

import json
import re
import sys


MAX_INPUT_BYTES = 1024 * 1024
OWNERSHIP = re.compile(r"\bowned\b.{0,256}\bagent\b.{0,256}\bcannot stop\b", re.I | re.S)
ADVISORY = ("TaskStop reported an ownership error and did not confirm the target stopped. "
            "The target may still be running. Verify the task and coordinate with the "
            "session's controller before relying on cancellation or starting overlapping "
            "work; preserve existing permission boundaries.")


def evaluate(value: object) -> dict:
    if not isinstance(value, dict) or value.get("hook_event_name") != "PostToolUseFailure":
        return {}
    interrupt = value.get("is_interrupt")
    if value.get("tool_name") != "TaskStop" or (interrupt is not None and interrupt is not False):
        return {}
    error = value.get("error")
    if not isinstance(error, str) or not OWNERSHIP.search(error[:8192]):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": ADVISORY}}


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    try:
        value = json.loads(raw) if len(raw) <= MAX_INPUT_BYTES else None
    except (ValueError, UnicodeDecodeError, RecursionError):
        value = None
    print(json.dumps(evaluate(value), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
