"""Login recovery uses local metadata, respects manual stop, and hides errors."""

import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

import autostart
import launcher
import runtime_bundle
import windows_acl


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        runtime_bundle.private_dir(self.root / "runtime")
        self.addCleanup(self.temporary.cleanup)

    def result(self):
        return json.loads((self.root / "runtime" / "autostart-status.json").read_text())

    def test_healthy_check_never_starts_or_calls_network(self):
        with patch.object(autostart, "healthy", return_value=True), \
             patch.object(launcher, "bridge_start", side_effect=AssertionError("unexpected start")), \
             patch.object(launcher, "helper_output", side_effect=AssertionError("unexpected verification")):
            self.assertEqual(autostart.run_once(self.root), 0)
        self.assertEqual(self.result()["result"], "healthy")

    def test_dead_service_verified_and_started_once(self):
        with patch.object(autostart, "healthy", side_effect=[RuntimeError("dead ingress"), True]), \
             patch.object(launcher, "helper_output") as verify, \
             patch.object(launcher, "bridge_start", return_value=0) as start:
            self.assertEqual(autostart.run_once(self.root), 0)
        verify.assert_called_once_with("verify")
        start.assert_called_once_with()
        self.assertEqual(self.result()["result"], "recovered")

    def test_manual_stop_keeps_periodic_recovery_paused(self):
        autostart.pause(self.root)
        with patch.object(autostart, "healthy", side_effect=AssertionError("must remain paused")), \
             patch.object(launcher, "bridge_start", side_effect=AssertionError("must remain paused")):
            self.assertEqual(autostart.run_once(self.root), 0)
        self.assertEqual(self.result()["result"], "paused")
        autostart.unpause(self.root)
        self.assertFalse((self.root / "runtime" / "autostart-paused").exists())

    def test_failed_recovery_does_not_log_exception_text(self):
        sentinel = "PRIVATE_FIXTURE_SENTINEL"
        with patch.object(autostart, "healthy", return_value=False), \
             patch.object(launcher, "helper_output", side_effect=RuntimeError(sentinel)):
            self.assertEqual(autostart.run_once(self.root), 1)
        self.assertEqual(self.result()["result"], "failed")
        self.assertNotIn(sentinel, (self.root / "runtime" / "autostart-status.json").read_text())

    def test_lock_refuses_a_simultaneous_start_stop(self):
        with autostart.operation_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, "in progress"):
                with autostart.operation_lock(self.root, wait_seconds=0): pass

    def test_login_definition_contains_paths_but_no_inherited_keys(self):
        sentinel = "PRIVATE_FIXTURE_SENTINEL"
        with patch.dict(os.environ, {"OPENAI_API_KEY": sentinel, "HTTPS_PROXY": sentinel}):
            definition = autostart.agent_definition(self.root, str(Path(sys.executable).absolute()))
        self.assertNotIn(sentinel.encode(), definition)
        plist = plistlib.loads(definition)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["StartInterval"], 120)
        self.assertEqual(set(plist["EnvironmentVariables"]), {"HOME", "PATH"})
        self.assertEqual(plist["ProgramArguments"][-1], str(self.root))

    def test_foreign_or_modified_login_agent_is_never_removed(self):
        path = self.root / "fixture.plist"
        path.write_bytes(autostart.agent_definition(self.root, str(Path(sys.executable).absolute())))
        self.assertTrue(autostart.agent_is_ours(path, self.root))
        data = plistlib.loads(path.read_bytes())
        data["EnvironmentVariables"]["OPENAI_API_KEY"] = "PRIVATE_FIXTURE_SENTINEL"
        path.write_bytes(plistlib.dumps(data))
        self.assertFalse(autostart.agent_is_ours(path, self.root))
        with patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart, "agent_path", return_value=path):
            with self.assertRaisesRegex(RuntimeError, "unrelated"):
                autostart.disable(self.root)
        self.assertTrue(path.exists())

    def test_manual_commands_serialize_and_pause_only_after_stop(self):
        with patch.object(launcher, "ROOT", self.root), patch.object(launcher, "helper_output"), \
             patch.object(launcher, "bridge_stop", side_effect=RuntimeError("active fixture")), \
             patch.object(launcher.sys, "argv", ["launcher.py", "bridge", "stop"]):
            with self.assertRaisesRegex(RuntimeError, "active fixture"): launcher.main()
        self.assertFalse((self.root / "runtime" / "autostart-paused").exists())
        with patch.object(launcher, "ROOT", self.root), patch.object(launcher, "helper_output"), \
             patch.object(launcher, "bridge_stop", return_value=0), \
             patch.object(launcher.sys, "argv", ["launcher.py", "bridge", "stop"]):
            self.assertEqual(launcher.main(), 0)
        self.assertTrue((self.root / "runtime" / "autostart-paused").exists())
        with patch.object(launcher, "ROOT", self.root), patch.object(launcher, "helper_output"), \
             patch.object(launcher, "bridge_start", return_value=0), \
             patch.object(launcher.sys, "argv", ["launcher.py", "bridge", "start"]):
            self.assertEqual(launcher.main(), 0)
        self.assertFalse((self.root / "runtime" / "autostart-paused").exists())

    def test_windows_startup_command_only_runs_our_private_watcher(self):
        pythonw = r"C:\Program Files\Python\pythonw.exe"
        value = autostart.windows_command(self.root, pythonw)
        self.assertTrue(autostart.windows_is_ours(self.root, value))
        self.assertFalse(autostart.windows_is_ours(self.root, value + " --unrelated"))
        self.assertFalse(autostart.windows_is_ours(self.root, value.replace("pythonw.exe", "other.exe")))
        self.assertFalse(autostart.windows_is_ours(self.root, "PRIVATE_FIXTURE_SENTINEL"))

    def test_windows_watch_checks_registry_each_cycle(self):
        value = autostart.windows_command(self.root, r"C:\Python\pythonw.exe")
        with patch.object(autostart, "windows_value", side_effect=[value, None]), \
             patch.object(autostart, "run_once", return_value=0) as check, \
             patch.object(autostart.time, "sleep") as sleep:
            self.assertEqual(autostart.windows_watch(self.root), 0)
        check.assert_called_once_with(self.root)
        sleep.assert_called_once_with(120)

    def test_windows_enable_spawns_with_sanitized_environment(self):
        pythonw = r"C:\Python\pythonw.exe"
        sentinel = "PRIVATE_FIXTURE_SENTINEL"
        with patch.dict(os.environ, {"OPENAI_API_KEY": sentinel, "HTTPS_PROXY": sentinel}), \
             patch.object(autostart, "windows_pythonw", return_value=pythonw), \
             patch.object(autostart, "windows_value", return_value=None), \
             patch.object(autostart, "windows_write") as write, \
             patch.object(windows_acl, "lock"), \
             patch.object(autostart.subprocess, "DETACHED_PROCESS", 8, create=True), \
             patch.object(autostart.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
             patch.object(autostart.subprocess, "Popen") as spawn:
            autostart.enable_windows(self.root)
        self.assertTrue(autostart.windows_is_ours(self.root, write.call_args.args[1]))
        self.assertNotIn("OPENAI_API_KEY", spawn.call_args.kwargs["env"])
        self.assertNotIn("HTTPS_PROXY", spawn.call_args.kwargs["env"])

    def test_windows_foreign_startup_value_is_untouched(self):
        with patch.object(autostart, "windows_value", return_value="unrelated fixture"), \
             patch.object(autostart, "windows_write") as write:
            with self.assertRaisesRegex(RuntimeError, "unrelated"): autostart.disable_windows(self.root)
        write.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows user registry required")
    def test_windows_user_registry_roundtrip_is_scoped_to_fixture(self):
        value = autostart.windows_command(self.root, r"C:\fixture\pythonw.exe")
        if autostart.windows_value(self.root) is not None:
            self.fail("Fixture startup name unexpectedly exists")
        try:
            autostart.windows_write(self.root, value)
            self.assertEqual(autostart.windows_value(self.root), value)
            autostart.disable_windows(self.root)
            self.assertIsNone(autostart.windows_value(self.root))
        finally:
            current = autostart.windows_value(self.root)
            if current == value: autostart.windows_write(self.root, None)


if __name__ == "__main__":
    unittest.main()
