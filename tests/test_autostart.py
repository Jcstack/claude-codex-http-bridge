"""Login recovery uses local metadata, respects manual stop, and hides errors."""

import json
import io
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import autostart
import launcher
import runtime_bundle
import windows_acl


class AutostartTests(unittest.TestCase):
    @staticmethod
    def system_environment():
        # Native Windows ACL tools still need the operating-system paths when
        # a synthetic login environment removes inherited application keys.
        allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
                   "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA"}
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

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
        self.assertNotIn("reason", self.result())
        self.assertNotIn(sentinel, (self.root / "runtime" / "autostart-status.json").read_text())

    def test_lock_refuses_a_simultaneous_start_stop(self):
        with autostart.operation_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, "in progress"):
                with autostart.operation_lock(self.root, wait_seconds=0): pass

    def test_macos_enable_publishes_policy_before_loading_agent(self):
        desired = {"format": 1, "source": "system", "schemes": ["https"], "bypass": "fixture.local"}
        agent = self.root / "fixture.plist"
        observed = {}

        def bootstrap(*arguments):
            observed["arguments"] = arguments
            observed["policy"] = autostart.read_macos_network_policy(self.root)
            observed["paused"] = (self.root / "runtime" / "autostart-paused").exists()
            try:
                with autostart.operation_lock(self.root, wait_seconds=0): pass
            except RuntimeError:
                observed["locked"] = True
            return SimpleNamespace(returncode=0)

        with patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart.os, "getuid", return_value=1000, create=True), \
             patch.object(autostart, "agent_path", return_value=agent), \
             patch.object(autostart, "capture_macos_network_policy", return_value=desired), \
             patch.object(autostart, "agent_loaded", side_effect=[False, True]), \
             patch.object(autostart, "launchctl", side_effect=bootstrap):
            autostart.enable(self.root)
        self.assertEqual(observed["arguments"][0], "bootstrap")
        self.assertEqual(observed["policy"], desired)
        self.assertTrue(observed["paused"])
        self.assertTrue(observed.get("locked"))
        self.assertFalse((self.root / "runtime" / "autostart-paused").exists())
        self.assertTrue(autostart.agent_is_ours(agent, self.root))

    def test_macos_failed_bootstrap_leaves_retryable_agent_paused(self):
        desired = {"format": 1, "source": "system", "schemes": ["https"], "bypass": "fixture.local"}
        agent = self.root / "fixture.plist"
        with patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart.os, "getuid", return_value=1000, create=True), \
             patch.object(autostart, "agent_path", return_value=agent), \
             patch.object(autostart, "capture_macos_network_policy", return_value=desired), \
             patch.object(autostart, "agent_loaded", return_value=False), \
             patch.object(autostart, "launchctl", return_value=SimpleNamespace(returncode=1)) as command:
            with self.assertRaisesRegex(RuntimeError, "Could not load"):
                autostart.enable(self.root)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0], "bootstrap")
        self.assertTrue(autostart.agent_is_ours(agent, self.root))
        self.assertTrue((self.root / "runtime" / "autostart-paused").exists())
        self.assertEqual(autostart.read_macos_network_policy(self.root), desired)
        with patch.object(autostart, "healthy", side_effect=AssertionError("must stay paused")):
            self.assertEqual(autostart.run_once(self.root), 0)
        self.assertEqual(self.result()["result"], "paused")

    def test_macos_policy_write_failure_does_not_bootstrap(self):
        desired = {"format": 1, "source": "system", "schemes": ["https"], "bypass": "fixture.local"}
        old = {"format": 1, "source": "system", "schemes": ["http"], "bypass": "old.fixture"}
        agent = self.root / "fixture.plist"
        runtime_bundle.write_file(self.root / "runtime" / "autostart-network.json",
                                  runtime_bundle.canonical(old), 0o600)
        with patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart, "agent_path", return_value=agent), \
             patch.object(autostart, "capture_macos_network_policy", return_value=desired), \
             patch.object(runtime_bundle, "write_file", side_effect=OSError("synthetic write failure")), \
             patch.object(autostart, "launchctl") as command:
            with self.assertRaises(OSError):
                autostart.enable(self.root)
        command.assert_not_called()
        self.assertFalse(agent.exists())
        self.assertTrue((self.root / "runtime" / "autostart-paused").exists())
        self.assertEqual(autostart.read_macos_network_policy(self.root), old)

    def test_macos_timed_out_bootstrap_also_stays_paused(self):
        desired = {"format": 1, "source": "system", "schemes": ["https"], "bypass": "fixture.local"}
        agent = self.root / "fixture.plist"
        with patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart.os, "getuid", return_value=1000, create=True), \
             patch.object(autostart, "agent_path", return_value=agent), \
             patch.object(autostart, "capture_macos_network_policy", return_value=desired), \
             patch.object(autostart, "agent_loaded", return_value=False), \
             patch.object(autostart, "launchctl", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                autostart.enable(self.root)
        self.assertTrue(autostart.agent_is_ours(agent, self.root))
        self.assertTrue((self.root / "runtime" / "autostart-paused").exists())
        self.assertEqual(autostart.read_macos_network_policy(self.root), desired)

    def test_login_check_waits_for_an_in_progress_operation(self):
        done = threading.Event()
        results = []

        def check():
            results.append(autostart.run_once(self.root))
            done.set()

        with patch.object(autostart, "healthy", return_value=True):
            with autostart.operation_lock(self.root):
                worker = threading.Thread(target=check)
                worker.start()
                self.assertFalse(done.wait(.3))
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [0])
        self.assertEqual(self.result()["result"], "healthy")

    def test_busy_login_check_does_not_overwrite_previous_status(self):
        autostart.record(self.root, "healthy")
        previous = (self.root / "runtime" / "autostart-status.json").read_bytes()
        with patch.object(autostart, "operation_lock", side_effect=autostart.OperationBusy("busy")):
            self.assertEqual(autostart.run_once(self.root), 0)
        self.assertEqual((self.root / "runtime" / "autostart-status.json").read_bytes(), previous)

    def test_login_check_rechecks_enablement_after_waiting_for_lock(self):
        enabled = threading.Event()
        enabled.set()
        done = threading.Event()
        results = []

        def check():
            results.append(autostart.run_once(self.root, enabled=enabled.is_set))
            done.set()

        with patch.object(autostart, "healthy", side_effect=AssertionError("disabled recovery must not run")) as health:
            with autostart.operation_lock(self.root):
                worker = threading.Thread(target=check)
                worker.start()
                self.assertFalse(done.wait(.3))
                enabled.clear()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [0])
        health.assert_not_called()
        self.assertFalse((self.root / "runtime" / "autostart-status.json").exists())

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

    def test_macos_login_restores_system_proxy_without_persisting_url_or_credentials(self):
        proxy = "http://user:PRIVATE_FIXTURE_SENTINEL@127.0.0.1:4567"
        settings = {"exclude_simple": False, "exceptions": ["exact.example"]}
        with patch.dict(os.environ, {**self.system_environment(), "https_proxy": proxy, "no_proxy": "localhost"}, clear=True), \
             patch.object(autostart, "macos_proxy_settings", return_value=({"https": proxy}, settings)):
            policy = autostart.capture_macos_network_policy()
        self.assertNotIn("PRIVATE_FIXTURE_SENTINEL", json.dumps(policy))
        self.assertNotIn("127.0.0.1", json.dumps(policy))
        self.assertEqual(policy["bypass"], "localhost")
        runtime_bundle.write_file(self.root / "runtime" / "autostart-network.json",
                                  runtime_bundle.canonical(policy), 0o600)
        with patch.dict(os.environ, self.system_environment(), clear=True), \
             patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart, "macos_proxy_settings", return_value=({"https": proxy}, settings)), \
             patch.object(autostart, "verify_local_proxy_listener") as listener:
            with autostart.startup_network_environment(self.root):
                child = launcher.origin_process_environment()
                self.assertEqual(child["HTTPS_PROXY"], proxy)
                self.assertEqual(child["https_proxy"], proxy)
                self.assertIn("localhost", child["NO_PROXY"])
                self.assertNotIn("OPENAI_API_KEY", child)
            self.assertNotIn("HTTPS_PROXY", os.environ)
            self.assertNotIn("NO_PROXY", os.environ)
            listener.assert_called_once_with("127.0.0.1", 4567)

    def test_macos_login_fails_closed_when_saved_system_proxy_disappears(self):
        policy = {"format": 1, "source": "system", "schemes": ["https"], "bypass": ""}
        runtime_bundle.write_file(self.root / "runtime" / "autostart-network.json",
                                  runtime_bundle.canonical(policy), 0o600)
        with patch.dict(os.environ, self.system_environment(), clear=True), \
             patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart, "macos_proxy_settings", return_value=({}, {})):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                with autostart.startup_network_environment(self.root): pass
            self.assertNotIn("HTTPS_PROXY", os.environ)

    def test_recovery_uses_saved_system_proxy_and_fails_closed_if_missing(self):
        policy = {"format": 1, "source": "system", "schemes": ["https"], "bypass": ""}
        runtime_bundle.write_file(self.root / "runtime" / "autostart-network.json",
                                  runtime_bundle.canonical(policy), 0o600)
        proxy = "http://127.0.0.1:4567"

        def start():
            self.assertEqual(launcher.origin_process_environment()["HTTPS_PROXY"], proxy)
            return 0

        with patch.dict(os.environ, self.system_environment(), clear=True), \
             patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart, "macos_proxy_settings", return_value=({"https": proxy}, {})), \
             patch.object(autostart, "verify_local_proxy_listener"), \
             patch.object(autostart, "healthy", side_effect=[False, True]), \
             patch.object(launcher, "helper_output"), patch.object(launcher, "bridge_start", side_effect=start):
            self.assertEqual(autostart.run_once(self.root), 0)
            self.assertNotIn("HTTPS_PROXY", os.environ)
        self.assertEqual(self.result()["result"], "recovered")

        with patch.dict(os.environ, self.system_environment(), clear=True), \
             patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart, "macos_proxy_settings", return_value=({}, {})), \
             patch.object(autostart, "healthy", return_value=False), \
             patch.object(launcher, "bridge_start") as start_mock:
            self.assertEqual(autostart.run_once(self.root), 1)
            start_mock.assert_not_called()
        self.assertEqual(self.result()["result"], "failed")
        self.assertEqual(self.result()["reason"], "network_configuration")

    def test_macos_proxy_capture_rejects_mismatch_and_unrepresentable_bypass(self):
        with patch.dict(os.environ, {**self.system_environment(), "https_proxy": "http://127.0.0.1:1111"}, clear=True), \
             patch.object(autostart, "macos_proxy_settings",
                          return_value=({"https": "http://127.0.0.1:2222"}, {})):
            with self.assertRaisesRegex(RuntimeError, "differ"):
                autostart.capture_macos_network_policy()
        with self.assertRaisesRegex(RuntimeError, "bypass"):
            autostart.macos_bypass({"exceptions": ["exact.example"]})
        with self.assertRaisesRegex(RuntimeError, "bypass"):
            autostart.macos_bypass({"exclude_simple": True})
        self.assertEqual(autostart.macos_bypass({"exceptions": ["*.example.test", "127.0.0.0/8"]}),
                         "*.example.test,127.0.0.0/8")

    def test_explicit_startup_proxy_is_not_overwritten(self):
        with patch.dict(os.environ, {**self.system_environment(), "https_proxy": "http://127.0.0.1:3210"}, clear=True), \
             patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart, "macos_proxy_settings", side_effect=AssertionError("unexpected system lookup")):
            with autostart.startup_network_environment(self.root):
                self.assertEqual(os.environ["https_proxy"], "http://127.0.0.1:3210")

    def test_direct_system_network_allows_irrelevant_bypass_settings(self):
        with patch.dict(os.environ, self.system_environment(), clear=True), \
             patch.object(autostart, "macos_proxy_settings",
                          return_value=({}, {"exclude_simple": True})):
            policy = autostart.capture_macos_network_policy()
        self.assertIsNone(policy["schemes"])

    def test_dynamic_system_policy_rejects_unrepresentable_bypass_at_login(self):
        with patch.dict(os.environ, self.system_environment(), clear=True), \
             patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart, "macos_proxy_settings",
                          return_value=({"https": "http://127.0.0.1:3210"},
                                        {"exceptions": ["exact.example"]})):
            with self.assertRaisesRegex(RuntimeError, "bypass"):
                with autostart.startup_network_environment(self.root): pass
            self.assertNotIn("HTTPS_PROXY", os.environ)

    def test_local_proxy_check_waits_for_a_listener_and_skips_remote_hosts(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(autostart.NetworkPolicyError, "unavailable"):
                autostart.verify_local_proxy_listener("127.0.0.1", port)
            listener.listen()
            autostart.verify_local_proxy_listener("127.0.0.1", port)
        with patch.object(autostart.socket, "create_connection", side_effect=AssertionError("external connection")):
            autostart.verify_local_proxy_listener("proxy.example.test", 8080)

    @unittest.skipUnless(sys.platform == "darwin", "macOS SystemConfiguration required")
    def test_automatic_system_proxy_discovery_is_not_evaluated(self):
        class Output:
            stdout = "ProxyAutoConfigEnable : 1\n"
        with patch.object(autostart.subprocess, "run", return_value=Output()):
            with self.assertRaisesRegex(RuntimeError, "not supported"):
                autostart.macos_proxy_settings()

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

    def test_status_only_returns_allowlisted_metadata(self):
        sentinel = "PRIVATE_FIXTURE_SENTINEL"
        autostart.api_profile.write_private(self.root / "runtime" / "autostart-status.json",
            json.dumps({"result": "failed", "time": 1, "reason": "network_configuration", "extra": sentinel}))
        output = io.StringIO()
        with patch.object(autostart.platform, "system", return_value="Darwin"), \
             patch.object(autostart, "agent_path", return_value=self.root / "absent.plist"), \
             redirect_stdout(output):
            self.assertEqual(autostart.main(["status"], root=self.root), 0)
        self.assertNotIn(sentinel, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["last"]["reason"], "network_configuration")

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
        observations = []
        def check(root, *, enabled):
            observations.append((root == self.root, enabled()))
            return 0
        with patch.object(autostart, "windows_value", side_effect=[value, None, None]), \
             patch.object(autostart, "run_once", side_effect=check) as run, \
             patch.object(autostart.time, "sleep") as sleep:
            self.assertEqual(autostart.windows_watch(self.root), 0)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(observations, [(True, False)])
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

    def test_windows_enable_serializes_the_startup_registry_write(self):
        observed = []
        def write(root, value):
            try:
                with autostart.operation_lock(root, wait_seconds=0): pass
            except autostart.OperationBusy:
                observed.append(True)
            else:
                observed.append(False)

        with patch.object(autostart, "windows_pythonw", return_value=r"C:\Python\pythonw.exe"), \
             patch.object(autostart, "windows_value", return_value=None), \
             patch.object(autostart, "windows_write", side_effect=write), \
             patch.object(windows_acl, "lock"), \
             patch.object(autostart.subprocess, "DETACHED_PROCESS", 8, create=True), \
             patch.object(autostart.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
             patch.object(autostart.subprocess, "Popen"):
            autostart.enable_windows(self.root)
        self.assertEqual(observed, [True])

    def test_windows_failed_enable_restores_registry_and_previous_pause(self):
        autostart.pause(self.root)
        state = {"value": None}
        def write(_root, value):
            state["value"] = value

        with patch.object(autostart, "windows_pythonw", return_value=r"C:\Python\pythonw.exe"), \
             patch.object(autostart, "windows_value", side_effect=lambda _root: state["value"]), \
             patch.object(autostart, "windows_write", side_effect=write), \
             patch.object(windows_acl, "lock"), \
             patch.object(autostart.subprocess, "DETACHED_PROCESS", 8, create=True), \
             patch.object(autostart.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
             patch.object(autostart.subprocess, "Popen", side_effect=OSError("synthetic spawn failure")):
            with self.assertRaises(OSError):
                autostart.enable_windows(self.root)
        self.assertIsNone(state["value"])
        self.assertTrue((self.root / "runtime" / "autostart-paused").exists())

    def test_windows_disable_waits_for_enable_and_removes_its_entry(self):
        state = {"value": None}
        entered_spawn, finish_spawn, disabled = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def write(_root, value):
            state["value"] = value
        def spawn(*_args, **_kwargs):
            entered_spawn.set()
            if not finish_spawn.wait(5):
                raise RuntimeError("Synthetic spawn did not finish")
        def enable():
            try: autostart.enable_windows(self.root)
            except Exception as error: errors.append(type(error).__name__)
        def disable():
            try:
                with autostart.operation_lock(self.root): autostart.disable_windows(self.root)
            except Exception as error: errors.append(type(error).__name__)
            finally: disabled.set()

        with patch.object(autostart, "windows_pythonw", return_value=r"C:\Python\pythonw.exe"), \
             patch.object(autostart, "windows_value", side_effect=lambda _root: state["value"]), \
             patch.object(autostart, "windows_write", side_effect=write), \
             patch.object(windows_acl, "lock"), \
             patch.object(autostart.subprocess, "DETACHED_PROCESS", 8, create=True), \
             patch.object(autostart.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
             patch.object(autostart.subprocess, "Popen", side_effect=spawn):
            start_thread = threading.Thread(target=enable)
            stop_thread = threading.Thread(target=disable)
            start_thread.start()
            try:
                self.assertTrue(entered_spawn.wait(5))
                stop_thread.start()
                self.assertFalse(disabled.wait(.3))
            finally:
                finish_spawn.set()
                start_thread.join(5)
                if stop_thread.ident is not None: stop_thread.join(5)
        self.assertFalse(start_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(disabled.is_set())
        self.assertIsNone(state["value"])

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
