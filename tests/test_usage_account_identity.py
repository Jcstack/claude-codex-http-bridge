"""Ensure quota reads cannot silently cross selected-account identities."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import codex_usage


class UsageAccountIdentityTest(unittest.TestCase):
    def match(self, bridge, native):
        with patch.object(codex_usage, "active_bridge_auth", return_value=bridge), \
             patch.object(codex_usage, "_private_json", return_value={"tokens": native}):
            return codex_usage.matching_account_id(Path("fixture"))

    def test_shared_workspace_requires_a_known_matching_person(self):
        self.assertIsNone(self.match({"account_id": "workspace"}, {"account_id": "workspace"}))
        self.assertIsNone(self.match({"account_id": "workspace", "email": "a@example.invalid"},
                                     {"account_id": "workspace"}))
        self.assertIsNone(self.match({"account_id": "workspace", "email": "a@example.invalid"},
                                     {"account_id": "workspace", "email": "b@example.invalid"}))
        self.assertEqual(self.match({"account_id": "workspace", "email": "a@example.invalid"},
                                    {"account_id": "workspace", "email": "A@example.invalid"}), "workspace")

    def test_unicode_and_invalid_unicode_fail_safely(self):
        email = "\u7528\u6237@example.invalid"
        self.assertEqual(self.match({"account_id": "workspace", "email": email},
                                    {"account_id": "workspace", "email": email}), "workspace")
        self.assertIsNone(self.match({"account_id": "workspace", "email": "\ud800@example.invalid"},
                                     {"account_id": "workspace", "email": "\ud800@example.invalid"}))

    def test_unknown_native_subject_uses_selected_bridge_path(self):
        sentinel = {"accountId": "workspace"}
        with patch.object(codex_usage, "selected_fingerprint", return_value="selected"), \
             patch.object(codex_usage, "matching_account_id", return_value=None), \
             patch.object(codex_usage, "_bridge_rate_limits", return_value=sentinel) as bridge, \
             patch.object(codex_usage, "_rpc_rate_limits") as native:
            self.assertIs(codex_usage.selected_rate_limits(Path("fixture")), sentinel)
        bridge.assert_called_once()
        native.assert_not_called()

    def test_unknown_bridge_subject_cannot_reuse_cache_or_fetch_usage(self):
        with patch.object(codex_usage, "active_bridge_auth", return_value={"account_id": "workspace"}), \
             patch.object(codex_usage, "_private_json") as read_file, \
             patch.object(codex_usage.urllib.request, "build_opener") as open_network, \
             patch.object(codex_usage, "selected_rate_limits") as selected:
            root = Path("fixture")
            self.assertIsNone(codex_usage.selected_fingerprint(root))
            self.assertIsNone(codex_usage.read_cache(root / "usage.json", root))
            self.assertIsNone(codex_usage._bridge_rate_limits(root))
            self.assertFalse(codex_usage.refresh_cache(root / "usage.json", root))
        read_file.assert_not_called()
        open_network.assert_not_called()
        selected.assert_not_called()

    def test_malformed_bridge_identity_does_not_fetch_usage(self):
        record = {"account_id": "workspace", "email": "\ud800@example.invalid", "access_token": "fixture"}
        with patch.object(codex_usage, "active_bridge_auth", return_value=record), \
             patch.object(codex_usage.urllib.request, "build_opener") as open_network:
            self.assertIsNone(codex_usage.selected_fingerprint(Path("fixture")))
            self.assertIsNone(codex_usage._bridge_rate_limits(Path("fixture")))
        open_network.assert_not_called()

    def test_selection_change_during_native_read_discards_result(self):
        sentinel = {"accountId": "workspace"}
        with patch.object(codex_usage, "selected_fingerprint", side_effect=["person-a", "person-b"]), \
             patch.object(codex_usage, "matching_account_id", return_value="workspace"), \
             patch.object(codex_usage, "_rpc_rate_limits", return_value=sentinel):
            self.assertIsNone(codex_usage.selected_rate_limits(Path("fixture")))

    def test_native_account_change_during_read_discards_result(self):
        with patch.object(codex_usage, "selected_fingerprint", return_value="person-a"), \
             patch.object(codex_usage, "matching_account_id", side_effect=["workspace", None]), \
             patch.object(codex_usage, "_rpc_rate_limits", return_value={"accountId": "workspace"}):
            self.assertIsNone(codex_usage.selected_rate_limits(Path("fixture")))

    def test_unchanged_native_account_returns_result(self):
        sentinel = {"accountId": "workspace"}
        with patch.object(codex_usage, "selected_fingerprint", return_value="person-a"), \
             patch.object(codex_usage, "matching_account_id", return_value="workspace"), \
             patch.object(codex_usage, "_rpc_rate_limits", return_value=sentinel):
            self.assertIs(codex_usage.selected_rate_limits(Path("fixture")), sentinel)

    def test_cli_does_not_display_a_result_after_account_change(self):
        with patch.object(sys, "argv", ["codex_usage.py", "usage"]), \
             patch.object(codex_usage, "selected_account_id", return_value="workspace"), \
             patch.object(codex_usage, "selected_fingerprint", side_effect=["person-a", "person-b"]), \
             patch.object(codex_usage, "selected_rate_limits", return_value={"accountId": "workspace"}), \
             patch("builtins.print") as display:
            self.assertEqual(codex_usage.main(), 1)
        self.assertEqual(display.call_count, 1)
        self.assertIn("unavailable", display.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
