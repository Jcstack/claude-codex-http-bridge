"""Privacy and incident-accounting regressions using synthetic private logs."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
import launcher


class DiagnosticsTests(unittest.TestCase):
    def test_metadata_allowlist_drops_bodies_paths_and_untrusted_values(self):
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "proxy.log"
            log.write_text(
                '[2026-01-02 12:00:00] [12345678] [info] [gin.go:1] 200 | 9m49s | 127.0.0.1 | POST "/v1/messages?key=PRIVATE_SECRET"\n'
                '[2026-01-02 12:00:01] [12345678] [error] TLS handshake EOF credential PRIVATE_SECRET\n'
                '[2026-01-02 12:00:02] [12345678] [info] [gin.go:1] 503 | 2ms | 127.0.0.1 | POST "/private/PRIVATE_SECRET"\n',
                encoding="utf-8")
            report = diagnostics.proxy_summary(log, since)
            self.assertEqual(report["completed_http_statuses"], {"200": 1, "503": 1})
            self.assertEqual(report["latency_seconds"]["max"], 589)
            self.assertEqual(report["transport_error_mentions"], {"tls_handshake": 1, "eof": 1})
            self.assertEqual(report["failed_requests"][0]["endpoint"], "other")
            transcript = root / "private-session-name.jsonl"
            records = [
                {"type": "api-request", "timestamp": "2026-01-02T12:00:00Z",
                 "params": {"model": "gpt-6.1-sol", "output_config": {"effort": "max"},
                            "messages": [{"content": "PRIVATE_SECRET"}]}},
                {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-02T12:09:49Z",
                 "cwd": "/private/PRIVATE_SECRET", "compactMetadata": {
                     "durationMs": 589457, "preTokens": 366956, "postTokens": 35307,
                     "trigger": "auto", "preservedMessages": {"secret": "PRIVATE_SECRET"}}},
                {"type": "assistant", "timestamp": "2026-01-02T12:10:00Z", "isApiErrorMessage": True,
                 "message": {"content": "PRIVATE_SECRET"}},
                {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-02T12:11:00Z",
                 "compactMetadata": {"durationMs": "PRIVATE_SECRET", "preTokens": True,
                                     "postTokens": float("nan"), "trigger": "PRIVATE_SECRET"}},
            ]
            transcript.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            session = diagnostics.session_summary(transcript, since)
            self.assertEqual(session["compaction_count"], 2)
            self.assertEqual(session["compaction_seconds_median"], 589.457)
            self.assertEqual(session["api_error_messages"], 1)
            self.assertIsNone(session["recent_compactions"][-1]["before_tokens"])
            output = json.dumps([report, session])
            for private in ("PRIVATE_SECRET", "private-session-name", "127.0.0.1", "preservedMessages"):
                self.assertNotIn(private, output)

    def test_cache_accounting_deduplicates_stream_blocks_and_rejects_invalid_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "session.jsonl"
            records = []
            for message_id, usage in [
                ("PRIVATE_ID", {"input_tokens": 10, "cache_read_input_tokens": 20}),
                ("PRIVATE_ID", {"input_tokens": 12, "cache_read_input_tokens": 30}),
                ("other", {"input_tokens": 3, "cache_read_input_tokens": True, "cache_creation_input_tokens": 5}),
                ("other", {"input_tokens": -1, "cache_read_input_tokens": "PRIVATE_SECRET"}),
            ]:
                records.append({"type": "assistant", "timestamp": "2026-01-02T12:00:00Z",
                                "message": {"id": message_id, "usage": usage, "content": "PRIVATE_SECRET"}})
            transcript.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            report = diagnostics.session_summary(transcript, datetime(2026, 1, 1, tzinfo=timezone.utc))
            usage = report["cache_usage"]
            self.assertEqual(usage["total_input_tokens"], 50)
            self.assertEqual(usage["cache_read_fraction"], .6)
            self.assertEqual(usage["responses_with_usage"], 2)
            self.assertNotIn("PRIVATE", json.dumps(report))

    def test_collection_does_not_create_runtime_or_start_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "missing"
            with patch.object(launcher, "ensure_service", side_effect=AssertionError("must not start")):
                report = diagnostics.collect(root, "subscription", [root / "claude"],
                                             datetime.now(timezone.utc))
            self.assertFalse(report["service"]["recorded"])
            self.assertFalse(root.exists())

    def test_large_blob_does_not_hide_next_metadata_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.jsonl"
            path.write_text("x" * 1000 + "\n" + '{"type":"safe"}\n', encoding="utf-8")
            with patch.object(diagnostics, "LINE_LIMIT", 64):
                self.assertEqual(list(diagnostics.lines(path)), ['{"type":"safe"}\n'])
        self.assertEqual(diagnostics.duration("1h2m3.5s"), 3723.5)
        self.assertAlmostEqual(diagnostics.duration("800µs"), .0008)
        self.assertIsNone(diagnostics.duration("private-input"))


if __name__ == "__main__":
    unittest.main()
