#!/usr/bin/env python3
"""Replace WebFetch PDF byte summaries with a truthful local-file handoff.

Claude Code's WebFetch can pass PDF bytes to its summarizer and append the saved
file path after the answer. This hook runs only for WebFetch, changes only that
specific result, and makes no network requests or additional file copies.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


PDF_SUFFIX = re.compile(
    r"\[Binary content \(application/pdf,\s*[^)\]\r\n]+\) also saved to ([^\]\r\n]+)\]\s*$",
    re.IGNORECASE,
)


def safe_saved_path(raw: str) -> str | None:
    path = Path(raw.strip())
    if (not path.is_absolute() or path.is_symlink() or
        path.suffix.lower() != ".pdf" or not path.name.startswith("webfetch-") or
        "tool-results" not in path.parts or not path.is_file()):
        return None
    return str(path)


def replace_pdf_summary(value: object) -> tuple[object, bool]:
    if isinstance(value, str):
        match = PDF_SUFFIX.search(value)
        if match is None:
            return value, False
        saved = safe_saved_path(match.group(1))
        warning = (
            "WebFetch downloaded a PDF, but its summarizer did not receive extracted PDF text. "
            "Any facts, quotes, or section references in the discarded WebFetch summary are unverified. "
        )
        if saved:
            warning += f"The saved PDF is at {saved}. Use Read on that file or local PDF text extraction, then verify quotations against the document."
        else:
            warning += "The saved file path could not be verified. Fetch or locate the PDF, extract its text locally, and verify against the document."
        return warning, True
    if isinstance(value, list):
        changed = False
        parts = []
        for part in value:
            replacement, did_change = replace_pdf_summary(part)
            parts.append(replacement)
            changed |= did_change
        return parts, changed
    if isinstance(value, dict):
        changed = False
        result = {}
        for key, part in value.items():
            replacement, did_change = replace_pdf_summary(part)
            result[key] = replacement
            changed |= did_change
        return result, changed
    return value, False


def transform(event: dict) -> dict | None:
    if event.get("hook_event_name") != "PostToolUse" or event.get("tool_name") != "WebFetch":
        return None
    if "tool_response" not in event:
        return None
    replacement, changed = replace_pdf_summary(event["tool_response"])
    if not changed:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "updatedToolOutput": replacement,
    }}


def main() -> None:
    try:
        event = json.load(sys.stdin)
        if isinstance(event, dict):
            result = transform(event)
            if result is not None:
                json.dump(result, sys.stdout, ensure_ascii=False)
                sys.stdout.write("\n")
    except (OSError, ValueError, TypeError):
        return


if __name__ == "__main__":
    main()
