"""Compare retained MCP definitions with a user-supplied offline snapshot.

This experimental, read-only check does not contact MCP servers or modify a
session. It compares only descriptions and input schemas for exact Claude
tool names; a matching result does not prove a current or safe definition.
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys


MAX_SESSION_BYTES = 256 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_INVENTORY_BYTES = 16 * 1024 * 1024
MAX_TOOLS = 100_000
SAFE_NAME = re.compile(r"mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+\Z", re.ASCII)


def strict_json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Non-finite JSON value")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def open_regular(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    if path.is_symlink():
        raise ValueError("Symlink input is not supported")
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("Input is not a bounded regular file")
        return os.fdopen(descriptor, "rb"), before
    except BaseException:
        os.close(descriptor)
        raise


def unchanged(handle, before, path):
    after = os.fstat(handle.fileno())
    named = os.stat(path, follow_symlinks=False)
    def identity(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
    if not stat.S_ISREG(named.st_mode) or identity(before) != identity(after) or identity(before) != identity(named):
        raise ValueError("Input changed during the read")


def definition(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid tool entry")
    name, description, schema = value.get("name"), value.get("description"), value.get("input_schema")
    if not isinstance(name, str) or len(name) > 128 or not SAFE_NAME.fullmatch(name):
        raise ValueError("Unsupported exact tool name")
    if not isinstance(description, str) or not isinstance(schema, dict):
        return name, None
    # Key order in a JSON object is not semantic. Array order and values are
    # retained, and absent fields are never treated as equivalent to null.
    canonical = json.dumps(schema, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return name, (description, canonical)


def inventory(path):
    handle, before = open_regular(path, MAX_INVENTORY_BYTES)
    with handle:
        raw = handle.read(MAX_INVENTORY_BYTES + 1)
        if len(raw) > MAX_INVENTORY_BYTES:
            raise ValueError("Inventory is too large")
        value = strict_json(raw)
        unchanged(handle, before, path)
    if not isinstance(value, dict) or not isinstance(value.get("tools"), list) or len(value["tools"]) > MAX_TOOLS:
        raise ValueError("Invalid inventory")
    rows, duplicate = {}, set()
    for item in value["tools"]:
        name, fields = definition(item)
        if name in rows:
            duplicate.add(name)
        rows[name] = fields
    return rows, duplicate


def retained(path):
    handle, before = open_regular(path, MAX_SESSION_BYTES)
    rows, ambiguous, records, total, skipped, seen = {}, set(), 0, 0, 0, 0
    with handle:
        while True:
            line = handle.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            total += len(line)
            if len(line) > MAX_LINE_BYTES or total > MAX_SESSION_BYTES or not line.endswith(b"\n"):
                raise ValueError("Session record is incomplete or too large")
            event = strict_json(line)
            attachment = event.get("attachment") if isinstance(event, dict) and event.get("type") == "attachment" else None
            if not isinstance(attachment, dict) or attachment.get("type") != "deferred_tools_record":
                continue
            entries = attachment.get("entries")
            if not isinstance(entries, list):
                raise ValueError("Unsupported deferred-tool record")
            records += 1
            for entry in entries:
                seen += 1
                if seen > MAX_TOOLS:
                    raise ValueError("Too many retained entries")
                if (isinstance(entry, dict) and isinstance(entry.get("name"), str)
                        and (len(entry["name"]) > 128 or not SAFE_NAME.fullmatch(entry["name"]))):
                    skipped += 1
                    continue
                name, fields = definition(entry)
                if name in rows and rows[name] != fields:
                    ambiguous.add(name)
                rows[name] = fields
                if len(rows) > MAX_TOOLS:
                    raise ValueError("Too many retained tools")
        unchanged(handle, before, path)
    return rows, ambiguous, records, skipped


def compare(old, old_ambiguous, current, current_ambiguous, records, skipped, *, show_names=False):
    compared, same, description_changed, schema_changed = 0, 0, 0, 0
    missing, ambiguous, incomplete = 0, 0, 0
    changed_names = []
    for name, old_fields in old.items():
        if name in old_ambiguous or name in current_ambiguous:
            ambiguous += 1
        elif name not in current:
            missing += 1
        elif old_fields is None or current[name] is None:
            incomplete += 1
        else:
            compared += 1
            new_fields = current[name]
            description_diff, schema_diff = old_fields[0] != new_fields[0], old_fields[1] != new_fields[1]
            description_changed += int(description_diff)
            schema_changed += int(schema_diff)
            if description_diff or schema_diff:
                changed_names.append(name)
            else:
                same += 1
    report = {"read_only": True, "mcp_server_contacted": False, "scope": "retained_description_and_input_schema_only",
              "inventory_is_user_supplied_snapshot": True, "complete_compatibility_proven": False,
              "deferred_records": records, "retained_names": len(old), "compared_names": compared,
              "unrecognized_retained_entries": skipped,
              "matching_compared_fields": same, "description_differences": description_changed,
              "input_schema_differences": schema_changed, "retained_missing_from_snapshot": missing,
              "ambiguous_names": ambiguous, "incomplete_definitions": incomplete,
              "snapshot_names_not_retained": len(set(current) - set(old))}
    if description_changed or schema_changed:
        report["result"] = "differences_found"
    elif not compared:
        report["result"] = "no_comparable_definitions"
    elif missing or ambiguous or incomplete or skipped:
        report["result"] = "partial_comparison"
    else:
        report["result"] = "compared_fields_match"
    if show_names:
        report["changed_names"] = sorted(changed_names)
    return report


def main(arguments=None):
    parser = argparse.ArgumentParser(prog="claude-codex mcp-definitions", description=__doc__)
    parser.add_argument("--session-file", required=True, type=Path, help="Explicit local Claude session JSONL")
    parser.add_argument("--inventory", required=True, type=Path, help="Offline snapshot of exact Claude tool definitions")
    parser.add_argument("--show-names", action="store_true", help="Print changed tool names locally")
    args = parser.parse_args(arguments)
    try:
        old, old_ambiguous, records, skipped = retained(args.session_file.expanduser())
        current, current_ambiguous = inventory(args.inventory.expanduser())
        print(json.dumps(compare(old, old_ambiguous, current, current_ambiguous, records, skipped,
                                 show_names=args.show_names), ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        print(json.dumps({"read_only": True, "error": "Inputs could not be inspected; details withheld"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
