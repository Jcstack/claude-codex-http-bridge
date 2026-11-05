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
    def test_overload_category_does_not_export_error_content_or_infer_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            write_records(path, [{"type": "assistant", "isApiErrorMessage": True,
                                  "message": {"content": [{"type": "text", "text":
                                      "API Error: Our servers are currently overloaded. PRIVATE_SECRET"}]}}])
            report = audit.scan(path, None, audit.Usage())
            self.assertEqual(report["api_error_category_mentions"], {"server_overloaded": 1})
            self.assertEqual(report["api_error_status_mentions"], {})
            self.assertNotIn("PRIVATE", json.dumps(report))
            self.assertEqual(audit.error_labels("server_is_overloaded"), ["server_overloaded"])

    def test_recent_thinking_counts_only_metadata_after_last_request_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "PRIVATE_SESSION.jsonl"
            def request(at):
                return {"timestamp": at, "type": "api-request", "params": {
                    "model": "gpt-6-astra", "output_config": {"effort": "xhigh"}}}
            def thinking(at, text, signature):
                return {"timestamp": at, "type": "assistant", "message": {"id": "PRIVATE_ID", "content": [
                    {"type": "thinking", "thinking": text, "signature": signature}]}}
            older, newer, later = "2026-01-02T12:00:00Z", "2026-01-02T12:01:00Z", "2026-01-02T12:02:00Z"
            write_records(path, [request(older), thinking(older, "", "PRIVATE_OLD_SIGNATURE"), request(newer),
                thinking(newer, "", "PRIVATE_SIGNATURE"), thinking(later, "", ""),
                thinking(later, "PRIVATE_THOUGHT", "PRIVATE_SIGNATURE"), thinking(later, None, "PRIVATE_SIGNATURE"),
                thinking(older, "", "PRIVATE_STALE"),
                {**thinking(later, "", "PRIVATE_ERROR"), "isApiErrorMessage": True}])
            report = audit.scan(path, None, audit.Usage())
            self.assertEqual(report["thinking_after_last_request_marker"], {
                "recorded_blocks": 4, "empty_text_blocks": 2, "empty_text_with_signature": 1,
                "nonempty_text_blocks": 1, "invalid_text_blocks": 1,
                "last_block_at": "2026-01-02T12:02:00+00:00"})
            self.assertNotIn("PRIVATE", json.dumps(report))

    def test_counter_patterns_are_bounded_metadata_not_duplicate_detection(self):
        timeline = audit.CacheCounterPatterns()
        def add(ident, value, model="PRIVATE_MODEL"):
            timeline.add({"id": ident, "model": model, "usage": {"cache_read_input_tokens": value}})
        add("PRIVATE_A", 10000)
        add("PRIVATE_A", 9000)
        add("PRIVATE_B", 20000)
        add("PRIVATE_C", 20050)
        add("PRIVATE_D", 19000)
        add("PRIVATE_E", 22000)
        timeline.compact_boundary()
        add("PRIVATE_F", 21000)
        add("PRIVATE_G", 42000, "PRIVATE_OTHER_MODEL")
        timeline.add({"id": "PRIVATE_INVALID", "usage": {"cache_read_input_tokens": True}})
        timeline.add({"usage": {"cache_read_input_tokens": 123}})
        report = timeline.report()
        self.assertEqual(report["responses_with_valid_cache_read"], 7)
        self.assertEqual(report["near_double_pairs"], 2)
        self.assertEqual(report["same_recorded_model_near_double_pairs"], 1)
        self.assertEqual(report["near_double_pairs_with_three_similar_followups"], 1)
        self.assertEqual(report["usage_records_without_valid_cache_read"], 1)
        self.assertEqual(report["records_without_valid_identity"], 1)
        self.assertIn("do not prove duplicated input", report["limits"])
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_counter_pattern_ambiguous_epoch_model_and_identity_limit(self):
        timeline = audit.CacheCounterPatterns()
        def add(ident, value, model):
            timeline.add({"id": ident, "model": model, "usage": {"cache_read_input_tokens": value}})
        with patch.object(audit, "IDENTITY_LIMIT", 2):
            add("one", 10000, "model-a")
            add("two", 20000, "model-a")
            add("two", 20000, "model-b")
            timeline.compact_boundary()
            add("one", 10000, "model-a")
            add("three", 40000, "model-a")
        report = timeline.report()
        self.assertEqual(report["ambiguous_epoch_responses"], 1)
        self.assertEqual(report["ambiguous_model_responses"], 1)
        self.assertEqual(report["omitted_records_at_limit"], 1)
        self.assertEqual(report["same_recorded_model_near_double_pairs"], 0)

    def test_scan_measures_only_retained_schema_sizes_without_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "PRIVATE_SESSION.jsonl"
            schema = {"type": "object", "properties": {"secret": {"description": "PRIVATE_SECRET"}}}
            write_records(path, [{"type": "attachment", "attachment": {"type": "deferred_tools_record", "entries": [
                {"name": "mcp__PRIVATE__tool", "input_schema": schema}, {"name": "PRIVATE_INCOMPLETE"}]}},
                {"type": "attachment", "attachment": {"type": "deferred_tools_record", "entries": "invalid"}}])
            result = audit.scan(path, None, audit.Usage())
            measured = result["retained_deferred_schema_sizes"]
            self.assertEqual(measured["deferred_records"], 2)
            self.assertEqual(measured["entries"], 2)
            self.assertEqual(measured["entries_with_input_schema"], 1)
            self.assertEqual(measured["unsupported_or_incomplete"], 2)
            self.assertEqual(measured["largest_input_schema_utf8_bytes"],
                             len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
            self.assertIn("not a current server inventory", measured["limits"])
            self.assertNotIn("PRIVATE", json.dumps(result))

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

    def test_disabled_session_tool_error_is_bounded_deduplicated_and_private(self):
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
            phrase = ("Error: No such tool available: PRIVATE_TOOL. PRIVATE_TOOL is still listed for this conversation "
                      "but is switched off in this session right now, so it cannot run. PRIVATE_SECRET")
            duplicate = pair("one", "PRIVATE_TOOL", phrase)
            records.append(duplicate)
            pair("two", "Read", [{"type": "text", "text": phrase}])
            pair("three", "PRIVATE_TOOL", phrase.replace("PRIVATE_TOOL is still", "OTHER_TOOL is still"))
            pair("four", "PRIVATE_TOOL", "x" * (audit.TOOL_ERROR_TEXT_LIMIT + 1) + phrase)
            write_records(path, records)
            report = audit.scan(path, None, audit.Usage())
            self.assertEqual(report["tool_error_categories"], {"tool_unavailable_session_disabled": 2})
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
