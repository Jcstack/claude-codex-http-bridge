"""Disposable two-layer real-Claude experiment; never uses an account.

Both binaries and every service are test-only. The candidate is intentionally
not authorized for installation or use against a real account.
"""

import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
import codex_stream_fixture as fixture  # noqa: E402
import probe_claude_stream_safety as probe  # noqa: E402

initial_env = fixture.isolated_environment


def enabled_environment(root):
    result = initial_env(root)
    result["BRIDGE_SYNTHETIC_REASONING_RETRY"] = "1"
    return result


original_emit = probe.Handler.emit_responses
original_write_text = Path.write_text


def fixture_write_text(path, data, *args, **kwargs):
    if path.name == "config.yaml" and path.parent.name.startswith(
        "bridge-codex-stream-fixture-"
    ):
        data += '\nmultimedia:\n  disable-image-generation: "passthrough"\n'
        if "\nrouting:" not in data:
            data += "\nrouting:\n  retry:\n    request-retry: 2\n  cooldown:\n    transient-error-cooldown-seconds: -1\n"
    return original_write_text(path, data, *args, **kwargs)


def emit_with_reasoning_failure(self, body):
    records = getattr(self.server, "synthetic_bodies", None)
    if records is None:
        records = []
        self.server.synthetic_bodies = records
    records.append(json.dumps(body, sort_keys=True, separators=(",", ":")))
    if len(records) == 1:
        print(
            json.dumps(
                {
                    "synthetic_tool_types": sorted(
                        {
                            x.get("type", "missing")
                            for x in body.get("tools", [])
                            if isinstance(x, dict)
                        }
                    ),
                    "synthetic_tool_choice": body.get("tool_choice"),
                }
            ),
            flush=True,
        )
        self.server.requests.append({"has_tool_result": False, "tool_result_hints": []})
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
    if len(records) == 2 and records[0] != records[1]:
        raise AssertionError("Synthetic retry changed the origin request")
    return original_emit(self, body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("baseline", type=Path)
    args = parser.parse_args()
    sys.argv = [
        "probe_claude_stream_safety.py",
        "--proxy-binary",
        str(args.candidate.resolve()),
        "--codex-executor",
        "--codex-origin-binary",
        str(args.baseline.resolve()),
        "--codex-managed-policy",
        "--case",
        "healthy",
        "--client-timeout",
        "60",
        "--file-output",
    ]
    with (
        patch.object(fixture, "isolated_environment", side_effect=enabled_environment),
        patch.object(Path, "write_text", new=fixture_write_text),
        patch.object(probe.Handler, "emit_responses", emit_with_reasoning_failure),
    ):
        probe.main()


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError):
        raise SystemExit(
            "Synthetic reasoning retry client gate failed; raw output withheld"
        ) from None
