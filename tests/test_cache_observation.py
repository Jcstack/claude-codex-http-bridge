"""Bounded local status-line observations never retain user content."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import api_profile
import cache_observation as observation
import session_audit
import statusline_adapter


SESSION = "synthetic-session-for-target"
PRIVATE = "SYNTHETIC_PRIVATE_DO_NOT_STORE"


class CacheObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cache-observation-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audit = self.root / "runtime-audit"
        self.audit.mkdir(mode=0o700)
        if os.name == "nt":
            import windows_acl
            windows_acl.lock([self.root, self.audit])
        else:
            self.root.chmod(0o700)
            self.audit.chmod(0o700)
        api_profile.write_private(self.audit / "target.json", json.dumps({"format": 1,
                                  "session_tag": session_audit.tag(SESSION)}))
        self.payload = {"session_id": SESSION, "cwd": PRIVATE, "transcript_path": PRIVATE,
                        "model": {"id": PRIVATE}, "user_input": PRIVATE,
                        "prompt_cache": {"caching_observed": True, "requests": 4,
                                         "hit_ratio": .375, "other": PRIVATE}}

    def corrected(self, enabled=True):
        return statusline_adapter.corrected_payload(self.payload, {}, expose_cache_metrics=enabled)

    def test_one_matching_observation_stores_only_fixed_field_shapes(self):
        observation.begin(self.root)
        self.assertEqual(observation.status(self.root), {"state": "waiting"})
        observation.capture(self.root, self.payload, self.corrected(), enabled=True)
        expected = {"state": "observed", "telemetry_enabled": True, "source_fields_inspected": True, "preexisting_namespace": False,
                    "source_cache_object": True, "source_observed_boolean": True, "source_positive_requests": True,
                    "source_valid_ratio": True, "output_cache_object": True, "output_observed_boolean": True,
                    "output_positive_requests": True, "output_valid_ratio": True}
        self.assertEqual(observation.status(self.root), expected)
        for name in (observation.CONTROL, observation.RESULT):
            raw = (self.audit / name).read_text()
            for forbidden in (SESSION, PRIVATE, "0.375"):
                self.assertNotIn(forbidden, raw)
            if os.name != "nt":
                self.assertEqual((self.audit / name).stat().st_mode & 0o077, 0)
        # The first observation wins even if another matching callback differs.
        observation.capture(self.root, {**self.payload, "prompt_cache": None}, {}, enabled=False)
        self.assertEqual(observation.status(self.root), expected)
        observation.stop(self.root)
        self.assertEqual(observation.status(self.root), {"state": "inactive"})
        self.assertTrue((self.audit / "target.json").is_file())

    def test_unmatched_and_expired_inputs_never_capture(self):
        with patch.object(observation.time, "time", return_value=1000):
            observation.begin(self.root)
            observation.capture(self.root, {**self.payload, "session_id": "another-session"}, self.corrected(), enabled=True)
            observation.capture(self.root, {**self.payload, "session_id": None}, self.corrected(), enabled=True)
            self.assertEqual(observation.status(self.root), {"state": "waiting"})
        with patch.object(observation.time, "time", return_value=1301):
            observation.capture(self.root, self.payload, self.corrected(), enabled=True)
            self.assertEqual(observation.status(self.root), {"state": "expired"})
        with patch.object(observation.time, "time", return_value=0):
            self.assertEqual(observation.status(self.root), {"state": "unavailable"})

    def test_missing_source_cache_is_not_reported_as_a_valid_output(self):
        observation.begin(self.root)
        payload = {**self.payload, "prompt_cache": None}
        corrected = statusline_adapter.corrected_payload(payload, {}, expose_cache_metrics=True)
        observation.capture(self.root, payload, corrected, enabled=True)
        status = observation.status(self.root)
        self.assertEqual(status["state"], "observed")
        self.assertTrue(status["telemetry_enabled"])
        self.assertFalse(status["source_cache_object"] or status["output_cache_object"] or status["output_valid_ratio"])

    def test_disabled_telemetry_withholds_cache_field_shapes(self):
        observation.begin(self.root)
        observation.capture(self.root, self.payload, self.corrected(enabled=False), enabled=False)
        status = observation.status(self.root)
        self.assertEqual(status["state"], "observed")
        self.assertFalse(status["telemetry_enabled"] or status["source_fields_inspected"])
        self.assertTrue(all(not value for key, value in status.items() if key != "state"))

    def test_start_replaces_a_previous_nonce_and_stop_is_bounded(self):
        observation.begin(self.root)
        old = json.loads((self.audit / observation.CONTROL).read_text())
        observation.begin(self.root)
        current = json.loads((self.audit / observation.CONTROL).read_text())
        self.assertNotEqual(old["nonce"], current["nonce"])
        result = json.loads((self.audit / observation.RESULT).read_text())
        self.assertEqual(result["nonce"], current["nonce"])
        result["nonce"] = old["nonce"]
        api_profile.write_private(self.audit / observation.RESULT, json.dumps(result))
        observation.capture(self.root, self.payload, self.corrected(), enabled=True)
        self.assertEqual(observation.status(self.root), {"state": "unavailable"})
        observation.stop(self.root)

    def test_nonblocking_concurrent_callbacks_capture_one_valid_snapshot(self):
        observation.begin(self.root)

        def run(_index):
            try:
                observation.capture(self.root, self.payload, self.corrected(), enabled=True)
            except (OSError, RuntimeError, ValueError):
                pass  # The real adapter treats observation as best effort.

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(run, range(24)))
        result = observation.status(self.root)
        self.assertEqual(result["state"], "observed")
        self.assertEqual(set(result) - {"state"}, observation.SUMMARY_FIELDS)
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_corrupt_or_unsafe_files_fail_closed(self):
        observation.begin(self.root)
        path = self.audit / observation.CONTROL
        api_profile.write_private(path, '{"version":1,"version":1}')
        self.assertEqual(observation.status(self.root), {"state": "unavailable"})
        if os.name != "nt":
            observation.begin(self.root)
            path.chmod(0o644)
            self.assertEqual(observation.status(self.root), {"state": "unavailable"})
            path.unlink()
            outside = self.root / "outside"
            outside.write_text(PRIVATE)
            path.symlink_to(outside)
            self.assertEqual(observation.status(self.root), {"state": "unavailable"})
            self.assertEqual(outside.read_text(), PRIVATE)
            path.unlink()
            os.mkfifo(path)
            self.assertEqual(observation.status(self.root), {"state": "unavailable"})

    def test_adapter_capture_is_opt_in_and_preserves_output_when_probe_fails(self):
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"context_windows": {}, "route": "subscription", "expose_cache_metrics": True}))

        def invoke() -> dict:
            source = TextIOWrapper(BytesIO(json.dumps(self.payload).encode()), encoding="utf-8")
            output = BytesIO()
            target = TextIOWrapper(output, encoding="utf-8", write_through=True)
            with patch.object(statusline_adapter, "ROOT", self.root), patch.object(statusline_adapter.sys, "stdin", source), \
                 patch.object(statusline_adapter.sys, "stdout", target), patch.object(statusline_adapter.sys, "argv", ["adapter", str(profile)]):
                self.assertEqual(statusline_adapter.main(), 0)
            return json.loads(output.getvalue())

        expected = invoke()
        self.assertFalse((self.audit / observation.RESULT).exists())
        observation.begin(self.root)
        profile.write_text(json.dumps({"context_windows": {}, "route": "api", "expose_cache_metrics": True}))
        self.assertEqual(invoke(), expected)
        self.assertEqual(observation.status(self.root), {"state": "waiting"})
        profile.write_text(json.dumps({"context_windows": {}, "route": "subscription", "expose_cache_metrics": True}))
        self.assertEqual(invoke(), expected)
        self.assertEqual(observation.status(self.root)["state"], "observed")
        observation.begin(self.root)
        with patch.object(observation, "capture", side_effect=RuntimeError(PRIVATE)):
            self.assertEqual(invoke(), expected)
        self.assertEqual(observation.status(self.root), {"state": "waiting"})

    def test_cli_reports_only_categories_and_does_not_need_inference(self):
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(statusline_adapter.cache_metrics_command(self.root, ["telemetry", "observe"]), 0)
        self.assertNotIn(SESSION, output.getvalue())
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(statusline_adapter.cache_metrics_command(self.root, ["telemetry", "observation"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"state": "waiting"})
        with redirect_stdout(StringIO()):
            self.assertEqual(statusline_adapter.cache_metrics_command(self.root, ["telemetry", "stop-observation"]), 0)

    def test_adapter_observation_respects_explicit_session_opt_out(self):
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"context_windows": {}, "route": "subscription", "expose_cache_metrics": False}))
        observation.begin(self.root)
        source = TextIOWrapper(BytesIO(json.dumps(self.payload).encode()), encoding="utf-8")
        output = BytesIO()
        target = TextIOWrapper(output, encoding="utf-8", write_through=True)
        with patch.object(statusline_adapter, "ROOT", self.root), patch.object(statusline_adapter.sys, "stdin", source), \
             patch.object(statusline_adapter.sys, "stdout", target), patch.object(statusline_adapter.sys, "argv", ["adapter", str(profile)]):
            self.assertEqual(statusline_adapter.main(), 0)
        self.assertNotIn("claude_codex_bridge", json.loads(output.getvalue()))
        result = observation.status(self.root)
        self.assertEqual(result["state"], "observed")
        self.assertFalse(result["telemetry_enabled"] or result["source_fields_inspected"])
        self.assertFalse(result["source_cache_object"] or result["output_cache_object"])


if __name__ == "__main__":
    unittest.main()
