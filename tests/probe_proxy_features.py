"""Credential-free release gate for Claude Code's dynamic tool protocol.

Run with a candidate CLIProxyAPI binary. Without --report-only, missing
capabilities fail the command. The current pinned release is expected to report
gaps, so CI uses --report-only until a replacement release passes the gate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile  # noqa: E402
from smoke_api_contract import MODEL, MOCK_UPSTREAM_KEY, Upstream, post  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


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
    Upstream.requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
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

                base = {"model": MODEL, "max_tokens": 1024, "stream": False}
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
                if len(Upstream.requests) != 3:
                    raise RuntimeError("Candidate proxy did not forward all synthetic requests")
                addition, before, after = [entry[2] for entry in Upstream.requests]
                before_names = {entry.get("name") for entry in before.get("tools", [])}
                after_names = {entry.get("name") for entry in after.get("tools", [])}
                return {
                    "mid_conversation_tool_addition":
                        "SubagentHandback" in names_in_additional_tools(addition),
                    "deferred_tool_hidden_initially":
                        "mcp__demo__lookup" not in before_names,
                    "deferred_tool_revealed_without_prefix_change":
                        "mcp__demo__lookup" in names_in_additional_tools(after)
                        and before_names == after_names,
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
