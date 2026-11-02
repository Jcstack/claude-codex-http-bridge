"""Reject stale or foreign origin owners before any localhost key is used."""

import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import api_profile
import launcher
import process_identity
import runtime_bundle


class ProcessIdentityTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
