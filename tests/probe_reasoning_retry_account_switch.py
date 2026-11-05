"""Exercise in-flight credential selection with disposable fake services only.

This tests configuration reload of synthetic API keys, not real OAuth switching,
refresh, or migration. The candidate binary is unsafe for production use.
"""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from unittest.mock import patch

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
import codex_stream_fixture as fixture  # noqa: E402
import probe_claude_stream_safety as probe  # noqa: E402
from probe_reasoning_retry_candidate import (  # noqa: E402
    enabled_environment,
    fixture_write_text,
    original_write_text,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    candidate = args.candidate.resolve()
    state = {
        "config": None,
        "port": None,
        "key": None,
        "auth": [],
        "selected_b": False,
        "bodies": [],
    }
    original_translator = probe.temporary_codex_translator
    original_emit = probe.Handler.emit_responses

    @contextmanager
    def capture_origin(binary, *positional, **keyword):
        with original_translator(binary, *positional, **keyword) as value:
            if Path(binary).resolve() == candidate:
                state["port"], state["key"], log = value
                state["config"] = log.parent / "config.yaml"
            yield value

    def selection_probe():
        payload = {
            "model": "gpt-6.1-sol",
            "stream": True,
            "input": [{"role": "user", "content": "SYNTHETIC_SELECTION_CHECK"}],
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{state['port']}/v1/responses",
            data=json.dumps(payload).encode(),
            headers={
                "authorization": "Bearer " + state["key"],
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=3
            ) as response:
                response.read(100_000)
        except (OSError, urllib.error.URLError):
            pass

    def emit(self, body):
        auth = self.headers.get("Authorization", "")
        if "SYNTHETIC_SELECTION_CHECK" in json.dumps(body):
            if auth == "Bearer fixture-local":
                state["selected_b"] = True
            item = {
                "id": "fixture-check",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "CHECK", "annotations": []}
                ],
            }
            events = [
                {
                    "type": "response.created",
                    "response": {"id": "fixture-check", "model": "gpt-6.1-sol"},
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "fixture-check",
                        "model": "gpt-6.1-sol",
                        "status": "completed",
                        "output": [item],
                        "usage": {"input_tokens": 10, "output_tokens": 1},
                    },
                },
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            return

        state["auth"].append(auth)
        state["bodies"].append(json.dumps(body, sort_keys=True, separators=(",", ":")))
        if len(state["auth"]) == 1:
            self.server.requests.append(
                {"has_tool_result": False, "tool_result_hints": []}
            )
            config = state["config"]
            old = config.read_text(encoding="utf-8")
            if old.count('api-key: "fixture-upstream"') != 1:
                raise AssertionError("Unexpected disposable fixture configuration")
            original_write_text(
                config,
                old.replace('api-key: "fixture-upstream"', 'api-key: "fixture-local"'),
                encoding="utf-8",
            )
            deadline = time.monotonic() + 25
            while not state["selected_b"] and time.monotonic() < deadline:
                time.sleep(0.25)
                selection_probe()
            if not state["selected_b"]:
                raise AssertionError("Synthetic configuration reload was not observed")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            item = {"id": "synthetic_reasoning", "type": "reasoning", "summary": []}
            events = [
                {
                    "type": "response.created",
                    "response": {"id": "synthetic_first", "model": body.get("model")},
                },
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {**item, "encrypted_content": "synthetic-signature"},
                },
                {
                    "type": "error",
                    "error": {
                        "type": "server_error",
                        "code": "request_timeout",
                        "message": "stream error: stream disconnected before completion: stream closed before response.completed",
                    },
                },
            ]
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
            return
        return original_emit(self, body)

    sys.argv = [
        "probe_claude_stream_safety.py",
        "--proxy-binary",
        str(args.baseline.resolve()),
        "--codex-executor",
        "--codex-origin-binary",
        str(candidate),
        "--codex-managed-policy",
        "--case",
        "healthy",
        "--client-timeout",
        "90",
        "--file-output",
    ]
    with (
        patch.object(fixture, "isolated_environment", side_effect=enabled_environment),
        patch.object(Path, "write_text", new=fixture_write_text),
        patch.object(probe, "temporary_codex_translator", new=capture_origin),
        patch.object(probe.Handler, "emit_responses", new=emit),
    ):
        probe.main()

    auth = state["auth"]
    checks = {
        "fake_new_selection_observed": state["selected_b"],
        "initial_request_used_original": len(auth) >= 1
        and auth[0] == "Bearer fixture-upstream",
        "in_flight_retry_kept_original": len(auth) >= 2
        and auth[1] == "Bearer fixture-upstream",
        "subsequent_request_used_new_selection": len(auth) >= 3
        and auth[2] == "Bearer fixture-local",
        "request_body_preserved": len(state["bodies"]) >= 2
        and state["bodies"][0] == state["bodies"][1],
    }
    print(json.dumps({"mock_only": True, **checks}))
    if not all(checks.values()):
        raise AssertionError("Synthetic credential-selection invariant failed")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError):
        raise SystemExit(
            "Synthetic account-switch gate failed; raw output withheld"
        ) from None
