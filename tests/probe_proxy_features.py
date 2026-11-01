"""Credential-free release gate for Claude Code's dynamic tool protocol.

Run with a candidate CLIProxyAPI binary. Without --report-only, missing
capabilities fail the command. The current pinned release is expected to report
gaps, so CI uses --report-only until a replacement release passes the gate.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile  # noqa: E402
from smoke_api_contract import MODEL, MOCK_UPSTREAM_KEY, Upstream, post  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


class CaptureUpstream(Upstream):
    """Capture synthetic requests in memory, never in a production proxy log."""

    requests: list[tuple[str, str, dict]] = []
    raw_requests: list[bytes] = []

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        self.raw_requests.append(raw)
        self.rfile = io.BytesIO(raw)
        super().do_POST()


def raw_json_field(data: bytes, wanted: str) -> bytes | None:
    """Return a top-level field's exact UTF-8 representation from a fixture."""
    value = data.decode("utf-8")
    decoder = json.JSONDecoder()
    position = 0

    def skip_space(index: int) -> int:
        while index < len(value) and value[index] in " \t\r\n":
            index += 1
        return index

    position = skip_space(position)
    if position >= len(value) or value[position] != "{":
        raise RuntimeError("Synthetic request is not a JSON object")
    position += 1
    while True:
        position = skip_space(position)
        if position >= len(value):
            raise RuntimeError("Synthetic request object is incomplete")
        if value[position] == "}":
            return None
        name, position = decoder.raw_decode(value, position)
        position = skip_space(position)
        if not isinstance(name, str) or position >= len(value) or value[position] != ":":
            raise RuntimeError("Synthetic request object is malformed")
        position = skip_space(position + 1)
        start = position
        _, position = decoder.raw_decode(value, position)
        if name == wanted:
            return value[start:position].encode("utf-8")
        position = skip_space(position)
        if position >= len(value) or value[position] not in ",}":
            raise RuntimeError("Synthetic request object is malformed")
        if value[position] == "}":
            return None
        position += 1


def raw_json_array_items(data: bytes | None) -> list[bytes] | None:
    """Return exact element bytes so a history prefix can be compared."""
    if data is None:
        return None
    value = data.decode("utf-8")
    decoder = json.JSONDecoder()
    position = 0
    while position < len(value) and value[position] in " \t\r\n":
        position += 1
    if position >= len(value) or value[position] != "[":
        raise RuntimeError("Synthetic input is not a JSON array")
    position += 1
    result = []
    while True:
        while position < len(value) and value[position] in " \t\r\n":
            position += 1
        if position >= len(value):
            raise RuntimeError("Synthetic input array is incomplete")
        if value[position] == "]":
            return result
        start = position
        _, position = decoder.raw_decode(value, position)
        result.append(value[start:position].encode("utf-8"))
        while position < len(value) and value[position] in " \t\r\n":
            position += 1
        if position >= len(value) or value[position] not in ",]":
            raise RuntimeError("Synthetic input array is malformed")
        if value[position] == "]":
            return result
        position += 1


def tool(name: str, *, deferred: bool = False) -> dict:
    value = {
        "name": name, "description": f"Synthetic {name} fixture",
        "input_schema": {"type": "object", "properties": {
            "value": {"type": "string"}}, "required": ["value"]},
    }
    if deferred:
        value["defer_loading"] = True
    return value


def names_in_additional_tools(body: dict) -> set[str]:
    return {
        spec.get("name")
        for item in body.get("input", [])
        if isinstance(item, dict) and item.get("type") == "additional_tools"
        for spec in item.get("tools", [])
        if isinstance(spec, dict) and isinstance(spec.get("name"), str)
    }


def probe(binary: Path) -> dict[str, bool]:
    CaptureUpstream.requests = []
    CaptureUpstream.raw_requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), CaptureUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-proxy-features-") as temporary:
            root = Path(temporary)
            runtime = root / "runtime-api"
            runtime.mkdir(mode=0o700)
            api_profile.set_key(root, MOCK_UPSTREAM_KEY)
            spec = {
                "context_window": 1_050_000,
                "supported_reasoning_levels": [{"effort": effort} for effort in
                                               ("low", "medium", "high", "xhigh", "max")],
                "supports_search_tool": True,
                "input_modalities": ["text", "image"],
                "source": api_profile.DOCS_MODEL_BASE + MODEL + ".md",
            }
            with patch.object(api_profile, "discover_models", return_value={MODEL: spec}):
                port, _, config_path, key_path, _ = api_profile.make_session(root, runtime)
            config_path.write_text(config_path.read_text().replace(
                'base-url: "https://api.openai.com/v1"',
                f'base-url: "http://127.0.0.1:{upstream.server_port}/v1"',
            ))
            process = subprocess.Popen(
                [str(binary), "-config", str(config_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        raise RuntimeError("Candidate proxy exited during startup")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.2):
                            break
                    except OSError:
                        time.sleep(.2)
                else:
                    raise RuntimeError("Candidate proxy did not listen")

                base = {"model": MODEL, "max_tokens": 1024, "stream": False,
                        "system": "Synthetic stable system prefix for cache testing.",
                        "metadata": {"user_id": json.dumps({
                            "session_id": "00000000-0000-4000-8000-000000000001"})}}
                post(port, key_path.read_text().strip(), {
                    **base,
                    "messages": [{"role": "user", "content": "Synthetic fixture"}, {
                        "role": "system", "content": [
                            {"type": "text", "text": "A handback tool is available."},
                            {"type": "tool_addition", "tool": {
                                "type": "tool_definition",
                                "definition": tool("SubagentHandback"),
                            }},
                        ],
                    }],
                    "tools": [tool("Read")],
                })
                post(port, key_path.read_text().strip(), {
                    **base,
                    "messages": [{"role": "user", "content": "Search synthetic tools"}],
                    "tools": [tool("ToolSearch"), tool("Read"),
                              tool("mcp__demo__lookup", deferred=True)],
                })
                post(port, key_path.read_text().strip(), {
                    **base,
                    "messages": [
                        {"role": "user", "content": "Search synthetic tools"},
                        {"role": "assistant", "content": [{
                            "type": "tool_use", "id": "toolu_search", "name": "ToolSearch",
                            "input": {"query": "mcp__demo__lookup"},
                        }]},
                        {"role": "user", "content": [{
                            "type": "tool_result", "tool_use_id": "toolu_search",
                            "content": [{"type": "tool_reference",
                                         "tool_name": "mcp__demo__lookup"}],
                        }]},
                    ],
                    "tools": [tool("ToolSearch"), tool("Read"),
                              tool("mcp__demo__lookup", deferred=True)],
                })
                post(port, key_path.read_text().strip(), {
                    **base,
                    "messages": [
                        {"role": "user", "content": "Search synthetic tools"},
                        {"role": "assistant", "content": [{
                            "type": "tool_use", "id": "toolu_search", "name": "ToolSearch",
                            "input": {"query": "mcp__demo__lookup"},
                        }]},
                        {"role": "user", "content": [{
                            "type": "tool_result", "tool_use_id": "toolu_search",
                            "content": [{"type": "tool_reference",
                                         "tool_name": "mcp__demo__lookup"}],
                        }]},
                        {"role": "system", "content": [{
                            "type": "tool_removal", "tool": {
                                "type": "tool_reference", "name": "mcp__demo__lookup"},
                        }]},
                    ],
                    "tools": [tool("ToolSearch"), tool("Read"),
                              tool("mcp__demo__lookup", deferred=True)],
                })
                forced_removed_rejected = False
                invalid_payload = {
                    **base, "tools": [tool("Read")],
                    "tool_choice": {"type": "tool", "name": "Read"},
                    "messages": [{"role": "user", "content": "Synthetic fixture"}, {
                        "role": "system", "content": [{
                            "type": "tool_removal", "tool": {
                                "type": "tool_reference", "name": "Read"},
                        }],
                    }],
                }
                invalid_request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/messages",
                    data=json.dumps(invalid_payload).encode(),
                    headers={"Authorization": "Bearer " + key_path.read_text().strip(),
                             "Content-Type": "application/json",
                             "anthropic-version": "2023-06-01"},
                )
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(invalid_request, timeout=10) as response:
                        response.read()
                except urllib.error.HTTPError as error:
                    forced_removed_rejected = error.code == 400
                    error.close()
                forwarded_after_invalid = len(CaptureUpstream.requests)
                if forwarded_after_invalid not in {4, 5}:
                    raise RuntimeError("Candidate proxy did not forward all synthetic requests")
                other_session_index = forwarded_after_invalid
                post(port, key_path.read_text().strip(), {
                    **base,
                    "metadata": {"user_id": json.dumps({
                        "session_id": "00000000-0000-4000-8000-000000000002"})},
                    "messages": [{"role": "user", "content": "Search synthetic tools"}],
                    "tools": [tool("ToolSearch"), tool("Read"),
                              tool("mcp__demo__lookup", deferred=True)],
                })
                if len(CaptureUpstream.requests) != other_session_index + 1:
                    raise RuntimeError("Candidate proxy did not forward the other-session fixture")
                addition, before, after, removed = [entry[2] for entry in CaptureUpstream.requests[:4]]
                other_session = CaptureUpstream.requests[other_session_index][2]
                before_raw, after_raw, removed_raw = CaptureUpstream.raw_requests[1:4]
                other_raw = CaptureUpstream.raw_requests[other_session_index]
                raw_tools = [raw_json_field(raw, "tools") for raw in (before_raw, after_raw, removed_raw)]
                raw_instructions = [raw_json_field(raw, "instructions") for raw in
                                    (before_raw, after_raw, removed_raw)]
                before_input, after_input, removed_input = [
                    raw_json_array_items(raw_json_field(raw, "input"))
                    for raw in (before_raw, after_raw, removed_raw)]
                cache_keys = [request.get("prompt_cache_key") for request in (before, after, removed)]
                other_cache_key = other_session.get("prompt_cache_key")
                before_names = {entry.get("name") for entry in before.get("tools", [])}
                after_names = {entry.get("name") for entry in after.get("tools", [])}
                removed_names = {entry.get("name") for entry in removed.get("tools", [])}
                removed_choice = removed.get("tool_choice")
                if not isinstance(removed_choice, dict):
                    removed_choice = {}
                allowed = {entry.get("name") for entry in removed_choice.get("tools", [])}
                return {
                    "mid_conversation_tool_addition":
                        "SubagentHandback" in names_in_additional_tools(addition),
                    "deferred_tool_hidden_initially":
                        "mcp__demo__lookup" not in before_names,
                    "deferred_tool_revealed_without_prefix_change":
                        "mcp__demo__lookup" in names_in_additional_tools(after)
                        and before_names == after_names,
                    "removed_tool_is_uncallable_without_prefix_change":
                        removed_choice.get("type") == "allowed_tools"
                        and "mcp__demo__lookup" not in allowed
                        and {"ToolSearch", "Read"}.issubset(allowed)
                        and before_names == removed_names
                        and "mcp__demo__lookup" in names_in_additional_tools(removed)
                        and forced_removed_rejected and forwarded_after_invalid == 4,
                    "dynamic_tools_preserve_definition_order_and_bytes":
                        raw_tools[0] is not None and raw_tools[0] == raw_tools[1] == raw_tools[2],
                    "dynamic_tools_preserve_instruction_bytes":
                        raw_instructions[0] is not None
                        and raw_instructions[0] == raw_instructions[1] == raw_instructions[2],
                    "synthetic_system_prefix_preserved_as_developer_input":
                        bool(before_input) and bool(after_input) and bool(removed_input)
                        and before_input[0] == after_input[0] == removed_input[0]
                        and isinstance(before.get("input"), list) and bool(before["input"])
                        and isinstance(before["input"][0], dict)
                        and before["input"][0].get("role") == "developer"
                        and b"Synthetic stable system prefix for cache testing." in before_input[0],
                    "reveal_preserves_previous_input_item_bytes":
                        bool(before_input) and after_input is not None
                        and after_input[:len(before_input)] == before_input,
                    "removal_preserves_previous_input_item_bytes":
                        bool(after_input) and removed_input is not None
                        and removed_input[:len(after_input)] == after_input,
                    "dynamic_tools_preserve_same_session_cache_key":
                        isinstance(cache_keys[0], str) and bool(cache_keys[0])
                        and cache_keys[0] == cache_keys[1] == cache_keys[2],
                    "different_sessions_use_different_cache_keys":
                        isinstance(other_cache_key, str) and bool(other_cache_key)
                        and isinstance(cache_keys[0], str) and bool(cache_keys[0])
                        and other_cache_key != cache_keys[0]
                        and all(raw_json_field(before_raw, field) is not None
                                and raw_json_field(before_raw, field) == raw_json_field(other_raw, field)
                                for field in ("model", "instructions", "tools", "input")),
                }
            finally:
                process.terminate()
                process.wait(timeout=5)
    finally:
        upstream.shutdown()
        upstream.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    if not args.binary.is_file():
        raise RuntimeError("Candidate proxy binary is missing")
    result = probe(args.binary.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if args.report_only or all(result.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Feature probe failed: {error}", file=sys.stderr)
        raise SystemExit(2) from None
