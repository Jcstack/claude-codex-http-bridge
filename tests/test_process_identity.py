"""Reject stale or foreign origin owners before any localhost key is used."""

import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import api_profile
import accounts
import launcher
import process_identity
import runtime_bundle


class ProcessIdentityTests(unittest.TestCase):
    def test_linux_truncated_utf8_process_name_does_not_hide_start_tick(self):
        boot = "12345678-1234-1234-1234-123456789abc"
        fields = [b"S"] + [b"0"] * 18 + [b"12345"] + [b"0"] * 3
        raw_stat = b"321 (fixture-\xce) " + b" ".join(fields)
        executable = "/synthetic/fixture-\udcff"
        with patch.object(process_identity.platform, "system", return_value="Linux"), \
             patch.object(Path, "read_text", return_value=boot), \
             patch.object(Path, "read_bytes", return_value=raw_stat), \
             patch.object(os, "readlink", return_value=executable):
            self.assertEqual(process_identity.capture(321), (boot + ":12345", executable))

    def test_origin_environment_omits_unrelated_secrets_and_store_overrides(self):
        sentinel = "SYNTHETIC_PRIVATE_FIXTURE"
        values = {"OPENAI_API_KEY": sentinel, "GITHUB_TOKEN": sentinel, "HOME_JWT": sentinel,
                  "PGSTORE_DSN": sentinel, "GITSTORE_GIT_URL": sentinel, "GITSTORE_GIT_TOKEN": sentinel,
                  "OBJECTSTORE_ENDPOINT": sentinel, "OBJECTSTORE_SECRET_KEY": sentinel,
                  "MANAGEMENT_PASSWORD": sentinel, "https_proxy": "http://127.0.0.1:8001",
                  "no_proxy": "example.invalid", "SSL_CERT_FILE": "/fixture/ca.pem"}
        with patch.dict(os.environ, values):
            environment = launcher.origin_process_environment()
        for name in ("OPENAI_API_KEY", "GITHUB_TOKEN", "HOME_JWT", "PGSTORE_DSN", "GITSTORE_GIT_URL",
                     "GITSTORE_GIT_TOKEN", "OBJECTSTORE_ENDPOINT", "OBJECTSTORE_SECRET_KEY", "MANAGEMENT_PASSWORD"):
            self.assertNotIn(name, environment)
        self.assertEqual(environment["https_proxy"], "http://127.0.0.1:8001")
        self.assertIn("example.invalid", environment["no_proxy"])
        self.assertIn("127.0.0.1", environment["no_proxy"])
        self.assertEqual(environment["SSL_CERT_FILE"], "/fixture/ca.pem")

    def test_real_self_identity_is_stable_and_invalid_birth_is_rejected(self):
        pid = os.getpid()
        identity = process_identity.capture(pid)
        self.assertTrue(process_identity.executable_matches(identity, Path(identity[1])))
        mark = process_identity.stamp(pid, identity)
        self.assertTrue(process_identity.matches(pid, mark))
        self.assertFalse(process_identity.matches(pid, "0" * 64))
        self.assertFalse(process_identity.matches(pid, "invalid"))

    def test_listener_reuse_and_legacy_adoption_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary, socket.socket() as listener:
            root = Path(temporary)
            runtime = root / "runtime"
            session = runtime / "session-fixture"
            runtime_bundle.private_dir(runtime)
            runtime_bundle.private_dir(session)
            for name in ("config.yaml", "key", "proxy.log"):
                api_profile.write_private(session / name, "SYNTHETIC_FIXTURE")
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            state = {"pid": os.getpid(), "port": listener.getsockname()[1], "session": session.name}
            api_profile.write_private(runtime / "service.json", json.dumps(state))
            with patch.object(launcher, "ROOT", root), \
                 patch.object(launcher, "service_proxy", return_value=Path(process_identity.capture(os.getpid())[1])):
                self.assertTrue(launcher.state_is_live(runtime, state))
                _, adopted, created = launcher.ensure_service(require_catalog=False)
                self.assertFalse(created)
                self.assertEqual(adopted["process_identity"], process_identity.stamp(os.getpid()))
                self.assertTrue(launcher.state_is_live(runtime, adopted))
                self.assertFalse(launcher.state_is_live(runtime, {**adopted, "process_identity": "0" * 64}))
                real = process_identity.capture(os.getpid())
                changed = (real[0] + "-different-birth", real[1])
                with patch.object(process_identity, "capture", side_effect=[real, changed]):
                    self.assertFalse(launcher.state_is_live(runtime, adopted))
            # A legacy PID/port that belongs to a different executable is not
            # adopted, contacted with a key, killed, or removed from state.
            listener.settimeout(.01)
            while True:
                try:
                    connection, _ = listener.accept()
                    connection.close()
                except TimeoutError:
                    break
            api_profile.write_private(runtime / "service.json", json.dumps(state))
            with patch.object(launcher, "ROOT", root), patch.object(launcher, "service_proxy", return_value=root / "foreign-executable"), \
                 patch.object(launcher, "helper_output", side_effect=AssertionError("must not start")):
                self.assertFalse(launcher.state_is_live(runtime, state))
                with self.assertRaisesRegex(RuntimeError, "occupied port"):
                    launcher.ensure_service(require_catalog=False)
                self.assertEqual(launcher.read_state(runtime), state)
                listener.settimeout(.1)
                with self.assertRaises(TimeoutError): listener.accept()

    def test_invalid_private_identity_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            state = {"pid": os.getpid(), "port": 12345, "session": "session-fixture", "process_identity": "bad"}
            api_profile.write_private(runtime / "service.json", json.dumps(state))
            with self.assertRaisesRegex(RuntimeError, "identity"):
                launcher.read_state(runtime)

    def test_account_management_rechecks_owner_before_sending_key(self):
        owner = object.__new__(accounts.Management)
        owner.root = Path("synthetic-root")
        owner.state = {"pid": 1, "port": 1, "session": "session-fixture"}
        owner.base = "http://127.0.0.1:1/v0/management/"
        owner.key = "SYNTHETIC_PRIVATE_FIXTURE"
        with patch.object(launcher, "state_is_live", return_value=False), \
             patch.object(accounts, "request_json") as send:
            with self.assertRaisesRegex(RuntimeError, "no key was sent"):
                owner.call("GET", "auth-files")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
