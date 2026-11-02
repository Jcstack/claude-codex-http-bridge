"""Observe synthetic turn-scoped system-message handling without an account."""

from http.server import ThreadingHTTPServer
import argparse
import http.client
import json
from pathlib import Path
import threading

from live_support import temporary_translator
from probe_effort_roundtrip import Handler


MODEL = "gpt-6.1-sol"
MARKER = "SYNTHETIC_SYSTEM_FIXTURE_817"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--expect-fidelity", action="store_true")
    args = parser.parse_args()
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin.model, origin.requests, origin.bodies = MODEL, [], []
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()
    spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
            "supported_reasoning_levels": [{"effort": value} for value in ("low", "medium", "high", "xhigh", "max")]}
    upstream = {"state": {"port": origin.server_port}, "key": "synthetic-local-upstream", "models": {MODEL: spec}}
    try:
        with temporary_translator(args.binary.resolve(), upstream, MODEL) as (port, key):
            def request(messages, *, expect_error=False):
                body = {"model": MODEL, "max_tokens": 64, "stream": True, "messages": messages}
                before = len(origin.bodies)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                try:
                    connection.request("POST", "/v1/messages", json.dumps(body),
                        {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                         "anthropic-version": "2023-06-01",
                         "anthropic-beta": "mid-conversation-system-clear-at-2026-08-21"})
                    response = connection.getresponse()
                    response.read()
                    if expect_error:
                        if response.status != 400 or len(origin.bodies) != before:
                            raise RuntimeError("Invalid scoped system request reached origin")
                        return None
                    if response.status != 200 or len(origin.bodies) != before + 1:
                        raise RuntimeError("Synthetic system-message fixture failed")
                    items = origin.bodies[-1].get("input", [])
                    return [item.get("role", "other") for item in items
                            if isinstance(item, dict) and MARKER in json.dumps(item)]
                finally:
                    connection.close()

            user = {"role": "user", "content": "fixture request"}
            assistant = {"role": "assistant", "content": "fixture reply"}
            system = {"role": "system", "content": MARKER}
            scoped = {**system, "clear_at": "next_user_message"}
            tool_call = {"role": "assistant", "content": [{"type": "tool_use", "id": "call_fixture",
                         "name": "fixture_tool", "input": {}}]}
            tool_only = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_fixture",
                          "content": "fixture result"}]}
            seen = {"unscoped": request([user, assistant, user, system]),
                    "not_yet_cleared": request([user, assistant, user, scoped]),
                    "after_next_user": request([user, assistant, user, scoped, assistant, user]),
                    "after_tool_result": request([user, assistant, user, scoped, tool_call, tool_only]),
                    "never_after_user": request([user, assistant, user, {**system, "clear_at": "never"}, assistant, user])}
            if args.expect_fidelity:
                expected = {"unscoped": ["developer"], "not_yet_cleared": ["developer"],
                            "after_next_user": [], "after_tool_result": [], "never_after_user": ["developer"]}
                if seen != expected:
                    raise RuntimeError("Scoped system-message fidelity did not match the fixture")
                invalid = {"role": "system", "clear_at": "next_user_message",
                           "content": [{"type": "tool_removal", "tool": {"type": "tool_reference", "name": "fixture"}}]}
                request([user, invalid], expect_error=True)
                request([user, scoped, user], expect_error=True)
            print(json.dumps({"synthetic_only": True, "marker_roles": seen,
                              "cleared_after_next_user": not seen["after_next_user"],
                              "source_role_preserved": seen["unscoped"] == ["developer"]}, sort_keys=True))
    finally:
        origin.shutdown()
        origin.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try: main()
    except (OSError, RuntimeError, ValueError, http.client.HTTPException):
        raise SystemExit("Synthetic system-message probe failed; raw output withheld") from None
