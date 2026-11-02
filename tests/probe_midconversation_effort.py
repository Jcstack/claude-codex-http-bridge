"""Observe mid-conversation effort translation with a local synthetic origin."""

from http.server import ThreadingHTTPServer
import argparse
import http.client
import json
from pathlib import Path
import threading

from live_support import temporary_translator
from probe_effort_roundtrip import Handler


MODEL = "gpt-6.1-sol"
LEVELS = ("low", "medium", "high", "xhigh", "max")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--expect-midconversation", action="store_true")
    args = parser.parse_args()
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin.model = MODEL
    origin.requests = []
    origin.bodies = []
    worker = threading.Thread(target=origin.serve_forever, daemon=True)
    worker.start()
    spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
            "supported_reasoning_levels": [{"effort": value} for value in LEVELS]}
    upstream = {"state": {"port": origin.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
    try:
        with temporary_translator(args.binary.resolve(), upstream, MODEL) as (port, key):
            def request(messages, *, expect_error=False):
                body = {"model": MODEL, "max_tokens": 64, "stream": True, "output_config": {"effort": "high"},
                        "metadata": {"user_id": json.dumps({"session_id": "00000000-0000-4000-8000-000000000001"})},
                        "tools": [{"name": "fixture_tool", "description": "fixture", "input_schema": {"type": "object"}}],
                        "messages": messages}
                before = len(origin.requests)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                try:
                    connection.request("POST", "/v1/messages", json.dumps(body),
                        {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                         "anthropic-version": "2023-06-01", "anthropic-beta": "mid-conversation-output-config-2026-07-01"})
                    response = connection.getresponse()
                    response.read()
                    if expect_error:
                        if response.status != 400 or len(origin.requests) != before:
                            raise RuntimeError("Invalid effort directive was not rejected before origin")
                        return None
                    if response.status != 200 or len(origin.requests) != before + 1:
                        raise RuntimeError("Synthetic mid-conversation fixture failed")
                    return origin.requests[-1]["effort"], origin.bodies[-1]
                finally:
                    connection.close()

            user = {"role": "user", "content": "fixture"}
            assistant = {"role": "assistant", "content": "fixture reply"}
            directive = {"role": "system", "content": [], "output_config": {"effort": "low"}}
            other = {"role": "system", "content": [], "output_config": {"effort": "max"}}
            tool_call = {"role": "assistant", "content": [{"type": "tool_use", "id": "call_fixture",
                          "name": "fixture_tool", "input": {}}]}
            tool_only = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_fixture",
                          "content": "fixture result"}]}
            mixed = {"role": "user", "content": [*tool_only["content"], {"type": "text", "text": "new input"}]}
            observed = {"top_level_control": request([user]),
                        "change_before_next_user": request([user, assistant, directive, user]),
                        "change_immediately_after_user": request([user, assistant, user, directive]),
                        "pending_before_tool_result": request([user, tool_call, directive, tool_only]),
                        "pending_after_tool_result": request([user, tool_call, tool_only, directive]),
                        "pending_then_new_input": request([user, tool_call, directive, tool_only, user]),
                        "mixed_tool_and_new_input": request([user, tool_call, directive, mixed]),
                        "latest_directive_wins": request([user, assistant, directive, other, user])}
            checks = {name: row[0] for name, row in observed.items()}
            if checks["top_level_control"] != "high": raise RuntimeError("Top-level effort control failed")
            initial = observed["top_level_control"][1]
            changed = observed["change_before_next_user"][1]
            stable = (initial.get("prompt_cache_key") and initial.get("prompt_cache_key") == changed.get("prompt_cache_key")
                      and initial.get("tools") == changed.get("tools") and
                      changed.get("input", [])[:len(initial.get("input", []))] == initial.get("input"))
            if args.expect_midconversation:
                expected = {"top_level_control": "high", "change_before_next_user": "low",
                            "change_immediately_after_user": "low", "pending_before_tool_result": "high",
                            "pending_after_tool_result": "high", "pending_then_new_input": "low",
                            "mixed_tool_and_new_input": "low", "latest_directive_wins": "max"}
                if checks != expected or not stable:
                    raise RuntimeError("Mid-conversation effort or stable prefix did not reach the origin")
                invalid = {**directive, "clear_at": "next_user_message"}
                request([user, invalid], expect_error=True)
            print(json.dumps({"synthetic_only": True, "stable_structural_prefix": bool(stable), **checks}, sort_keys=True))
    finally:
        origin.shutdown()
        origin.server_close()
        worker.join(timeout=2)


if __name__ == "__main__":
    try: main()
    except (OSError, RuntimeError, ValueError, http.client.HTTPException):
        raise SystemExit("Synthetic mid-conversation effort probe failed; raw output withheld") from None
