"""Reject unreviewed or modified runtime code before changing an active choice."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
import sys
from unittest.mock import patch

import accounts
import launcher
import rolling_runtime
import runtime_bundle


class BundleTests(unittest.TestCase):
    def test_native_compaction_headers_are_retained_in_background_settings(self):
        import rolling_runtime
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "settings.json"
            rolling_runtime.write(base, {"env": {"UNCHANGED_FIXTURE": "kept"}})
            env = {"ANTHROPIC_CUSTOM_HEADERS": "X-Codex-Bridge-Native-Compaction: 1"}
            saved = rolling_runtime.client_settings(base, env, root)
            self.assertEqual(json.loads(saved.read_text())["env"], {**env, "UNCHANGED_FIXTURE": "kept"})

    def fixture(self, root):
        proxy, ingress = root / "proxy-input", root / "ingress-input"
        proxy.write_bytes(b"synthetic-proxy-fixture"); ingress.write_bytes(b"synthetic-ingress-fixture")
        bundle = root / "bundle"
        runtime_bundle.pack(proxy, ingress, bundle)
        return bundle

    def test_modified_binary_and_source_are_rejected_before_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime_bundle.private_dir(root)
            bundle = self.fixture(root)
            destination = root / "install"; runtime_bundle.private_dir(destination)
            first = runtime_bundle.activate(destination, bundle)
            selected = (destination / "runtime-selected.json").read_bytes()
            value, identity = runtime_bundle.verify(bundle)
            self.assertEqual(first, identity)
            binary = bundle / value["binaries"]["proxy"]["file"]
            binary.write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                runtime_bundle.activate(destination, bundle)
            self.assertEqual((destination / "runtime-selected.json").read_bytes(), selected)
            value["source"]["upstream"] = "0" * 40
            (bundle / "bundle.json").write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError, "source lock"):
                runtime_bundle.verify(bundle)

    def test_bundle_paths_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime_bundle.private_dir(root)
            bundle = self.fixture(root)
            value, _ = runtime_bundle.verify(bundle)
            value["binaries"]["proxy"]["file"] = "../credential-fixture"
            (bundle / "bundle.json").write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError, "path"):
                runtime_bundle.verify(bundle)

    def test_previously_selected_runtime_survives_launcher_source_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime_bundle.private_dir(root)
            bundle = self.fixture(root)
            destination = root / "install"; runtime_bundle.private_dir(destination)
            identity = runtime_bundle.activate(destination, bundle)
            changed = {"upstream": "0" * 40, "files": {}}
            with patch.object(runtime_bundle, "source_lock", return_value=changed):
                self.assertEqual(runtime_bundle.selected(destination)[2], identity)
                with self.assertRaisesRegex(RuntimeError, "source lock"):
                    runtime_bundle.activate(destination, bundle)
                installed = destination / "runtimes" / identity / "bundle.json"
                value = json.loads(installed.read_text())
                value["source"] = changed
                installed.write_text(json.dumps(value))
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    runtime_bundle.selected(destination)

    def test_upgrade_failure_restores_previous_runtime_selection(self):
        for had_previous in (False, True):
            with self.subTest(had_previous=had_previous), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); runtime_bundle.private_dir(root)
                old_dir = root / "old"; old_dir.mkdir()
                new_dir = root / "new"; new_dir.mkdir()
                old_bundle = self.fixture(old_dir)
                self.fixture(new_dir)
                # Give the new fixture a distinct identity.
                proxy = new_dir / "proxy-input"; proxy.write_bytes(b"different-synthetic-proxy")
                candidate = new_dir / "candidate"
                runtime_bundle.pack(proxy, new_dir / "ingress-input", candidate)
                install = root / "install"; runtime_bundle.private_dir(install)
                if had_previous:
                    runtime_bundle.activate(install, old_bundle)
                before = runtime_bundle.selection_snapshot(install)
                with patch.object(launcher, "ROOT", install), patch.object(launcher, "service_lock", return_value=nullcontext()), \
                     patch.object(launcher, "helper_output", return_value=""), \
                     patch.object(accounts, "switch_lock", return_value=nullcontext()), \
                     patch.object(launcher, "ensure_service", return_value=(install / "runtime", {"fixture": True}, None)), \
                     patch.object(launcher, "managed_catalog", return_value=({}, lambda: None)), \
                     patch.object(rolling_runtime, "ensure", side_effect=RuntimeError("synthetic readiness failure")), \
                     patch.object(sys, "argv", ["launcher.py", "bridge", "upgrade", str(candidate)]):
                    with self.assertRaisesRegex(RuntimeError, "synthetic readiness failure"):
                        launcher.main()
                self.assertEqual(runtime_bundle.selection_snapshot(install), before)
                if had_previous:
                    self.assertEqual(runtime_bundle.selected(install)[2], json.loads(before)["bundle"])

    def test_failed_upgrade_does_not_overwrite_another_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime_bundle.private_dir(root)
            previous = runtime_bundle.canonical({"bundle": "a" * 64})
            attempted = "b" * 64
            changed = runtime_bundle.canonical({"bundle": "c" * 64})
            runtime_bundle.write_file(root / "runtime-selected.json", changed, 0o600)
            if os.name == "nt":
                import windows_acl
                windows_acl.lock([root / "runtime-selected.json"])
            with self.assertRaisesRegex(RuntimeError, "selection changed"):
                runtime_bundle.rollback_selection(root, previous, attempted)
            self.assertEqual((root / "runtime-selected.json").read_bytes(), changed)

    def test_upgrade_activation_error_after_selection_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); runtime_bundle.private_dir(root)
            bundle = self.fixture(root)
            install = root / "install"; runtime_bundle.private_dir(install)
            real_activate = runtime_bundle.activate

            def fail_after_selection(destination, source):
                real_activate(destination, source)
                raise RuntimeError("synthetic post-selection failure")

            with patch.object(launcher, "ROOT", install), patch.object(launcher, "service_lock", return_value=nullcontext()), \
                 patch.object(launcher, "helper_output", return_value=""), \
                 patch.object(accounts, "switch_lock", return_value=nullcontext()), \
                 patch.object(runtime_bundle, "activate", side_effect=fail_after_selection), \
                 patch.object(sys, "argv", ["launcher.py", "bridge", "upgrade", str(bundle)]):
                with self.assertRaisesRegex(RuntimeError, "synthetic post-selection failure"):
                    launcher.main()
            self.assertIsNone(runtime_bundle.selection_snapshot(install))


if __name__ == "__main__": unittest.main()
