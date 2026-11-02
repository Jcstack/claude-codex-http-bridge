"""Lifecycle coverage and privacy regressions using synthetic session trees."""

from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
import launcher
import session_audit as audit


AT = "2026-01-02T12:00:00Z"
SESSION = "00000000-0000-0000-0000-000000000001"


def write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"timestamp": AT, **record}) + "\n" for record in records), encoding="utf-8")


class SessionAuditTests(unittest.TestCase):
    def test_child_coverage_cross_file_usage_dedup_and_allowlisted_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = root / "projects" / "PRIVATE_PROJECT" / (SESSION + ".jsonl")
            usage = {"input_tokens": 10, "cache_read_input_tokens": 90,
                     "cache_creation_input_tokens": 0, "output_tokens": 5}
            answer = {"type": "assistant", "message": {"id": "PRIVATE_RESPONSE", "usage": usage,
                      "content": [{"type": "tool_use", "id": "PRIVATE_TOOL_ID", "name": "Read",
                                   "input": {"path": "/PRIVATE_SECRET"}}]}}
            write_records(main, [
                {"type": "api-request", "params": {"model": "gpt-6.1-sol", "output_config": {"effort": "max"}}},
                answer, answer,
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "PRIVATE_TOOL_ID", "is_error": True, "content": "PRIVATE_SECRET"}]}},
                {"type": "user", "message": {"content": "<command-name>/model</command-name>PRIVATE_SECRET"}},
                {"type": "system", "subtype": "compact_boundary", "compactMetadata": {
                    "trigger": "auto", "durationMs": 527000, "preTokens": 700000, "postTokens": 30000}},
                {"type": "assistant", "isApiErrorMessage": True, "message": {
                    "content": [{"type": "text", "text": "API Error: 429 PRIVATE_SECRET"}]}}
            ])
            child = main.parent / main.stem / "subagents" / "agent-PRIVATE_ID.jsonl"
            write_records(child, [answer, {"type": "assistant", "message": {"id": "PRIVATE_CHILD_RESPONSE",
                "usage": {"input_tokens": 20, "cache_read_input_tokens": 180}, "content": [
                    {"type": "tool_use", "id": "PRIVATE_MCP_ID", "name": "mcp__PRIVATE_SECRET__tool"},
                    {"type": "tool_use", "id": "PRIVATE_UNKNOWN_ID", "name": "PRIVATE_SECRET"}]}},
                {"type": "api-request", "params": {"model": "/PRIVATE_SECRET", "output_config": {"effort": ["PRIVATE_SECRET"]}}}
            ])
            report = audit.collect([root], session_tag=audit.tag(SESSION), include_children=True)
            self.assertEqual(report["selection_status"], "selected")
            entry = report["sessions"][0]
            self.assertEqual(entry["child_logs_discovered"], 1)
            self.assertEqual(entry["main"]["tool_calls"], {"Read": 1})
            self.assertEqual(entry["main"]["tool_error_results"], {"Read": 1})
            self.assertEqual(entry["main"]["api_error_status_mentions"], {"429": 1})
            self.assertEqual(entry["main"]["command_record_mentions"], {"model": 1})
            self.assertEqual(entry["main"]["compaction_seconds_median"], 527)
            self.assertEqual(entry["children"][0]["tool_calls"], {"Read": 1, "mcp": 1, "other": 1})
            usage_report = report["combined_cache_usage"]
            self.assertEqual(usage_report["responses_with_usage"], 2)
            self.assertEqual(usage_report["reported_total_input_tokens"], 300)
            self.assertEqual(usage_report["cache_read_fraction_of_reported_input"], .9)
            self.assertEqual(usage_report["responses_missing_counter"]["cache_creation_input_tokens"], 1)
            for private in ("PRIVATE", SESSION, str(root), "input_text", "Bearer"):
                self.assertNotIn(private, json.dumps(report))

    def test_snapshot_does_not_consume_appends_and_reports_growth(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "log.jsonl"
            write_records(path, [{"type": "user", "message": {"content": "PRIVATE"}}])
            coverage = dict.fromkeys(("bytes_read", "lines_seen", "oversized_lines", "records_parsed", "malformed_lines", "nonobject_records"), 0)
            iterator = audit.records(path, coverage)
            self.assertEqual(next(iterator)["type"], "user")
            with path.open("ab") as handle:
                handle.write(b'{"type":"PRIVATE_APPENDED"}\n')
            self.assertEqual(list(iterator), [])
            self.assertTrue(coverage["changed_during_scan"])
            self.assertEqual(coverage["bytes_read"], coverage["snapshot_bytes"])

    def test_task_stop_ownership_error_is_bounded_deduplicated_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "PRIVATE_SESSION.jsonl"
            records = []
            def pair(tool_id, name, content):
                records.append({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": tool_id, "name": name, "input": {"PRIVATE": "PRIVATE_SECRET"}}]}})
                result = {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "is_error": True, "content": content}]}}
                records.append(result)
                return result
            repeated = pair("one", "TaskStop", "Task PRIVATE_ID is owned by PRIVATE_OWNER; agent PRIVATE_AGENT cannot stop it. PRIVATE_SECRET")
            records.append(repeated)
            pair("two", "TaskStop", [{"type": "text", "text": "Other PRIVATE_SECRET failure"}])
            pair("three", "Read", "owned by agent PRIVATE_AGENT cannot stop PRIVATE_SECRET")
            pair("four", "TaskStop", "x" * (audit.TOOL_ERROR_TEXT_LIMIT + 1) + " owned by agent cannot stop")
            write_records(path, records)
            report = audit.scan(path, None, audit.Usage())
            self.assertEqual(report["tool_error_results"], {"TaskStop": 3, "Read": 1})
            self.assertEqual(report["tool_error_categories"], {"task_stop_ownership_denial": 1,
                                                               "task_stop_other_or_unclassified": 2})
            self.assertEqual(report["coverage"]["tool_error_text_truncated"], 1)
            self.assertNotIn("PRIVATE", json.dumps(report))

    def test_oversized_corrupt_and_partial_records_have_explicit_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "log.jsonl"
            valid = json.dumps({"type": "system", "timestamp": AT}).encode() + b"\n"
            path.write_bytes(b"x" * 300 + b"\n{broken}\n123\n\xff\n" + valid + b'{"partial":')
            with patch.object(audit, "LINE_LIMIT", 128):
                report = audit.scan(path, None, audit.Usage())
            coverage = report["coverage"]
            self.assertEqual(coverage["oversized_lines"], 1)
            self.assertEqual(coverage["malformed_lines"], 3)
            self.assertEqual(coverage["nonobject_records"], 1)
            self.assertEqual(coverage["records_parsed"], 1)
            self.assertTrue(coverage["unterminated_tail"])
            self.assertFalse(coverage["read_failed"])
            self.assertNotIn("partial", json.dumps(report))

    def test_usage_limits_invalid_counters_and_missing_fields_are_not_hidden(self):
        usage = audit.Usage()
        with patch.object(audit, "IDENTITY_LIMIT", 1):
            usage.add({"id": "one", "usage": {"input_tokens": True, "cache_read_input_tokens": 40}})
            usage.add({"id": "one", "usage": {"input_tokens": 10, "cache_read_input_tokens": 30, "output_tokens": -1}})
            usage.add({"id": "two", "usage": {"input_tokens": 999}})
            usage.add({"usage": {"input_tokens": 999}})
        report = usage.report()
        self.assertEqual(report["reported_total_input_tokens"], 50)
        self.assertEqual(report["omitted_usage_records_at_limit"], 1)
        self.assertEqual(report["usage_records_without_valid_identity"], 1)
        self.assertEqual(report["responses_missing_counter"]["cache_creation_input_tokens"], 1)

    def test_target_missing_or_ambiguous_never_falls_back_to_other_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_records(root / "projects" / "one" / (SESSION + ".jsonl"), [])
            report = audit.collect([root], session_tag="a" * 12)
            self.assertEqual(report["selection_status"], "target_missing")
            write_records(root / "projects" / "two" / (SESSION + ".jsonl"), [])
            report = audit.collect([root], session=SESSION)
            self.assertEqual(report["selection_status"], "target_ambiguous")
            self.assertEqual(report["sessions"], [])

    def test_children_are_bounded_and_other_workspaces_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = root / "projects" / "one" / (SESSION + ".jsonl")
            write_records(main, [])
            for index in range(3):
                write_records(main.parent / main.stem / "subagents" / f"agent-{index}.jsonl", [])
            write_records(root / "projects" / "other" / "unrelated.jsonl", [])
            with patch.object(audit, "CHILD_LIMIT", 2):
                report = audit.collect([root], session=SESSION, include_children=True)
            self.assertEqual(report["matching_main_logs"], 1)
            entry = report["sessions"][0]
            self.assertEqual(len(entry["children"]), 2)
            self.assertEqual(entry["child_logs_omitted_at_limit"], 1)

    def test_symlinked_child_directory_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = root / "projects" / "one" / (SESSION + ".jsonl")
            write_records(main, [])
            outside = root / "outside"
            write_records(outside / "private.jsonl", [{"type": "user"}])
            link = main.parent / main.stem / "subagents"
            link.parent.mkdir()
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks is unavailable")
            report = audit.collect([root], session=SESSION, include_children=True)
            self.assertEqual(report["sessions"][0]["unsafe_child_paths_skipped"], 1)
            self.assertEqual(report["sessions"][0]["children"], [])

    def test_time_window_and_cli_remain_read_only_without_a_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "claude"
            main = config / "projects" / "one" / (SESSION + ".jsonl")
            write_records(main, [{"type": "user"}, {"timestamp": "not a date", "type": "user"}])
            report = audit.scan(main, datetime(2026, 1, 3, tzinfo=timezone.utc), audit.Usage())
            self.assertEqual(report["coverage"]["in_scope_records"], 0)
            self.assertEqual(report["coverage"]["records_missing_timestamp"], 1)
            output = StringIO()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}), patch("sys.stdout", output), \
                 patch.object(launcher, "ensure_service", side_effect=AssertionError("must not start")):
                code = diagnostics.main(["--root", str(root / "missing"), "--lifetime", "--include-children", "--session", SESSION])
            self.assertEqual(code, 0)
            self.assertFalse((root / "missing").exists())
            self.assertEqual(json.loads(output.getvalue())["session_audit"]["time_scope"], "lifetime")

    def test_undated_metadata_is_included_only_in_lifetime_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "log.jsonl"
            path.write_text(json.dumps({"type": "assistant", "message": {"id": "PRIVATE", "usage": {"input_tokens": 7}}}) + "\n")
            lifetime = audit.scan(path, None, audit.Usage())
            window = audit.scan(path, datetime(2026, 1, 1, tzinfo=timezone.utc), audit.Usage())
            self.assertEqual(lifetime["cache_usage"]["input_tokens"], 7)
            self.assertEqual(window["cache_usage"]["input_tokens"], 0)
            self.assertIsNone(lifetime["first_activity"])
            self.assertEqual(lifetime["coverage"]["records_missing_timestamp"], 1)

    def test_private_target_validation_and_no_fallback_on_bad_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "runtime-audit" / "target.json"
            path.parent.mkdir(mode=0o700)
            path.write_text(json.dumps({"format": 1, "session_tag": audit.tag(SESSION), "private_extra": "PRIVATE_SECRET"}))
            if os.name == "nt":
                import windows_acl
                windows_acl.lock([path.parent, path])
            else:
                path.chmod(0o600)
            self.assertEqual(audit.tracked_tag(root), audit.tag(SESSION))
            path.write_text(json.dumps({"format": True, "session_tag": audit.tag(SESSION)}))
            with self.assertRaises(RuntimeError):
                audit.tracked_tag(root)
            output = StringIO()
            with patch("sys.stdout", output), patch.object(launcher, "ensure_service", side_effect=AssertionError("must not start")):
                code = diagnostics.main(["--root", str(root), "--lifetime", "--tracked"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["read_only"], True)
            self.assertNotIn(str(root), output.getvalue())

    def test_stream_failure_is_classified_without_exporting_its_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "log.jsonl"
            write_records(path, [{"type": "assistant", "isApiErrorMessage": True, "message": {"content": [
                {"type": "text", "text": "API Error: stream disconnected before completion: stream closed before response.completed PRIVATE_SECRET"}]}}])
            report = audit.scan(path, None, audit.Usage())
            self.assertEqual(report["api_error_category_mentions"], {"incomplete_stream": 1})
            self.assertEqual(report["last_api_error_records"][0]["status_mentions"], [])
            self.assertNotIn("PRIVATE_SECRET", json.dumps(report))

    def test_incomplete_tool_guard_has_a_separate_sanitized_category(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "log.jsonl"
            write_records(path, [{"type": "assistant", "isApiErrorMessage": True, "message": {"content": [
                {"type": "text", "text": "Upstream ended before a client tool call was complete. PRIVATE_SECRET"}]}}])
            report = audit.scan(path, None, audit.Usage())
            self.assertEqual(report["api_error_category_mentions"], {"incomplete_tool_call": 1})
            self.assertNotIn("PRIVATE_SECRET", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
