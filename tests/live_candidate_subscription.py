"""Exercise a candidate translator through the existing subscription bridge.

No OAuth file or OpenAI project key is copied to the candidate. It receives
only the current private localhost bridge key and sends synthetic requests to
that bridge. No model response body, prompt, or credential is printed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile  # noqa: E402
import bridge_models  # noqa: E402
import launcher  # noqa: E402
from smoke_api_contract import post  # noqa: E402


def run(binary: Path, installed: Path, claude_smoke: bool = False) -> dict:
    runtime = installed / "runtime"
    state = launcher.read_state(runtime)
    if state is None or not launcher.state_is_live(runtime, state):
        raise RuntimeError("Existing subscription bridge is not healthy")
    _, _, upstream_key_path, _ = launcher.state_paths(runtime, state)
    upstream_key = upstream_key_path.read_text().strip()
    with patch.dict("os.environ", {
        "CODEX_BRIDGE_PORT": str(state["port"]),
        "CODEX_BRIDGE_KEY_FILE": str(upstream_key_path),
    }):
        chat, _ = bridge_models.discover()
    model = bridge_models.aliases(chat).get("sol")
    if model is None:
        raise RuntimeError("The live catalog has no eligible Sol model")
    spec = chat[model]

    with tempfile.TemporaryDirectory(prefix="candidate-via-subscription-") as temporary:
        root = Path(temporary)
        candidate_runtime = root / "runtime-api"
        candidate_runtime.mkdir(mode=0o700)
        api_profile.set_key(root, upstream_key)
        with patch.object(api_profile, "discover_models", return_value={model: spec}):
            port, _, config_path, client_key_path, _ = api_profile.make_session(root, candidate_runtime)
        source = config_path.read_text()
        official_url = 'base-url: "https://api.openai.com/v1"'
        if source.count(official_url) != 1:
            raise RuntimeError("Unexpected candidate upstream configuration")
        source = source.replace(official_url, f'base-url: "http://127.0.0.1:{state["port"]}/v1"')
        if "api.openai.com" in source:
            raise RuntimeError("Candidate would contact the public API")
        config_path.write_text(source)
        process = subprocess.Popen(
            [str(binary), "-config", str(config_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            launcher.wait_for_private_listener(process, port)
            key = client_key_path.read_text().strip()
            tool_name = "mcp__bridge_fixture__lookup"
            tools = [
                {"name": "ToolSearch", "description": "Find a fixture tool",
                 "input_schema": {"type": "object", "properties": {
                     "query": {"type": "string"}}}},
                {"name": "Read", "description": "Read a local fixture file",
                 "input_schema": {"type": "object", "properties": {
                     "file_path": {"type": "string"}}, "required": ["file_path"]}},
                {"name": tool_name, "description": "Look up a synthetic value",
                 "defer_loading": True,
                 "input_schema": {"type": "object", "properties": {
                     "value": {"type": "string"}}, "required": ["value"]}},
            ]
            history = [
                {"role": "user", "content": "Find the fixture lookup tool."},
                {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "call_fixture_search",
                    "name": "ToolSearch", "input": {"query": tool_name},
                }]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "call_fixture_search",
                    "content": [{"type": "tool_reference", "tool_name": tool_name}],
                }]},
            ]
            base = {"model": model, "max_tokens": 400, "stream": False, "tools": tools}
            first = post(port, key, {
                **base, "tool_choice": {"type": "none"},
                "messages": [*history, {"role": "user", "content": "Reply PONG only."}],
            }, timeout=60)
            first_text = "".join(part.get("text", "") for part in first.get("content", [])
                                 if part.get("type") == "text")
            if first_text.strip() != "PONG":
                raise RuntimeError("Candidate did not complete the synthetic reveal request")

            second = post(port, key, {
                **base, "tool_choice": {"type": "tool", "name": tool_name},
                "messages": [*history, {"role": "user", "content":
                                        "Call the fixture lookup tool with value PONG."}],
            }, timeout=60)
            calls = [part for part in second.get("content", [])
                     if part.get("type") == "tool_use" and part.get("name") == tool_name]
            if len(calls) != 1:
                raise RuntimeError("Loaded deferred tool was not callable")
            third = post(port, key, {
                **base, "tool_choice": {"type": "none"},
                "messages": [*history,
                             {"role": "user", "content": "Call the fixture lookup tool with value PONG."},
                             {"role": "assistant", "content": second["content"]},
                             {"role": "user", "content": [{
                                 "type": "tool_result", "tool_use_id": calls[0]["id"],
                                 "content": "PONG",
                             }]},
                             {"role": "user", "content": "Reply PONG only."}],
            }, timeout=60)
            third_text = "".join(part.get("text", "") for part in third.get("content", [])
                                 if part.get("type") == "text")
            if third_text.strip() != "PONG":
                raise RuntimeError("Candidate did not continue after the synthetic tool result")
            removed = post(port, key, {
                **base, "tool_choice": {"type": "any"},
                "messages": [*history,
                             {"role": "system", "content": [{
                                 "type": "tool_removal", "tool": {
                                     "type": "tool_reference", "name": tool_name},
                             }]},
                             {"role": "user", "content":
                              "Call Read on the local fixture file README.md."}],
            }, timeout=60)
            allowed_calls = [part.get("name") for part in removed.get("content", [])
                             if part.get("type") == "tool_use"]
            if not allowed_calls or any(name not in {"Read", "ToolSearch"}
                                        for name in allowed_calls):
                raise RuntimeError("Removed tool remained callable in live inference")
            result = {"model": model, "reveal": "pass", "tool_call": "pass",
                      "tool_result_continuation": "pass", "tool_removal": "pass"}
            if claude_smoke:
                claude = shutil.which("claude")
                if claude is None:
                    raise RuntimeError("Claude Code executable is missing")
                work = root / "claude-smoke"
                work.mkdir(mode=0o700)
                mcp_server = root / "fixture_mcp.py"
                mcp_server.write_text('''import json, sys
for line in sys.stdin:
    try:
        request = json.loads(line)
        method = request.get("method", "")
        call_id = request.get("id")
        if call_id is None:
            continue
        if method == "server/discover":
            reply = {"jsonrpc": "2.0", "id": call_id,
                     "error": {"code": -32601, "message": "Use legacy initialize"}}
        elif method == "initialize":
            reply = {"jsonrpc": "2.0", "id": call_id, "result": {
                "protocolVersion": request.get("params", {}).get("protocolVersion", "2025-11-25"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1.0"}}}
        elif method == "tools/list":
            reply = {"jsonrpc": "2.0", "id": call_id, "result": {"tools": [{
                "name": "lookup", "description": "Return the synthetic fixture value",
                "inputSchema": {"type": "object", "properties": {
                    "value": {"type": "string"}}, "required": ["value"]}}]}}
        elif method == "tools/call":
            reply = {"jsonrpc": "2.0", "id": call_id, "result": {
                "content": [{"type": "text", "text": "BRIDGE_DYNAMIC_TOOL_OK"}]}}
        else:
            reply = {"jsonrpc": "2.0", "id": call_id,
                     "error": {"code": -32601, "message": "Unknown method"}}
        sys.stdout.write(json.dumps(reply, separators=(",", ":")) + "\\n")
        sys.stdout.flush()
    except (ValueError, TypeError):
        pass
''')
                mcp_config = root / "mcp.json"
                mcp_config.write_text(json.dumps({"mcpServers": {"fixture": {
                    "command": sys.executable, "args": [str(mcp_server)]}}}))
                settings = root / "claude-settings.json"
                settings.write_text(json.dumps({"modelPicker": {
                    "options": [{"model": model, "label": "Candidate Sol",
                                 "behavesAs": "claude-sonnet-5-5"}],
                    "replaceBuiltInOptions": True,
                }}))
                environment = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP")
                               if name in os.environ}
                environment.update({
                    "CLAUDE_CONFIG_DIR": str(root / "claude-config"),
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                    "ANTHROPIC_AUTH_TOKEN": key,
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                    "ENABLE_TOOL_SEARCH": "true",
                    "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING": "1",
                    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
                    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "DISABLE_TELEMETRY": "1",
                })
                command = [claude, "--restricted", "--permission-mode", "dontAsk", "--settings", str(settings),
                           "--model", model, "--tools", "Read,ToolSearch",
                           "--allowedTools", "Read,mcp__fixture__lookup",
                           "--mcp-config", str(mcp_config), "--strict-mcp-config", "-p",
                           "Use ToolSearch to find mcp__fixture__lookup, call it with value check, "
                           "and reply with its exact returned text.",
                           "--output-format", "stream-json", "--include-partial-messages",
                           "--verbose"]
                execution = subprocess.run(command, cwd=work, env=environment,
                                           capture_output=True, text=True, timeout=90)
                events = []
                for line in execution.stdout.splitlines():
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        pass
                stream = [entry.get("event", {}) for entry in events
                          if entry.get("type") == "stream_event"]
                used = [entry.get("content_block", {}).get("name") for entry in stream
                        if entry.get("type") == "content_block_start" and
                        entry.get("content_block", {}).get("type") == "tool_use"]
                final = next((entry for entry in reversed(events)
                              if entry.get("type") == "result"), {})
                if (execution.returncode or final.get("is_error") or
                        final.get("result", "").strip() != "BRIDGE_DYNAMIC_TOOL_OK"):
                    raise RuntimeError("Claude Code candidate smoke did not complete")
                if "ToolSearch" not in used or "mcp__fixture__lookup" not in used:
                    raise RuntimeError("Claude Code did not exercise ToolSearch and MCP")
                result["claude_code_tool_search"] = "pass"
                result["claude_code_mcp"] = "pass"
            return result
        finally:
            process.terminate()
            process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--root", type=Path, required=True,
                        help="Installed subscription bridge root; no credentials are printed")
    parser.add_argument("--claude-smoke", action="store_true",
                        help="Also exercise a minimal isolated Claude Code session")
    args = parser.parse_args()
    if not args.binary.is_file():
        raise RuntimeError("Candidate proxy binary is missing")
    print(json.dumps(run(args.binary.resolve(), args.root.expanduser().resolve(),
                         args.claude_smoke), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Live candidate probe failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
