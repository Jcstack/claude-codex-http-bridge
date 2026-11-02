"""Observe native client-toolset translation using only loopback fake Responses.

The fixture does not launch a browser, a Claude client, a real model, or an
account. It records booleans about its own synthetic tool definitions/results.
"""

import argparse
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading

from live_support import temporary_translator
from probe_dynamic_toolsearch import Handler as ResponsesHandler
from smoke_api_contract import MODEL, post, post_stream


class Handler(ResponsesHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        tools = body.get("tools", [])
        state_only = next((item.get("output") for item in body.get("input", []) if isinstance(item, dict)
                           and item.get("type") == "function_call_output" and item.get("call_id") == "call_fixture_tabs"), None)
        self.server.observations.append({
            "function_count": sum(isinstance(tool, dict) and tool.get("type") == "function" for tool in tools),
            "empty_function_names": sum(isinstance(tool, dict) and tool.get("type") == "function" and
                                        tool.get("name") == "" for tool in tools),
            "custom_navigate_present": any(isinstance(tool, dict) and tool.get("name") == "navigate" for tool in tools),
            "browser_state_marker_present": "SYNTHETIC_TAB_STATE" in json.dumps(body.get("input", [])),
            "state_only_returned_as_string": isinstance(state_only, str) and "SYNTHETIC_ONLY_TAB" in state_only,
            "text_result_marker_present": "SYNTHETIC_NAV_DONE" in json.dumps(body.get("input", [])),
        })
        self.send_response_stream(len(self.server.observations), "call_fixture_new", "navigate",
                                  {"url": "https://example.invalid"}, "")


def observe(binary):
    if not binary.is_file():
        raise RuntimeError("Disposable translator is missing")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.observations = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
            "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
    upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
    payload = {"model": MODEL, "max_tokens": 128, "messages": [
        {"role": "user", "content": "synthetic fixture"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_fixture", "name": "navigate",
            "toolset_name": "browser", "input": {"url": "https://example.invalid"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_fixture", "toolset_name": "browser",
            "content": [{"type": "text", "text": "SYNTHETIC_NAV_DONE"},
                        {"type": "browser_state", "tabs": [{"tab_id": "SYNTHETIC_TAB_STATE", "title": "Synthetic tab",
                                                               "url": "https://example.invalid", "active": True}]}]}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_fixture_tabs", "name": "list_tabs",
            "toolset_name": "browser", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_fixture_tabs", "toolset_name": "browser",
            "content": [{"type": "browser_state", "tabs": [{"tab_id": "SYNTHETIC_ONLY_TAB", "title": "Synthetic tab",
                                                               "url": "https://example.invalid", "active": True}]}]}]}],
        "tools": [{"type": "browser_toolset_20260801", "allowed_callers": ["direct"],
                   "configs": {"javascript_exec": {"enabled": True}}},
                  {"type": "computer_toolset_20260801", "allowed_callers": ["direct"]},
                  {"name": "navigate", "input_schema": {"type": "object", "properties": {}}}]}
    try:
        with temporary_translator(binary, upstream, MODEL) as (port, key):
            response = post(port, key, {**payload, "stream": False})
            events = post_stream(port, key, {**payload, "stream": True})
        blocks = [block for block in response.get("content", []) if block.get("type") == "tool_use"]
        starts = [event.get("content_block", {}) for _, event in events if event.get("type") == "content_block_start"
                  and event.get("content_block", {}).get("type") == "tool_use"]
        if len(server.observations) != 2 or len(blocks) != 1 or len(starts) != 1:
            raise RuntimeError("Synthetic toolset controls were incomplete")
        same = all(item == server.observations[0] for item in server.observations)
        result = {"synthetic_only": True, "fake_upstream_only": True, "real_claude_client": False,
                  "real_provider": False, "browser_opened": False, "both_modes_same_request_observation": same,
                  **server.observations[0],
                  "nonstream_tool_name_navigate": blocks[0].get("name") == "navigate",
                  "nonstream_toolset_name_present": "toolset_name" in blocks[0],
                  "stream_tool_name_navigate": starts[0].get("name") == "navigate",
                  "stream_toolset_name_present": "toolset_name" in starts[0]}
        if not (same and result["function_count"] == 3 and result["empty_function_names"] == 2
                and result["custom_navigate_present"] and not result["browser_state_marker_present"]
                and result["state_only_returned_as_string"] and result["text_result_marker_present"] and result["nonstream_tool_name_navigate"]
                and result["stream_tool_name_navigate"] and not result["nonstream_toolset_name_present"]
                and not result["stream_toolset_name_present"]):
            raise RuntimeError("Synthetic toolset observation changed; reassess the compatibility finding")
        return result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--translator", type=Path, required=True)
    options = parser.parse_args()
    print(json.dumps(observe(options.translator.resolve()), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic toolset observation failed; raw output withheld") from None
