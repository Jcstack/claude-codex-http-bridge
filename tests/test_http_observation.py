"""HTTP-phase accounting and privacy with owned synthetic logs only."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
import launcher
import rolling_runtime


RUN = "9a8b7c6d5e4f3210"
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def line(phase="dispatch", *, run=RUN, attempt=1, request_class="compaction", **changes):
    values = {"phase": phase, "outcome": {"dispatch": "started", "headers": "returned", "decode": "ready"}[phase],
        "request_class": request_class, "route": "responses", "status": 0 if phase == "dispatch" else 200,
        "elapsed_ms": {"dispatch": 0, "headers": 2400, "decode": 4200}[phase],
        "phase_elapsed_ms": {"dispatch": 0, "headers": 2400, "decode": 1800}[phase],
        "error_kind": "none", "context_state": "active"}
    values.update(changes)
    return (f"[2026-01-02T12:00:00Z] [aabbccdd] [info] codex http observation: run={run} attempt={attempt} " +
            " ".join(f"{key}={value}" for key, value in values.items()) + "\n")


def summary(records):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "proxy.log"
        path.write_text("".join(records), encoding="utf-8")
        return diagnostics.proxy_summary(path, SINCE)


class HTTPObservationTests(unittest.TestCase):
    def test_phases_are_separate_and_private_identities_do_not_escape(self):
        report = summary([line(), line("headers"), line("decode")])
        phases = report["http_request_phases"]
        self.assertEqual(phases["phase_outcome_mentions"], {"dispatch.started": 1, "headers.returned": 1, "decode.ready": 1})
        row = phases["recent_attempts"][0]
        self.assertEqual(row["recorded_phases"], ["dispatch", "headers", "decode"])
        self.assertEqual((row["header_wait_ms"], row["decoder_setup_ms"], row["elapsed_ms"]), (2400, 1800, 4200))
        self.assertFalse(row["ambiguous"])
        self.assertIn("not response completion", phases["limits"])
        for secret in (RUN, "aabbccdd", "attempt=", "run="):
            self.assertNotIn(secret, json.dumps(report))

    def test_restart_identity_and_partial_window_are_not_merged(self):
        report = summary([line(), line("headers", run="123456789abcdef0"), line("decode", run="123456789abcdef0")])
        rows = report["http_request_phases"]["recent_attempts"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["recorded_phases"], ["dispatch"])
        self.assertFalse(rows[1]["dispatch_seen"])
        self.assertFalse(rows[1]["ambiguous"])
        self.assertEqual(rows[1]["header_wait_ms"], 2400)

    def test_ambiguous_sequences_do_not_publish_paired_timings(self):
        for records in (
            [line(), line("headers"), line("headers"), line("decode")],
            [line(), line("decode"), line("headers")],
            [line(), line("headers", request_class="main"), line("decode")],
            [line(), line("headers", status=503), line("decode")],
            [line(), line("headers", outcome="error", error_kind="other"), line("decode")],
        ):
            with self.subTest(records=len(records)):
                row = summary(records)["http_request_phases"]["recent_attempts"][0]
                self.assertTrue(row["ambiguous"])
                for key in ("header_wait_ms", "decoder_setup_ms", "elapsed_ms", "status"):
                    self.assertIsNone(row[key])

    def test_cancellation_errors_are_retained_separately_from_healthy_controls(self):
        records = [line(), line("headers", status=0, outcome="error", error_kind="cancelled", context_state="cancelled")]
        for attempt in range(2, 40):
            records.extend([line(attempt=attempt), line("headers", attempt=attempt), line("decode", attempt=attempt)])
        phases = summary(records)["http_request_phases"]
        self.assertEqual(len(phases["recent_errors"]), 1)
        self.assertEqual(phases["recent_errors"][0]["context_state"], "cancelled")
        self.assertEqual(len(phases["recent_attempts"]), 12)
        self.assertEqual(phases["phase_outcome_mentions"]["headers.error"], 1)

    def test_invalid_and_untrusted_metadata_are_rejected(self):
        invalid = [line("headers", request_class="PRIVATE_SECRET"), line(attempt=2**64),
            line("headers", status=999), line("headers", elapsed_ms=86400001),
            line("decode", phase_elapsed_ms=9999), line("decode", outcome="started"),
            line("headers", outcome="error"), line("decode", error_kind="PRIVATE_SECRET"),
            line().rstrip() + " PRIVATE_SECRET\n"]
        phases = summary(invalid)["http_request_phases"]
        self.assertEqual(phases["invalid_records"], len(invalid))
        self.assertFalse(phases["recent_attempts"])
        self.assertNotIn("PRIVATE_SECRET", json.dumps(phases))

    def test_http_refusal_is_retained_even_when_client_do_returns_normally(self):
        records = [line(), line("headers", status=503), line("decode", status=503)]
        for attempt in range(2, 20):
            records.extend([line(attempt=attempt), line("headers", attempt=attempt), line("decode", attempt=attempt)])
        phases = summary(records)["http_request_phases"]
        self.assertEqual(len(phases["recent_errors"]), 1)
        refusal = phases["recent_errors"][0]
        self.assertEqual((refusal["status"], refusal["outcome"], refusal["error_kind"]), (503, "returned", "none"))

    def test_attempt_state_and_error_history_are_bounded(self):
        records = [line(attempt=i) for i in range(1, 301)]
        phases = summary(records)["http_request_phases"]
        self.assertEqual(phases["attempt_limit"], 256)
        self.assertEqual(phases["attempts_evicted"], 44)
        self.assertEqual(len(phases["recent_attempts"]), 12)
        self.assertFalse(summary([])["http_request_phases"]["phase_outcome_mentions"])

    def test_managed_dispatch_without_access_or_completion_records_is_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = "g-" + "b" * 24
            directory = root / "runtime-managed" / generation
            directory.mkdir(parents=True)
            (directory / "proxy.log").write_text(line(), encoding="utf-8")
            with patch.object(rolling_runtime, "state", return_value={"pid": 123, "port": 12345, "current": generation}), \
                 patch.object(launcher, "listener_pids", return_value={123}):
                report = diagnostics.collect(root, "subscription", [], SINCE, include_sessions=False)
            backends = report["managed"]["backends"]
            self.assertEqual(len(backends), 1)
            self.assertEqual(backends[0]["http_request_phases"]["phase_outcome_mentions"], {"dispatch.started": 1})
            self.assertNotIn(RUN, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
