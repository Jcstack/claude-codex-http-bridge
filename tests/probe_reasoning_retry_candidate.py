"""Disposable two-layer real-Claude experiment; never uses an account.

Both binaries and every service are test-only. The candidate is intentionally
not authorized for installation or use against a real account.
"""

import argparse
import json
from pathlib import Path
import sys
import time
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


def fixture_keepalive_write(path, data, *args, **kwargs):
    if path.name == "config.yaml" and path.parent.name.startswith(
        "bridge-codex-stream-fixture-"
    ):
        data += "\nrequests:\n  streaming:\n    keepalive-seconds: 1\n    send-keepalive-before-ft: true\n"
    return fixture_write_text(path, data, *args, **kwargs)


def emit_with_reasoning_failure(self, body, *, failure="incomplete", timing=None):
    records = getattr(self.server, "synthetic_bodies", None)
    if records is None:
        records = []
        self.server.synthetic_bodies = records
    records.append(json.dumps(body, sort_keys=True, separators=(",", ":")))
    if timing is not None and len(records) in (1, 2):
        timing[len(records)] = time.monotonic()
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
        error = (
            {
                "type": "service_unavailable_error",
                "code": "server_is_overloaded",
                "message": "synthetic overload",
            }
            if failure == "overload"
            else {
                "type": "server_error",
                "code": "request_timeout",
                "message": "stream error: stream disconnected before completion: stream closed before response.completed",
            }
        )
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
                "error": error,
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
    parser.add_argument(
        "--origin",
        action="store_true",
        help="Put the test candidate at the credential-owning hop",
    )
    parser.add_argument(
        "--failure", choices=("incomplete", "overload"), default="incomplete"
    )
    parser.add_argument("--real-wait", action="store_true")
    args = parser.parse_args()
    if args.real_wait and (not args.origin or args.failure != "overload"):
        parser.error("--real-wait requires --origin --failure overload")
    outer = args.baseline if args.origin else args.candidate
    inner = args.candidate if args.origin else args.baseline
    sys.argv = [
        "probe_claude_stream_safety.py",
        "--proxy-binary",
        str(outer.resolve()),
        "--codex-executor",
        "--codex-origin-binary",
        str(inner.resolve()),
        "--codex-managed-policy",
        "--case",
        "healthy",
        "--client-timeout",
        "90" if args.real_wait else "60",
        "--file-output",
    ]

    def environment(root):
        env = enabled_environment(root)
        if args.real_wait:
            env["BRIDGE_SYNTHETIC_REAL_BACKOFF"] = "1"
        return env

    timing = {}
    with (
        patch.object(fixture, "isolated_environment", side_effect=environment),
        patch.object(
            Path,
            "write_text",
            new=fixture_keepalive_write if args.real_wait else fixture_write_text,
        ),
        patch.object(
            probe.Handler,
            "emit_responses",
            lambda handler, body: emit_with_reasoning_failure(
                handler, body, failure=args.failure, timing=timing
            ),
        ),
    ):
        probe.main()
    if args.real_wait:
        elapsed = timing.get(2, 0) - timing.get(1, 0)
        if not 29.5 <= elapsed < 90:
            raise AssertionError(
                "Synthetic client did not observe the bounded real delay"
            )
        print(
            json.dumps(
                {
                    "mock_only": True,
                    "real_client": True,
                    "elapsed_seconds": round(elapsed, 1),
                }
            )
        )


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError):
        raise SystemExit(
            "Synthetic reasoning retry client gate failed; raw output withheld"
        ) from None
