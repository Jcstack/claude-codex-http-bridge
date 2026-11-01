"""Read local MCP configuration and optional tool-list snapshots without connecting.

The report is deliberately partial: plugin manifests, running sessions, remote
servers and unprovided or subsequently changed tool lists are not inspected.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_NAMES = 100_000
MAX_NAME_LENGTH = 1024


def normalized(value):
    """Return the observed Claude alias spelling for bounded ASCII names only."""
    if not isinstance(value, str) or not value or len(value) > MAX_NAME_LENGTH or not value.isascii():
        return None
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def read_object(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Input is not a regular non-symlink file")
    with path.open("rb") as handle:
        raw = handle.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("Input exceeds the size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Input must be a JSON object")
    return value


def server_keys(config, cwd):
    """Read only keys, including the exact current-project entry if present."""
    sections = []
    if "mcpServers" in config:
        if not isinstance(config["mcpServers"], dict):
            raise ValueError("Invalid MCP server section")
        sections.append(config["mcpServers"])
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("Invalid project section")
    keys = {str(cwd.absolute()), str(cwd.resolve())}
    for key in keys:
        project = projects.get(key)
        if isinstance(project, dict) and "mcpServers" in project:
            if not isinstance(project["mcpServers"], dict):
                raise ValueError("Invalid project MCP server section")
            sections.append(project["mcpServers"])
    return {name for section in sections for name in section}


def inventory_rows(value):
    rows = value.get("servers")
    if not isinstance(rows, list) or len(rows) > MAX_NAMES:
        raise ValueError("Invalid tool inventory")
    total = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("tools"), list):
            raise ValueError("Invalid tool inventory entry")
        total += len(row["tools"])
        if total > MAX_NAMES:
            raise ValueError("Tool inventory exceeds the size limit")
        for tool in row["tools"]:
            name = tool.get("name") if isinstance(tool, dict) else tool
            if not isinstance(name, str):
                raise ValueError("Invalid tool name in inventory")
            yield row["name"], name


def analyze(server_names, rows, *, show_names=False):
    if len(server_names) > MAX_NAMES:
        raise ValueError("Server inventory exceeds the size limit")
    servers, tools = {}, {}
    unverified_servers = unverified_tools = overlong = 0
    for name in set(server_names):
        alias = normalized(name)
        if alias is None:
            unverified_servers += 1
        else:
            servers.setdefault(alias, set()).add(name)
    inspected = 0
    for server, tool in rows:
        inspected += 1
        prefix, suffix = normalized(server), normalized(tool)
        if prefix is None or suffix is None:
            unverified_tools += 1
            continue
        alias = "mcp__" + prefix + "__" + suffix
        if len(alias) > 128:
            overlong += 1
            continue
        tools.setdefault(alias, set()).add((server, tool))
    server_conflicts = {key: values for key, values in servers.items() if len(values) > 1}
    tool_conflicts = {key: values for key, values in tools.items() if len(values) > 1}
    report = {"read_only": True, "scope": "partial_snapshot", "server_names_inspected": len(set(server_names)),
              "possible_server_name_collision_groups": len(server_conflicts),
              "tool_names_inspected": inspected, "possible_tool_alias_collision_groups": len(tool_conflicts),
              "unverified_server_names": unverified_servers, "unverified_tool_names": unverified_tools,
              "over_128_character_tool_aliases": overlong,
              "plugin_manifests_automatically_scanned": False, "running_sessions_examined": False,
              "mcp_servers_contacted": False, "complete_compatibility_proven": False}
    if show_names:
        report["server_collisions"] = [{"alias": alias, "server_names": sorted(values)}
                                       for alias, values in sorted(server_conflicts.items())]
        report["tool_collisions"] = [{"alias": alias, "originals": [{"server": server, "tool": tool}
                                      for server, tool in sorted(values)]}
                                     for alias, values in sorted(tool_conflicts.items())]
    return report


def default_configs(cwd):
    home = Path.home()
    roots = [Path(part).expanduser() for part in os.environ.get("CLAUDE_CONFIG_DIR", "").split(os.pathsep) if part]
    candidates = [home / ".claude.json", *(root / ".claude.json" for root in roots), cwd / ".mcp.json"]
    return list(dict.fromkeys(path.absolute() for path in candidates))


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", action="append", type=Path,
                        help="Read only server keys from this configuration; may be repeated")
    parser.add_argument("--inventory", type=Path,
                        help="Optional previously captured local tool inventory; no server is contacted")
    parser.add_argument("--show-names", action="store_true",
                        help="Print colliding names locally; may reveal private server/tool names")
    args = parser.parse_args(arguments)
    cwd = Path.cwd()
    configs = args.config if args.config is not None else default_configs(cwd)
    names, read_count, skipped = set(), 0, 0
    try:
        for path in configs:
            if args.config is None and not path.exists():
                continue
            try:
                names.update(server_keys(read_object(path.expanduser()), cwd))
                read_count += 1
            except (OSError, ValueError, UnicodeError):
                if args.config is not None:
                    raise ValueError("Explicit configuration could not be inspected") from None
                skipped += 1
        inventory = read_object(args.inventory.expanduser()) if args.inventory else None
        rows = list(inventory_rows(inventory)) if inventory is not None else []
        if inventory is not None:
            names.update(row["name"] for row in inventory["servers"])
        report = analyze(names, rows, show_names=args.show_names)
        report.update({"configuration_files_read": read_count, "default_files_skipped": skipped,
                       "tool_inventory_supplied": args.inventory is not None})
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
        return 0
    except (OSError, ValueError, UnicodeError):
        print(json.dumps({"read_only": True, "error": "Input could not be inspected; details withheld"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
