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
import windows_acl


class BundleTests(unittest.TestCase):
    def test_recovery_readiness_requires_both_authenticated_catalogs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = {"port": 12345}
            good = {"data": [{"id": "gpt-fixture"}]}
            with patch.object(rolling_runtime, "endpoint_status") as status, \
                 patch.object(rolling_runtime, "private_key", return_value="synthetic-key"), \
                 patch.object(rolling_runtime, "get_json", side_effect=[good, good]) as get:
                rolling_runtime.preflight_routes(root, value, {"gpt-fixture": {}})
            status.assert_called_once_with(root, value)
            self.assertEqual([call.args[2] for call in get.call_args_list], ["/bridge/origin/v1/models", "/v1/models"])
            for origin, frontend in (({"data": []}, good), (good, {"data": []}),
                                     (good, {"data": [{"id": "gpt-other"}]})):
                with self.subTest(origin=origin, frontend=frontend), \
                     patch.object(rolling_runtime, "endpoint_status"), \
                     patch.object(rolling_runtime, "private_key", return_value="synthetic-key"), \
                     patch.object(rolling_runtime, "get_json", side_effect=[origin, frontend]):
                    with self.assertRaisesRegex(RuntimeError, "catalog"):
                        rolling_runtime.preflight_routes(root, value, {"gpt-fixture": {}})

    def test_managed_process_environment_excludes_unrelated_shell_secrets(self):
        sentinel = "PRIVATE_FIXTURE_SENTINEL"
        inherited = {"OPENAI_API_KEY": sentinel, "ANTHROPIC_AUTH_TOKEN": sentinel,
                     "GITHUB_TOKEN": sentinel, "AWS_SECRET_ACCESS_KEY": sentinel,
                     "HTTPS_PROXY": "http://fixture:private@proxy.invalid",
                     "CLAUDE_CODEX_LOOPBACK_CA": sentinel, "SSL_CERT_FILE": "/fixture/ca.pem", "GOGC": "75"}
        with patch.dict(os.environ, inherited):
            environment = rolling_runtime.process_environment()
            self.assertEqual(os.environ["OPENAI_API_KEY"], sentinel)
        for name in inherited:
            if name not in ("SSL_CERT_FILE", "GOGC"):
                self.assertNotIn(name, environment)
        self.assertEqual(environment["SSL_CERT_FILE"], "/fixture/ca.pem")
        self.assertEqual(environment["GOGC"], "75")
        self.assertEqual(environment["NO_PROXY"], "127.0.0.1,localhost")
        self.assertEqual(environment["no_proxy"], "127.0.0.1,localhost")

    def test_managed_spawn_uses_only_explicit_loopback_trust(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"OPENAI_API_KEY": "PRIVATE_FIXTURE_SENTINEL",
                                        "CLAUDE_CODEX_LOOPBACK_CA": "stale-untrusted-path"}), \
                patch.object(runtime_bundle, "safe_file"), patch.object(windows_acl, "lock"), \
                patch.object(rolling_runtime.subprocess, "Popen") as spawn:
            root = Path(temporary)
            ca = root / "fixture-ca.pem"
            rolling_runtime.start(root / "fixture-binary", [], root / "fixture.log",
                                  ca=ca, authority="127.0.0.1:12345")
            environment = spawn.call_args.kwargs["env"]
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertEqual(environment["CLAUDE_CODEX_LOOPBACK_CA"], str(ca))
            self.assertEqual(environment["CLAUDE_CODEX_LOOPBACK_AUTHORITY"], "127.0.0.1:12345")

    def test_real_managed_child_does_not_receive_shell_credentials(self):
        child = ("import json,os; print(json.dumps({"
                 "'secret_present': 'OPENAI_API_KEY' in os.environ, "
                 "'proxy_present': 'HTTPS_PROXY' in os.environ, "
                 "'loopback_only': os.environ.get('NO_PROXY') == '127.0.0.1,localhost'}))")
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"OPENAI_API_KEY": "PRIVATE_FIXTURE_SENTINEL",
                                        "HTTPS_PROXY": "http://fixture:private@proxy.invalid"}), \
                patch.object(runtime_bundle, "safe_file"):
            root = Path(temporary)
            log = root / "child.log"
            process = rolling_runtime.start(Path(sys.executable), ["-c", child], log)
            self.assertEqual(process.wait(timeout=15), 0)
            self.assertEqual(json.loads(log.read_text()),
                             {"secret_present": False, "proxy_present": False, "loopback_only": True})

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
