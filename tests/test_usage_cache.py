"""Quota snapshot display never treats expired windows as current."""

from pathlib import Path
from io import BytesIO, TextIOWrapper
import json
import tempfile
import unittest
from unittest.mock import patch

import codex_usage
import statusline_adapter


class UsageCacheTests(unittest.TestCase):
    def read(self, windows, *, fetched=1000, now=1010, fingerprint="same"):
        snapshot = {"fetched_at": fetched, "account_fingerprint": fingerprint, "windows": windows}
        with patch.object(codex_usage, "selected_fingerprint", return_value="same"), \
             patch.object(codex_usage, "_private_json", return_value=snapshot), \
             patch.object(codex_usage.time, "time", return_value=now):
            return codex_usage.read_cache(Path("fixture"))

    def test_reset_window_is_removed_without_losing_other_valid_window(self):
        source = {"five_hour": {"used_percentage": 99, "resets_at": 1010},
                  "seven_day": {"used_percentage": 30, "resets_at": 2000}}
        self.assertEqual(self.read(source), {"seven_day": source["seven_day"]})
        self.assertIn("five_hour", source)  # The caller's snapshot is unchanged.

    def test_all_expired_windows_are_hidden_without_forcing_a_refresh(self):
        self.assertEqual(self.read({"five_hour": {"used_percentage": 99, "resets_at": 1009}}), {})
        self.assertIsNone(self.read({"five_hour": {"used_percentage": 99, "resets_at": 1009}}, now=1300))

    def test_invalid_or_unrecognized_windows_do_not_reach_hud(self):
        invalid = (None, {}, {"used_percentage": True, "resets_at": 2000},
                   {"used_percentage": float("nan"), "resets_at": 2000},
                   {"used_percentage": 101, "resets_at": 2000},
                   {"used_percentage": 10, "resets_at": True},
                   {"used_percentage": 10, "resets_at": "2000"})
        for row in invalid:
            with self.subTest(row=row):
                self.assertEqual(self.read({"five_hour": row, "unknown": {"private": "fixture"}}), {})

    def test_wrong_account_and_unavailable_snapshot_fail_closed(self):
        row = {"seven_day": {"used_percentage": 10, "resets_at": 2000}}
        self.assertIsNone(self.read(row, fingerprint="different"))
        self.assertIsNone(self.read(row, fetched=1011))
        self.assertIsNone(self.read(None))
        self.assertEqual(self.read(row), row)

    def test_adapter_hides_expired_window_but_retains_fresh_weekly_window(self):
        with tempfile.TemporaryDirectory(prefix="usage-cache-test-") as temporary:
            root = Path(temporary)
            profile = root / "profile.json"
            profile.write_text(json.dumps({"context_windows": {}, "usage_cache": str(root / "quota.json")}))
            windows = {"five_hour": {"used_percentage": 99, "resets_at": 1010},
                       "seven_day": {"used_percentage": 30, "resets_at": 2000}}
            snapshot = {"fetched_at": 1000, "account_fingerprint": "same", "windows": windows}
            source = TextIOWrapper(BytesIO(b'{"rate_limits":{"five_hour":{"used_percentage":100}}}'), encoding="utf-8")
            output = BytesIO()
            target = TextIOWrapper(output, encoding="utf-8", write_through=True)
            with patch.object(codex_usage, "selected_fingerprint", return_value="same"), \
                 patch.object(codex_usage, "_private_json", return_value=snapshot), \
                 patch.object(codex_usage.time, "time", return_value=1010), \
                 patch.object(codex_usage, "start_refresh") as refresh, \
                 patch.object(statusline_adapter.sys, "stdin", source), \
                 patch.object(statusline_adapter.sys, "stdout", target), \
                 patch.object(statusline_adapter.sys, "argv", ["adapter", str(profile)]):
                self.assertEqual(statusline_adapter.main(), 0)
                refresh.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["rate_limits"], {"seven_day": windows["seven_day"]})


if __name__ == "__main__":
    unittest.main()
