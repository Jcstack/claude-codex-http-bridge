"""Synthetic catalog handoff tests: no live accounts or services."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import accounts
import api_profile
import launcher
import rolling_runtime
import runtime_bundle
from test_accounts import tokens


MODEL = "gpt-6.1-sol"
SPEC = {"context_window": 872000, "input_modalities": ["text"], "supports_search_tool": True,
        "supported_reasoning_levels": [{"effort": level} for level in ("low", "medium", "high", "xhigh", "max")]}


class CatalogSyncTests(unittest.TestCase):
    def test_selected_catalog_requires_the_same_account_and_its_models(self):
        class Manager:
            state = {"pid": 123, "port": 1234, "session": "session-test"}
            def files(self): return [{"name": "one.json", "disabled": False}]
            def call(self, *_args): return {"models": [{"id": MODEL}]}

        local = [{"name": "one.json", "fingerprint": "fixture-a", "record": {"disabled": False}}]
        result = SimpleNamespace(stdout=json.dumps({MODEL: SPEC}))
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(accounts, "local_accounts", return_value=local), \
                patch.object(launcher, "state_is_live", return_value=True), \
                patch.object(rolling_runtime, "private_key", return_value="synthetic-local-key"), \
                patch.object(accounts, "request_json", return_value={"models": [{"slug": MODEL, **SPEC}]}), \
                patch.object(accounts.subprocess, "run", return_value=result):
            root = Path(temporary)
            self.assertEqual(accounts.selected_catalog(root, Manager()), {MODEL: SPEC})
            result.stdout = json.dumps({"gpt-unknown-sol": SPEC})
            with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                accounts.selected_catalog(root, Manager())
            result.stdout = json.dumps({MODEL: SPEC})
            with patch.object(accounts, "local_accounts", side_effect=[local, [
                    {"name": "one.json", "fingerprint": "fixture-b", "record": {"disabled": False}}]]):
                with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                    accounts.selected_catalog(root, Manager())
            extra = "gpt-6.2-sol"
            manager = Manager()
            with patch.object(manager, "call", return_value={"models": [{"id": MODEL}, {"id": extra}]}), \
                    patch.object(accounts, "request_json", return_value={"models": [
                        {"slug": MODEL, **SPEC}, {"slug": extra, **SPEC}]}):
                with self.assertRaisesRegex(RuntimeError, "inconsistent"):
                    accounts.selected_catalog(root, manager)

    def test_catalog_comparison_checks_context_and_effort_without_exposing_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime-managed"
            runtime_bundle.private_dir(runtime)
            runtime_bundle.private_dir(runtime / ("g-" + "a" * 24))
            api_profile.write_private(runtime / "origin-guard-key", "x" * 64)
            config = runtime / ("g-" + "a" * 24) / "config.yaml"
            api_profile.write_private(config, rolling_runtime.backend_config(runtime, 1, 2, {MODEL: SPEC}, "fixture"))
            generation = config.parent.name
            value = {"current": generation, "generations": {generation: {"pid": 123, "port": 1234}}}
            with patch.object(launcher, "listener_pids", return_value={123}):
                self.assertTrue(rolling_runtime.catalog_matches(root, value, {MODEL: SPEC}))
                self.assertFalse(rolling_runtime.catalog_matches(root, value, {MODEL: {**SPEC, "context_window": 123456}}))
                self.assertFalse(rolling_runtime.catalog_matches(root, value, {MODEL: {**SPEC, "supported_reasoning_levels": [{"effort": "low"}]}}))
                self.assertFalse(rolling_runtime.catalog_matches(root, value, {MODEL: SPEC, "gpt-6.2-sol": SPEC}))
                for bad in ({**SPEC, "supported_reasoning_levels": []},
                            {**SPEC, "supported_reasoning_levels": [{"effort": "low"}, {}]},
                            {**SPEC, "supported_reasoning_levels": [{"effort": "bad\nvalue"}]},
                            {**SPEC, "context_window": True}, {**SPEC, "context_window": 0}):
                    with self.assertRaisesRegex(RuntimeError, "metadata"):
                        rolling_runtime.catalog_matches(root, value, {MODEL: bad})
                api_profile.write_private(config, "broken")
                with self.assertRaisesRegex(RuntimeError, "configuration"):
                    rolling_runtime.catalog_matches(root, value, {MODEL: SPEC})

    def test_switch_rolls_back_selection_and_catalog_when_sync_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            old = {**accounts.token_record(tokens("synthetic-old")), "disabled": False}
            new = accounts.token_record(tokens("synthetic-new"))
            api_profile.write_private(root / "auth" / "old.json", json.dumps(old))
            staged = accounts.stage_path(root, "next")
            api_profile.write_private(staged, json.dumps(new))
            api_profile.write_private(staged.with_suffix(".checked"), json.dumps({
                "fingerprint": accounts.fingerprint(new), "checked_at": time.time()}))

            class Manager:
                def files(self):
                    return [{"name": row["name"], "disabled": row["record"]["disabled"]}
                            for row in accounts.local_accounts(root)]
                def call(self, method, path, body=None):
                    if method == "POST":
                        import urllib.parse
                        name = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["name"][0]
                        api_profile.write_private(root / "auth" / name, json.dumps(body))
                    return {"models": [{"id": MODEL}]}
                def set_disabled(self, name, disabled):
                    path = root / "auth" / name
                    api_profile.write_private(path, json.dumps({**accounts.private_json(path), "disabled": disabled}))

            calls = []
            def sync(_root, _management):
                selected = [row["name"] for row in accounts.local_accounts(root) if not row["record"]["disabled"]]
                calls.append(selected)
                if len(calls) == 1:
                    raise RuntimeError("synthetic catalog failure")
            with patch.object(accounts, "Management", return_value=Manager()), \
                    patch.object(accounts, "synchronize_managed_catalog", side_effect=sync), \
                    redirect_stdout(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "restored"):
                    accounts.switch_account(root, "next")
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0], ["old.json"])
            self.assertEqual(calls[1], ["old.json"])
            self.assertFalse((root / "accounts" / "switch.pending").exists())
            with patch.object(accounts, "Management", return_value=Manager()), \
                    patch.object(accounts, "synchronize_managed_catalog", side_effect=RuntimeError("synthetic unavailable")), \
                    redirect_stdout(StringIO()):
                with self.assertRaisesRegex(RuntimeError, "recovery"):
                    accounts.switch_account(root, "next")
                self.assertTrue((root / "accounts" / "switch.pending").exists())
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    accounts.recover_account(root)
                self.assertTrue((root / "accounts" / "switch.pending").exists())
            with patch.object(accounts, "Management", return_value=Manager()), \
                    patch.object(accounts, "synchronize_managed_catalog", return_value=False), \
                    redirect_stdout(StringIO()):
                accounts.recover_account(root)
            self.assertFalse((root / "accounts" / "switch.pending").exists())

    def test_waiting_launcher_cannot_enter_during_account_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entered = threading.Event()
            errors = []
            def wait_for_lock():
                try:
                    with accounts.switch_lock(root, wait_seconds=3):
                        entered.set()
                except Exception as error:
                    errors.append(error)
            with accounts.switch_lock(root):
                thread = threading.Thread(target=wait_for_lock)
                thread.start()
                self.assertFalse(entered.wait(.25))
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(entered.is_set())


if __name__ == "__main__":
    unittest.main()
