"""Privacy and incident-accounting regressions using synthetic private logs."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
import launcher
import rolling_runtime


class DiagnosticsTests(unittest.TestCase):
    def test_buffered_observation_has_no_invented_gap_and_no_raw_reason(self):
        line = ('codex stream observation: phase=buffered outcome=terminal_event '
                'last_event=response.failed data_items=2 elapsed_ms=1200 '
                'read_error=none failure_kind=reported_overload')
        row = diagnostics.stream_observation(line)
        self.assertEqual(row['phase'], 'buffered')
        self.assertEqual(row['failure_kind'], 'reported_overload')
        self.assertEqual(row['elapsed_ms'], 1200)
        for key in ('since_read_ms', 'event_gap_ms', 'max_read_gap_ms', 'prior_event'):
            self.assertIsNone(row[key])
        self.assertIsNone(diagnostics.stream_observation(line.replace('reported_overload', 'private_secret')))
        self.assertIsNone(diagnostics.stream_observation(line.replace('data_items=', 'since_read_ms=10 data_items=')))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'PRIVATE.log'
            path.write_text('[2026-01-02T12:00:00Z] [aabbccdd] ' + line + '\n'
                            '[2026-01-02T12:00:00Z] [aabbccdd] [gin.go:1] 502 | 2s | PRIVATE | POST "/v1/messages"\n',
                            encoding='utf-8')
            report = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            observed = report['stream_termination_observations']
            self.assertEqual(observed['max_read_gap_buckets_by_outcome'], {})
            failure, = observed['recent_noncompletion']
            self.assertEqual(failure['same_log_match'], 'unique')
            self.assertEqual(failure['http_status'], 502)
            for private in ('aabbccdd', 'PRIVATE', str(path)):
                self.assertNotIn(private, json.dumps(report))

    def test_reported_failure_kind_is_optional_for_old_stream_observations(self):
        line = ('codex stream observation: phase=stream outcome=terminal_event '
                'last_event=error data_items=2 elapsed_ms=1000 since_read_ms=0 read_error=none')
        self.assertIsNone(diagnostics.stream_observation(line)['failure_kind'])
        for kind in diagnostics.STREAM_FAILURE_KINDS:
            self.assertEqual(diagnostics.stream_observation(line + ' failure_kind=' + kind)['failure_kind'], kind)
        self.assertIsNone(diagnostics.stream_observation(line + ' failure_kind=private_secret'))
        completed = line.replace('terminal_event', 'completion_event').replace('last_event=error', 'last_event=response.completed')
        self.assertIsNone(diagnostics.stream_observation(completed + ' failure_kind=reported_auth'))

    def test_completion_observations_are_controls_not_http_success(self):
        line = ('codex stream observation: phase=stream outcome=completion_event '
                'last_event=response.completed prior_event=response.output_text.delta '
                'data_items=3 elapsed_ms=41000 since_read_ms=0 '
                'event_gap_ms=40000 max_read_gap_ms=40000 read_error=none')
        report = diagnostics.stream_observation(line)
        self.assertEqual(report['outcome'], 'completion_event')
        self.assertEqual(report['max_read_gap_ms'], 40000)
        self.assertNotIn('http_status', report)
        self.assertIsNotNone(diagnostics.stream_observation(line.replace('response.completed', 'response.done')))
        self.assertIsNone(diagnostics.stream_observation(line.replace('response.completed', 'response.incomplete')))
        self.assertIsNone(diagnostics.stream_observation(line.replace('read_error=none', 'read_error=timeout')))

    def test_read_gap_comparison_retains_failures_after_recent_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'PRIVATE.log'
            prefix = '[2026-01-02 12:00:00] [info] codex stream observation: phase=stream '
            suffix = ('prior_event=response.created data_items=2 elapsed_ms=45000 '
                      'since_read_ms=0 event_gap_ms=40000 max_read_gap_ms=40000 read_error=none\n')
            path.write_text(prefix + 'outcome=eof last_event=response.created ' + suffix +
                            (prefix + 'outcome=completion_event last_event=response.completed ' + suffix) * 20 +
                            prefix + 'outcome=eof last_event=response.created '
                            'data_items=1 elapsed_ms=1 since_read_ms=0 read_error=none\n', encoding='utf-8')
            report = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            observed = report['stream_termination_observations']
            self.assertEqual(observed['max_read_gap_buckets_by_outcome'], {
                'eof': {'30_to_under_60s': 1}, 'completion_event': {'30_to_under_60s': 20}})
            self.assertEqual(observed['outcome_mentions'], {'eof': 2, 'completion_event': 20})
            self.assertEqual(len(observed['recent']), 12)
            self.assertNotIn('PRIVATE', json.dumps(report))

    def test_failed_terminal_detail_survives_completed_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'PRIVATE.log'
            path.write_text(
                '[2026-01-02T12:00:00Z] [aabbccdd] [warn] codex stream observation: phase=stream '
                'outcome=terminal_event last_event=response.failed prior_event=response.output_item.added '
                'data_items=5 elapsed_ms=17000 since_read_ms=0 event_gap_ms=11000 '
                'max_read_gap_ms=11000 read_error=none\n'
                '[2026-01-02T12:00:00Z] [aabbccdd] [info] [gin.go:1] 200 | 25.125s | PRIVATE_HOST | '
                'POST "/v1/messages?token=PRIVATE_SECRET"\n' +
                ('[2026-01-02T12:01:00Z] [11223344] [info] codex stream observation: phase=stream '
                 'outcome=completion_event last_event=response.completed prior_event=response.output_item.done '
                 'data_items=10 elapsed_ms=1000 since_read_ms=0 event_gap_ms=1 '
                 'max_read_gap_ms=500 read_error=none\n') * 20, encoding='utf-8')
            report = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            observed = report['stream_termination_observations']
            self.assertEqual(len(observed['recent']), 12)
            self.assertTrue(all(row['outcome'] == 'completion_event' for row in observed['recent']))
            failure, = observed['recent_noncompletion']
            self.assertEqual(failure['last_event'], 'response.failed')
            self.assertEqual(failure['max_read_gap_ms'], 11000)
            self.assertEqual(failure['same_log_match'], 'unique')
            self.assertEqual(failure['http_status'], 200)
            self.assertEqual(failure['http_seconds'], 25.125)
            self.assertEqual(observed['noncompletion_omitted_at_limit'], 0)
            self.assertEqual(observed['outcome_mentions'], {'terminal_event': 1, 'completion_event': 20})
            for secret in ('aabbccdd', '11223344', 'PRIVATE', str(path)):
                self.assertNotIn(secret, json.dumps(report))

    def test_noncompletion_retention_is_bounded_and_respects_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'PRIVATE.log'
            failure = ('[2026-01-02T12:00:{second:02d}Z] [warn] codex stream observation: phase=stream '
                       'outcome=eof last_event=response.created data_items=1 '
                       'elapsed_ms=1 since_read_ms=0 read_error=none\n')
            path.write_text(
                failure.format(second=0).replace('2026-01-02', '2025-12-31') +
                ''.join(failure.format(second=i) for i in range(15)) +
                ('[2026-01-02T12:01:00Z] [info] codex stream observation: phase=stream '
                 'outcome=completion_event last_event=response.completed '
                 'data_items=1 elapsed_ms=1 since_read_ms=0 read_error=none\n') * 40,
                encoding='utf-8')
            observed = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))[
                'stream_termination_observations']
            self.assertEqual(observed['outcome_mentions'], {'eof': 15, 'completion_event': 40})
            self.assertEqual(len(observed['recent_noncompletion']), 12)
            self.assertEqual(observed['noncompletion_omitted_at_limit'], 3)
            self.assertEqual(observed['recent_noncompletion'][0]['at'], '2026-01-02T12:00:03+00:00')
            self.assertTrue(all(row['same_log_match'] == 'untagged' and row['http_status'] is None
                                and row['max_read_gap_ms'] is None
                                for row in observed['recent_noncompletion']))

    def test_stream_observer_accepts_old_and_new_metadata_without_partial_shapes(self):
        prefix = ('codex stream observation: phase=stream outcome=eof last_event=response.output_text.delta ')
        legacy = prefix + 'data_items=2 elapsed_ms=3000 since_read_ms=1000 read_error=none'
        current = (prefix + 'prior_event=response.created data_items=2 elapsed_ms=3000 since_read_ms=1000 '
                   'event_gap_ms=500 max_read_gap_ms=800 read_error=none')
        old = diagnostics.stream_observation(legacy)
        new = diagnostics.stream_observation(current)
        self.assertIsNone(old['prior_event'])
        self.assertIsNone(old['event_gap_ms'])
        self.assertIsNone(old['max_read_gap_ms'])
        self.assertEqual(new['prior_event'], 'response.created')
        self.assertEqual(new['event_gap_ms'], 500)
        metadata = diagnostics.stream_observation(current.replace('prior_event=response.created', 'prior_event=response.metadata'))
        self.assertEqual(metadata['prior_event'], 'response.metadata')
        self.assertIsNone(diagnostics.stream_observation(legacy.replace('data_items=', 'prior_event=none data_items=')))
        self.assertIsNone(diagnostics.stream_observation(current.replace('prior_event=response.created ', '')))
        self.assertIsNone(diagnostics.stream_observation(current.replace('prior_event=response.created', 'prior_event=PRIVATE_SECRET')))

    def test_cross_layer_stream_timing_is_bounded_ambiguous_and_private(self):
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'PRIVATE_PATH.log'
            path.write_text(
                '[2026-01-02T12:00:00Z] [aabbccdd] [info] [gin.go:1] 200 | 78s | PRIVATE_HOST | POST "/v1/responses?key=PRIVATE_SECRET"\n'
                '[2026-01-02T12:01:00Z] [bbccddee] [info] [gin.go:1] 200 | 50s | PRIVATE_HOST | POST "/v1/responses"\n'
                '[2026-01-02T12:01:01Z] [ccddee11] [info] [gin.go:1] 502 | 51s | PRIVATE_HOST | POST "/v1/responses"\n'
                '[2026-01-02T12:02:00Z] [11223344] [info] [gin.go:1] 200 | 80s | PRIVATE_HOST | POST "/v1/responses"\n'
                '[2026-01-02T12:04:00Z] [11223344] [info] [gin.go:1] 200 | 30s | PRIVATE_HOST | GET "/v1/responses"\n',
                encoding='utf-8')
            def failure(at, seconds, match='unique'):
                return {'at': f'2026-01-02T12:0{at}:00+00:00', 'same_log_match': match,
                        'http_status': 200, 'http_seconds': seconds}
            backends = [{'recent_stream_failure_http': [failure(0, 78), failure(1, 50),
                        failure(2, 60), failure(3, 1, 'ambiguous'), failure(4, 30)]}]
            report = diagnostics.correlate_stream_endings(path, backends, since)
            self.assertEqual([r['origin_timing_match'] for r in report['recent']],
                             ['unique_candidate', 'ambiguous', 'unmatched', 'unavailable', 'unmatched'])
            self.assertEqual(report['recent'][0]['origin_candidate'],
                             {'status': 200, 'seconds': 78, 'time_delta_seconds': 0, 'duration_delta_seconds': 0})
            self.assertIsNone(report['recent'][1]['origin_candidate'])
            self.assertIsNone(report['recent'][3]['managed_http_status'])
            self.assertIn('not a cross-layer identity', report['limits'])
            for private in ('PRIVATE_SECRET', 'PRIVATE_PATH', 'PRIVATE_HOST', 'aabbccdd', 'bbccddee'):
                self.assertNotIn(private, json.dumps(report))

            bounded = diagnostics.correlate_stream_endings(path,
                [{'recent_stream_failure_http': [failure(4, 30) for _ in range(20)]}], since)
            self.assertEqual(len(bounded['recent']), 12)
            self.assertEqual(bounded['omitted_at_limit'], 8)

    def test_collection_compares_only_owned_log_metadata_without_reading_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / 'origin.log'
            origin.write_text('[2026-01-02T12:00:00Z] [12345678] [info] [gin.go:1] 200 | 78s | '
                              'PRIVATE_HOST | POST "/v1/responses?key=PRIVATE_SECRET"\n')
            generation = 'g-' + 'a' * 24
            directory = root / 'runtime-managed' / generation
            directory.mkdir(parents=True)
            (directory / 'proxy.log').write_text(
                '[2026-01-02T12:00:00Z] [87654321] upstream stream failed: '
                'stream disconnected before completion PRIVATE_SECRET\n'
                '[2026-01-02T12:00:00Z] [87654321] [info] [gin.go:1] 200 | 78s | '
                'PRIVATE_HOST | POST "/v1/messages"\n')
            with patch.object(launcher, 'read_state', return_value={'pid': 123, 'port': 12345}), \
                 patch.object(launcher, 'state_paths', return_value=(None, None, None, origin)), \
                 patch.object(launcher, 'listener_pids', return_value={123}), \
                 patch.object(rolling_runtime, 'state', return_value={'pid': 123, 'port': 12345, 'current': generation}), \
                 patch.object(rolling_runtime, 'private_key', side_effect=AssertionError('must not read keys')):
                report = diagnostics.collect(root, 'subscription', [], datetime(2026, 1, 1, tzinfo=timezone.utc))
                with patch.object(diagnostics, 'correlate_stream_endings', side_effect=OSError('PRIVATE_SECRET')):
                    unavailable = diagnostics.collect(root, 'subscription', [], datetime(2026, 1, 1, tzinfo=timezone.utc))
            row, = report['stream_timing_correlations']['recent']
            self.assertEqual(row['origin_timing_match'], 'unique_candidate')
            self.assertEqual(row['origin_candidate']['seconds'], 78)
            self.assertEqual(unavailable['stream_timing_correlations'], {'metadata_unavailable': True})
            self.assertEqual(len(unavailable['managed']['backends']), 1)
            for private in ('PRIVATE_SECRET', 'PRIVATE_HOST', '12345678', '87654321', generation, str(root)):
                self.assertNotIn(private, json.dumps(report))

    def test_cross_layer_comparison_includes_observer_only_failures_after_healthy_controls(self):
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            origin, managed = (Path(temporary) / name for name in ('origin.log', 'managed.log'))
            origin.write_text('[2026-01-02T12:00:00Z] [12345678] [info] [gin.go:1] 200 | 7s | '
                              'PRIVATE_HOST | POST "/v1/responses?key=PRIVATE_SECRET"\n')
            failed = ('[2026-01-02T12:00:00Z] [87654321] codex stream observation: phase=stream '
                      'outcome=terminal_event last_event=response.failed prior_event=response.output_item.added '
                      'data_items=2 elapsed_ms=7000 since_read_ms=0 event_gap_ms=1000 max_read_gap_ms=1000 '
                      'read_error=none failure_kind=reported_incomplete\n')
            healthy = ''.join(f'[2026-01-02T12:01:{i:02d}Z] [aabbccdd] codex stream observation: phase=stream '
                             'outcome=completion_event last_event=response.completed prior_event=response.created '
                             'data_items=2 elapsed_ms=10 since_read_ms=0 event_gap_ms=10 max_read_gap_ms=10 '
                             'read_error=none failure_kind=none\n' for i in range(13))
            managed.write_text(failed + '[2026-01-02T12:00:00Z] [87654321] [info] [gin.go:1] 200 | 7s | '
                               'PRIVATE_HOST | POST "/v1/messages"\n' + healthy)
            summary = diagnostics.proxy_summary(managed, since)
            self.assertEqual(summary['recent_stream_failure_http'], [])
            self.assertEqual(summary['stream_failure_mentions'], {})
            self.assertTrue(all(v['outcome'] == 'completion_event'
                                for v in summary['stream_termination_observations']['recent']))
            report = diagnostics.correlate_stream_endings(origin, [summary], since)
            row, = report['recent']
            self.assertEqual(row['metadata_source'], 'stream_observer')
            self.assertEqual(row['managed_stream_outcome'], 'terminal_event')
            self.assertEqual(row['managed_failure_kind'], 'reported_incomplete')
            self.assertEqual(row['managed_read_error'], 'none')
            self.assertEqual(row['managed_http_status'], 200)
            self.assertEqual(row['origin_timing_match'], 'unique_candidate')
            self.assertEqual(row['origin_candidate']['seconds'], 7)
            for private in ('PRIVATE_SECRET', 'PRIVATE_HOST', '12345678', '87654321', 'aabbccdd', str(temporary)):
                self.assertNotIn(private, json.dumps(report))

    def test_cross_layer_observer_cancellation_and_completion_do_not_become_failures(self):
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = {'at': '2026-01-02T12:00:00Z', 'same_log_match': 'unique', 'http_seconds': 7,
                  'http_status': 200, 'read_error': 'none', 'failure_kind': 'none'}
        rows = [{**record, 'outcome': 'read_error', 'read_error': 'cancelled'},
                {**record, 'outcome': 'completion_event'}, {**record, 'outcome': 'other'}]
        with patch.object(diagnostics, 'lines', side_effect=AssertionError('No origin scan is needed')):
            report = diagnostics.correlate_stream_endings(Path('unused'),
                [{'stream_termination_observations': {'recent_noncompletion': rows}}], since)
        self.assertEqual(report['recent'], [])
        self.assertEqual(report['omitted_at_limit'], 0)

    def test_cross_layer_keeps_both_evidence_sources_without_claiming_unique_failures(self):
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = {'at': '2026-01-02T12:00:00Z', 'same_log_match': 'unique', 'http_seconds': 7,
                  'http_status': 200, 'outcome': 'eof', 'read_error': 'unexpected_eof',
                  'failure_kind': 'none', 'private': 'PRIVATE_SECRET'}
        with tempfile.TemporaryDirectory() as temporary:
            origin = Path(temporary) / 'origin.log'
            origin.write_text('[2026-01-02T12:00:00Z] [12345678] [info] [gin.go:1] 200 | 7s | '
                              'PRIVATE_HOST | POST "/v1/responses"\n')
            backend = {'recent_stream_failure_http': [record],
                       'stream_termination_observations': {'recent_noncompletion': [record]}}
            report = diagnostics.correlate_stream_endings(origin, [backend], since)
            self.assertEqual([v['metadata_source'] for v in report['recent']], ['error_log', 'stream_observer'])
            self.assertIsNone(report['recent'][0]['managed_stream_outcome'])
            self.assertEqual(report['recent'][1]['managed_stream_outcome'], 'eof')
            self.assertIn('not a unique failure count', report['limits'])
            self.assertNotIn('PRIVATE', json.dumps(report))
            unknown = {**record, 'failure_kind': 'PRIVATE_SECRET', 'read_error': 'PRIVATE_SECRET',
                       'same_log_match': 'ambiguous'}
            report = diagnostics.correlate_stream_endings(origin,
                [{'stream_termination_observations': {'recent_noncompletion': [unknown]}}], since)
            row, = report['recent']
            self.assertEqual(row['origin_timing_match'], 'unavailable')
            self.assertIsNone(row['managed_http_status'])
            self.assertIsNone(row['managed_failure_kind'])
            self.assertIsNone(row['managed_read_error'])
            self.assertNotIn('PRIVATE', json.dumps(report))

    def test_stream_observer_categories_are_bounded_private_and_paired_only_within_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proxy.log"
            prefix = "codex stream observation: phase=stream outcome=eof last_event=response.output_text.delta prior_event=response.created "
            suffix = "data_items=2 elapsed_ms=3000 since_read_ms=1000 event_gap_ms=2000 max_read_gap_ms=2000 read_error=none"
            path.write_text(
                f'[2025-12-31 23:59:59] [aabbccdd] {prefix}{suffix}\n'
                f'[2026-01-02 12:00:00] [aabbccdd] [warn] {prefix}{suffix}\n'
                '[2026-01-02 12:00:01] [aabbccdd] [info] [gin.go:1] 200 | 3s | 127.0.0.1 | POST "/v1/messages"\n'
                f'[2026-01-02 12:01:00] [11223344] [warn] {prefix}{suffix}\n'
                '[2026-01-02 12:01:01] [11223344] [info] [gin.go:1] 200 | 2s | 127.0.0.1 | POST "/v1/messages"\n'
                '[2026-01-02 12:01:02] [11223344] [info] [gin.go:1] 200 | 3s | 127.0.0.1 | POST "/v1/messages"\n'
                f'[2026-01-02 12:02:00] [55667788] [warn] {prefix}{suffix}\n'
                f'[2026-01-02 12:03:00] [warn] {prefix}{suffix}\n'
                '[2026-01-02 12:04:00] [warn] codex stream observation: phase=stream outcome=terminal_event '
                'last_event=error prior_event=response.created data_items=9 elapsed_ms=900 since_read_ms=2 event_gap_ms=800 max_read_gap_ms=800 read_error=none\n'
                '[2026-01-02 12:05:00] [warn] codex stream observation: phase=stream outcome=eof '
                'last_event=private_secret prior_event=response.created data_items=9 elapsed_ms=900 since_read_ms=2 event_gap_ms=800 max_read_gap_ms=800 read_error=none\n'
                f'[2026-01-02 12:05:01] [warn] {prefix}{suffix} PRIVATE_SECRET\n'
                f'[2026-01-02 12:05:02] [warn] {prefix}data_items=1000001 elapsed_ms=3000 since_read_ms=1000 event_gap_ms=2000 max_read_gap_ms=2000 read_error=none\n'
                '[2026-01-02 12:05:03] [warn] codex stream observation: phase=stream outcome=eof '
                'last_event=error prior_event=private_secret data_items=1 elapsed_ms=1 since_read_ms=1 event_gap_ms=1 max_read_gap_ms=1 read_error=none\n'
                '[2026-01-02 12:05:04] [warn] codex stream observation: phase=stream outcome=eof '
                'last_event=error prior_event=none data_items=1 elapsed_ms=1 since_read_ms=1 event_gap_ms=86400001 max_read_gap_ms=1 read_error=none\n',
                encoding="utf-8")
            report = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            observation = report["stream_termination_observations"]
            self.assertEqual(observation["outcome_mentions"], {"eof": 4, "terminal_event": 1})
            self.assertEqual(observation["recent_noncompletion"], observation["recent"])
            self.assertEqual(observation["noncompletion_omitted_at_limit"], 0)
            self.assertEqual([row["same_log_match"] for row in observation["recent"]],
                             ["unique", "ambiguous", "unmatched", "untagged", "untagged"])
            self.assertEqual(observation["recent"][0]["http_status"], 200)
            self.assertEqual(observation["recent"][0]["http_seconds"], 3)
            self.assertEqual(observation["recent"][0]["event_gap_ms"], 2000)
            self.assertEqual(observation["recent"][0]["max_read_gap_ms"], 2000)
            self.assertEqual(observation["recent"][-1]["last_event"], "error")
            self.assertEqual(observation["recent"][-1]["prior_event"], "response.created")
            for private in ("PRIVATE_SECRET", "private_secret", "aabbccdd", "11223344", "55667788", str(path)):
                self.assertNotIn(private, json.dumps(report))

    def test_managed_backend_with_only_stream_observation_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = "g-" + "a" * 24
            directory = root / "runtime-managed" / generation
            directory.mkdir(parents=True)
            (directory / "proxy.log").write_text(
                '[2026-01-02 12:00:00] [aabbccdd] [warn] codex stream observation: '
                'phase=bootstrap outcome=read_error last_event=other prior_event=none data_items=1 '
                'elapsed_ms=50 since_read_ms=10 event_gap_ms=5 max_read_gap_ms=5 read_error=timeout\n', encoding="utf-8")
            with patch.object(rolling_runtime, "state", return_value={"pid": 123, "port": 12345, "current": generation}), \
                 patch.object(launcher, "listener_pids", return_value={123}):
                report = diagnostics.collect(root, "subscription", [], datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(len(report["managed"]["backends"]), 1)
            observation = report["managed"]["backends"][0]["stream_termination_observations"]
            self.assertEqual(observation["outcome_mentions"], {"read_error": 1})
            self.assertNotIn("aabbccdd", json.dumps(report))

    def test_background_refresh_events_are_separate_bounded_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proxy.log"
            path.write_text(
                '[2025-12-31T23:59:59Z] [12345678] failed to refresh antigravity version: timeout\n'
                '[2026-01-02T12:00:01Z] [12345678] failed to refresh antigravity version: context deadline exceeded PRIVATE_SECRET\n'
                '[2026-01-02T12:00:00Z] [12345678] failed to refresh antigravity version: timeout https://user:PRIVATE_SECRET@example.invalid/PRIVATE_PATH\n'
                '[2026-01-02T12:00:02Z] [87654321] failed to refresh antigravity version: connection refused PRIVATE_SECRET\n'
                '[2026-01-02T12:00:03Z] [aabbccdd] upstream execution failed: timeout PRIVATE_SECRET\n'
                '[2026-01-02T12:00:04Z] [aabbccdd] [info] [gin.go:1] 503 | 2s | 127.0.0.1 | POST "/v1/messages"\n'
                '[2026-01-02T12:00:05Z] [aabbccdd] [info] [gin.go:1] 200 | 2ms | 127.0.0.1 | GET "/failed to refresh antigravity version?token=PRIVATE_SECRET"\n',
                encoding="utf-8")
            report = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(report["background_event_mentions"], {
                "antigravity_version_refresh_timeout": {"mentions": 2, "last_at": "2026-01-02T12:00:01+00:00"},
                "antigravity_version_refresh_other": {"mentions": 1, "last_at": "2026-01-02T12:00:02+00:00"}})
            self.assertEqual(report["transport_error_mentions"]["timeout"], 2)
            self.assertEqual(report["completed_http_statuses"], {"503": 1, "200": 1})
            self.assertEqual([row["endpoint"] for row in report["failed_requests"]], ["/v1/messages"])
            for private in ("PRIVATE_SECRET", "PRIVATE_PATH", "example.invalid", "12345678", "87654321", str(path)):
                self.assertNotIn(private, json.dumps(report))

    def test_stream_failure_error_shapes_are_allowlisted_and_not_a_cause_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proxy.log"
            message = "stream disconnected before completion PRIVATE_SECRET"
            timeout = json.dumps({"error": {"type": "server_error", "code": "request_timeout", "message": message},
                                  "request_id": "PRIVATE_RESPONSE_ID"})
            sentinel = json.dumps({"error": {"type": "server_error", "code": "request_timeout",
                                             "message": diagnostics.INCOMPLETE_STREAM_SENTINEL}})
            other = json.dumps({"error": {"type": "server_error", "code": "PRIVATE_CODE", "message": message}})
            path.write_text(
                f'[2026-01-02 12:00:00] [aabbccdd] upstream stream failed: err={timeout}\n'
                f'[2026-01-02 12:00:01] [aabbccdd] upstream stream failed: err={timeout}\n'
                f'[2026-01-02 12:00:01] [deadbeef] upstream stream failed: err={sentinel}\n'
                f'[2026-01-02 12:00:02] [11223344] upstream stream failed: err={other}\n'
                '[2026-01-02 12:00:03] [55667788] upstream stream failed: err=stream error: stream disconnected before completion PRIVATE_SECRET\n'
                '[2026-01-02 12:00:04] [99887766] upstream stream failed: stream disconnected before completion PRIVATE_SECRET\n', encoding="utf-8")
            report = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            shapes = report["stream_failure_shapes"]
            self.assertEqual(shapes["reported_server_request_timeout"]["mentions"], 2)
            self.assertEqual(shapes["reported_server_request_timeout"]["tagged_requests"], 1)
            self.assertEqual(shapes["reported_incomplete_stream_sentinel"]["mentions"], 1)
            self.assertEqual(shapes["reported_incomplete_stream_sentinel"]["tagged_requests"], 1)
            self.assertEqual(shapes["other_error_payload"]["mentions"], 1)
            self.assertEqual(shapes["plain_or_redacted_incomplete"]["mentions"], 1)
            self.assertEqual(shapes["unclassified"]["mentions"], 1)
            for secret in ("PRIVATE_SECRET", "PRIVATE_RESPONSE_ID", "PRIVATE_CODE", "aabbccdd", "deadbeef", str(path)):
                self.assertNotIn(secret, json.dumps(report))

    def test_http_200_does_not_hide_a_reported_native_stream_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proxy.log"
            path.write_text(
                '[2026-01-02 12:00:00] [aabbccdd] upstream stream failed: stream error: stream disconnected before completion: stream closed before response.completed PRIVATE_SECRET\n'
                '[2026-01-02 12:00:00] [aabbccdd] [info] [gin.go:1] 200 | 59s | 127.0.0.1 | POST "/v1/messages"\n')
            report = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(report["completed_http_statuses"], {"200": 1})
            self.assertEqual(report["failed_requests"], [])
            self.assertEqual(report["stream_failure_mentions"]["incomplete_native_stream"]["tagged_requests"], 1)
            self.assertEqual(report["recent_stream_failure_http"][0]["same_log_match"], "unique")
            self.assertEqual(report["recent_stream_failure_http"][0]["http_status"], 200)
            self.assertEqual(report["recent_stream_failure_http"][0]["http_seconds"], 59)
            for private in ("PRIVATE_SECRET", "aabbccdd", str(path)):
                self.assertNotIn(private, json.dumps(report))

    def test_stream_matching_stays_ambiguous_when_a_local_tag_is_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proxy.log"
            path.write_text(
                '[2026-01-02 12:00:00] [aabbccdd] upstream stream failed: stream disconnected before completion\n'
                '[2026-01-02 12:00:00] [aabbccdd] [info] [gin.go:1] 200 | 47s | 127.0.0.1 | POST "/v1/messages"\n'
                '[2026-01-02 12:00:01] [aabbccdd] [info] [gin.go:1] 200 | 215s | 127.0.0.1 | POST "/v1/messages"\n')
            row, = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))["recent_stream_failure_http"]
            self.assertEqual(row["same_log_match"], "ambiguous")
            self.assertIsNone(row["http_status"])
            self.assertIsNone(row["http_seconds"])
            self.assertNotIn("aabbccdd", json.dumps(row))

    def test_native_media_failures_are_distinct_without_leaking_call_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proxy.log"
            requests = [("201", "POST", "/v1/live"),
                        ("404", "GET", "/v1/live/rtc_PRIVATE_CALL?token=PRIVATE_SECRET"),
                        ("502", "POST", "/v1/realtime/calls/rtc_PRIVATE_CALL/hangup"),
                        ("400", "GET", "/v1/live/sessions/PRIVATE_SESSION?key=PRIVATE_SECRET"),
                        ("400", "POST", "/v1/realtime/client_secrets"),
                        ("404", "GET", "/v1/live/sessions")]
            path.write_text("".join(f'[2026-01-02 12:00:00] [fixture] [info] [gin.go:1] {status} | 2ms | 127.0.0.1 | {method} "{url}"\n'
                                    for status, method, url in requests), encoding="utf-8")
            result = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertEqual([row["endpoint"] for row in result["failed_requests"]],
                ["/v1/live/:call_id", "/v1/realtime/calls/:call_id/hangup",
                 "/v1/live/sessions/:session_id", "/v1/realtime/client_secrets", "/v1/live/sessions"])
            self.assertEqual(result["inference_requests"], 0)
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_upstream_handshake_attempts_are_separate_from_user_requests_and_disconnects(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proxy.log"
            path.write_text(
                '[2026-01-02 12:00:00] [aabbccdd] upstream execution failed: err=utls: TLS handshake: EOF PRIVATE_SECRET\n'
                '[2026-01-02 12:00:01] [aabbccdd] upstream execution failed: err=utls: TLS handshake: EOF PRIVATE_SECRET\n'
                '[2026-01-02 12:00:02] [00112233] upstream execution failed: err=utls: TLS handshake: EOF PRIVATE_SECRET\n'
                '[2026-01-02 12:00:03] [44556677] downstream closed: EOF PRIVATE_SECRET\n')
            result = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            detail = result["upstream_preconnection_failures"]["tls_handshake_eof"]
            self.assertEqual(detail["attempt_mentions"], 3)
            self.assertEqual(detail["tagged_requests"], 2)
            self.assertEqual(result["transport_error_mentions"]["eof"], 4)
            for private in ("PRIVATE_SECRET", "aabbccdd", "00112233", "44556677"):
                self.assertNotIn(private, json.dumps(result))

    def test_handshake_url_host_categories_never_expose_private_urls(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proxy.log"
            path.write_text(
                '[2026-01-02 12:00:00] [aabbccdd] upstream execution failed: Post "https://user:PRIVATE_SECRET@chatgpt.com/private?token=PRIVATE_SECRET": TLS handshake: EOF\n'
                '[2026-01-02 12:00:01] [aabbccdd] upstream execution failed: Post "https://chatgpt.com.evil.invalid/private": TLS handshake: EOF\n'
                '[2026-01-02 12:00:02] [11223344] upstream execution failed: Post "https://127.0.0.1:12345/private": TLS handshake: EOF\n'
                '[2026-01-02 12:00:03] [55667788] upstream execution failed: Post "https://api.openai.com/private": TLS handshake: EOF\n'
                '[2026-01-02 12:00:04] [99887766] upstream execution failed: Post "https://chatgpt.com/a" and "https://127.0.0.1:12345/b": TLS handshake: EOF\n'
                '[2026-01-02 12:00:05] [99887766] upstream execution failed: TLS handshake: EOF\n', encoding="utf-8")
            result = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            detail = result["upstream_preconnection_failures"]["tls_handshake_eof"]
            self.assertEqual(detail["url_host_mentions"],
                             {"chatgpt": 1, "other": 1, "loopback": 1, "openai_api": 1, "mixed": 1, "none": 1})
            for private in ("PRIVATE_SECRET", "evil.invalid", "12345", "aabbccdd"):
                self.assertNotIn(private, json.dumps(result))

    def test_handshake_completion_matches_are_bounded_and_never_expose_tags(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proxy.log"
            path.write_text(
                '[2026-01-02 12:00:00] [aabbccdd] upstream execution failed: TLS handshake: EOF PRIVATE_SECRET\n'
                '[2026-01-02 12:00:12] [aabbccdd] [info] [gin.go:1] 200 | 22.445s | 127.0.0.1 | POST "/v1/responses"\n'
                '[2026-01-02 12:01:00] [11223344] upstream execution failed: TLS handshake: EOF PRIVATE_SECRET\n'
                '[2026-01-02 12:01:05] [11223344] [info] [gin.go:1] 200 | 5s | 127.0.0.1 | POST "/v1/responses"\n'
                '[2026-01-02 12:01:07] [11223344] [info] [gin.go:1] 503 | 7s | 127.0.0.1 | POST "/v1/responses"\n'
                '[2026-01-02 12:02:00] [55667788] upstream execution failed: TLS handshake: EOF PRIVATE_SECRET\n'
                '[2026-01-02 12:02:01] upstream execution failed: TLS handshake: EOF PRIVATE_SECRET\n', encoding="utf-8")
            report = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            rows = report["recent_preconnection_http"]
            self.assertEqual([row["same_log_match"] for row in rows], ["unique", "ambiguous", "unmatched", "untagged"])
            self.assertEqual(rows[0]["http_status"], 200)
            self.assertEqual(rows[0]["http_seconds"], 22.445)
            self.assertEqual(rows[0]["seconds_after_failure"], 12)
            self.assertIsNone(rows[1]["http_status"])
            for private in ("PRIVATE_SECRET", "aabbccdd", "11223344", "55667788", str(path)):
                self.assertNotIn(private, json.dumps(report))

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

    def test_managed_backend_logs_remain_metadata_only_and_do_not_read_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = "g-" + "a" * 24
            directory = root / "runtime-managed" / generation
            directory.mkdir(parents=True)
            (directory / "proxy.log").write_text('[2026-01-02 12:00:00] [fixture] [info] [gin.go:1] 503 | 2ms | 127.0.0.1 | POST "/v1/messages?key=PRIVATE_SECRET"\n', encoding="utf-8")
            with patch.object(rolling_runtime, "state", return_value={"pid": 123, "port": 12345, "current": generation}), \
                 patch.object(rolling_runtime, "private_key", side_effect=AssertionError("must not read keys")), \
                 patch.object(launcher, "listener_pids", return_value={123}):
                report = diagnostics.collect(root, "subscription", [], datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertTrue(report["managed"]["listener_matches_state"])
            self.assertEqual(report["managed"]["backends"][0]["completed_http_statuses"], {"503": 1})
            for private in ("PRIVATE_SECRET", generation, "127.0.0.1", str(root)):
                self.assertNotIn(private, json.dumps(report))

    def test_managed_stream_only_and_oversized_logs_are_not_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = "g-" + "a" * 24
            oversized = "g-" + "b" * 24
            irrelevant = "g-" + "c" * 24
            for generation in (current, oversized, irrelevant):
                (root / "runtime-managed" / generation).mkdir(parents=True)
            (root / "runtime-managed" / current / "proxy.log").write_text(
                '[2026-01-02 12:00:00] [aabbccdd] upstream stream failed: '
                'stream disconnected before completion PRIVATE_SECRET\n', encoding="utf-8")
            (root / "runtime-managed" / oversized / "proxy.log").write_text(
                'PRIVATE_SECRET' + 'x' * 200 + '\n', encoding="utf-8")
            (root / "runtime-managed" / irrelevant / "proxy.log").write_text(
                '[2026-01-02 12:00:00] ordinary startup\n', encoding="utf-8")
            with patch.object(diagnostics, "LINE_LIMIT", 160), \
                 patch.object(rolling_runtime, "state", return_value={"pid": 123, "port": 12345, "current": current}), \
                 patch.object(rolling_runtime, "private_key", side_effect=AssertionError("must not read keys")), \
                 patch.object(launcher, "listener_pids", return_value={123}):
                report = diagnostics.collect(root, "subscription", [], datetime(2026, 1, 1, tzinfo=timezone.utc))
            backends = report["managed"]["backends"]
            self.assertEqual(len(backends), 2)
            stream, = (row for row in backends if row["current"])
            self.assertEqual(stream["completed_http_statuses"], {})
            self.assertEqual(stream["transport_error_mentions"], {})
            self.assertEqual(stream["stream_failure_mentions"]["incomplete_native_stream"]["mentions"], 1)
            skipped, = (row for row in backends if not row["current"])
            self.assertEqual(skipped["coverage"]["oversized_lines"], 1)
            for private in ("PRIVATE_SECRET", "aabbccdd", current, oversized, str(root)):
                self.assertNotIn(private, json.dumps(report))

    def test_managed_background_only_log_is_not_omitted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = "g-" + "a" * 24
            directory = root / "runtime-managed" / generation
            directory.mkdir(parents=True)
            (directory / "proxy.log").write_text(
                '[2026-01-02T12:00:00Z] failed to refresh antigravity version: connection refused PRIVATE_SECRET\n',
                encoding="utf-8")
            with patch.object(rolling_runtime, "state", return_value={"pid": 123, "port": 12345, "current": generation}), \
                 patch.object(rolling_runtime, "private_key", side_effect=AssertionError("must not read keys")), \
                 patch.object(launcher, "listener_pids", return_value={123}):
                report = diagnostics.collect(root, "subscription", [], datetime(2026, 1, 1, tzinfo=timezone.utc))
            backend, = report["managed"]["backends"]
            self.assertEqual(backend["transport_error_mentions"], {})
            self.assertEqual(backend["background_event_mentions"]["antigravity_version_refresh_other"]["mentions"], 1)
            for private in ("PRIVATE_SECRET", generation, str(root)):
                self.assertNotIn(private, json.dumps(report))

    def test_large_blob_does_not_hide_next_metadata_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.jsonl"
            path.write_text("x" * 1000 + "\n" + '{"type":"safe"}\n', encoding="utf-8")
            with patch.object(diagnostics, "LINE_LIMIT", 64):
                self.assertEqual(list(diagnostics.lines(path)), ['{"type":"safe"}\n'])
                report = diagnostics.proxy_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
                session = diagnostics.session_summary(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(report["coverage"]["oversized_lines"], 1)
            self.assertEqual(report["coverage"]["lines_seen"], 2)
            self.assertEqual(session["coverage"]["oversized_lines"], 1)
            self.assertEqual(session["coverage"]["records_missing_timestamp"], 1)
        self.assertEqual(diagnostics.duration("1h2m3.5s"), 3723.5)
        self.assertAlmostEqual(diagnostics.duration("800µs"), .0008)
        self.assertIsNone(diagnostics.duration("private-input"))


if __name__ == "__main__":
    unittest.main()
