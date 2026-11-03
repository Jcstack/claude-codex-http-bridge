"""Durable automatic quota-probe throttling with synthetic accounts only."""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import accounts
import quota_recovery as recovery
import runtime_bundle


IDENTITY_A = "a" * 16
IDENTITY_B = "b" * 16


class QuotaProbeLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="quota-probe-ledger-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_raw(self, content):
        directory = self.root / "runtime-quota"
        runtime_bundle.private_dir(directory)
        path = directory / "probe-ledger.json"
        runtime_bundle.write_file(path, content, 0o600)
        if os.name == "nt":
            import windows_acl
            windows_acl.lock([path])
        return path

    def test_fresh_ledger_survives_new_process_maps_and_is_per_identity(self):
        recovery.initialize_probe_ledger(self.root, 0)
        self.assertFalse(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 0))
        self.assertFalse(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 59))
        self.assertTrue(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 60))
        self.assertTrue(recovery.reserve_automatic_probe(self.root, IDENTITY_B, 61))
        self.assertFalse(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 90))
        self.assertTrue(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 120))
        path = self.root / "runtime-quota" / "probe-ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertNotIn("account_id", data)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_legacy_monitor_without_ledger_gets_one_interval_of_grace(self):
        directory = self.root / "runtime-quota"
        runtime_bundle.private_dir(directory)
        runtime_bundle.write_file(directory / "status.json", b'{"result":"no_stale_credential_quota"}\n', 0o600)
        recovery.initialize_probe_ledger(self.root, 100)
        self.assertFalse(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 159))
        self.assertTrue(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 160))

    def test_missing_report_may_be_a_legacy_monitor_that_exited_during_its_first_probe(self):
        # Both files are absent if the predecessor died before its first report.
        self.assertFalse(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 100))
        self.assertFalse(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 159))
        self.assertTrue(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 160))

    def test_each_watcher_start_gets_global_grace_across_unknown_clock_origins(self):
        path = recovery.initialize_probe_ledger(self.root, 0)
        for prior in (0, 1000):
            with self.subTest(prior=prior):
                recovery._write_probe_ledger(path, {IDENTITY_A: prior})
                recovery.initialize_probe_ledger(self.root, 100)
                self.assertEqual(recovery._read_probe_ledger(path), {"*": 100})
                self.assertFalse(recovery.reserve_automatic_probe(self.root, IDENTITY_B, 159))
                self.assertTrue(recovery.reserve_automatic_probe(self.root, IDENTITY_B, 160))

    def test_watcher_records_legacy_grace_before_replacing_old_status(self):
        directory = self.root / "runtime-quota"
        runtime_bundle.private_dir(directory)
        runtime_bundle.write_file(directory / "status.json", b'{"result":"no_stale_credential_quota"}\n', 0o600)

        def fake_recovery(root, *, attempts):
            self.assertEqual(attempts, {})
            self.assertEqual(recovery._read_probe_ledger(root / "runtime-quota" / "probe-ledger.json"), {"*": 100})
            return {"status": "no_stale_credential_quota"}

        with patch.object(recovery.launcher, "read_state", side_effect=[{"fixture": True}, None]), \
             patch.object(recovery.launcher, "state_is_live", return_value=True), \
             patch.object(accounts, "switch_lock", return_value=nullcontext()), \
             patch.object(recovery, "recover_once", side_effect=fake_recovery) as recover, \
             patch.object(recovery.time, "monotonic", return_value=100):
            self.assertEqual(recovery.watch(self.root, wait=lambda _seconds: None), 0)
        recover.assert_called_once()
        status = json.loads((directory / "status.json").read_text())
        self.assertEqual(status["status"], "no_stale_credential_quota")
        self.assertEqual(status["monitor_protocol"], 2)

    def test_watcher_with_invalid_ledger_never_attempts_recovery(self):
        path = self.write_raw(b"invalid synthetic ledger")
        with patch.object(recovery.launcher, "read_state", side_effect=[{"fixture": True}, None]), \
             patch.object(recovery.launcher, "state_is_live", return_value=True), \
             patch.object(accounts, "switch_lock", return_value=nullcontext()), \
             patch.object(recovery, "recover_once") as recover:
            self.assertEqual(recovery.watch(self.root, wait=lambda _seconds: None), 0)
        recover.assert_not_called()
        self.assertEqual(path.read_bytes(), b"invalid synthetic ledger")
        self.assertEqual(json.loads((path.parent / "status.json").read_text())["status"], "probe_state_unavailable")

    def test_real_watcher_reconciles_a_synthetic_gate_after_startup_grace(self):
        record = {"account_id": "fixture", "email": "fixture@example.invalid", "access_token": "synthetic"}
        selected = {"name": "fixture.json", "record": record, "fingerprint": accounts.fingerprint(record)}
        row = {"name": "fixture.json", "provider": "codex", "auth_index": "synthetic-index",
               "cooldowns": [{"scope": "credential", "reason": "credential_quota"}]}
        owner = type("Owner", (), {"state": {"pid": 10, "port": 12345}, "key": "synthetic-management"})()
        clock = {"monotonic": 0}
        observed_statuses, calls = [], []

        def request(url, **_options):
            if url.endswith("/credentials"):
                return {"files": [row]}
            if url == recovery.USAGE_URL:
                calls.append("usage")
                return {"rate_limit": {"allowed": True, "limit_reached": False,
                                       "primary_window": {"used_percent": 1}}}
            if url.endswith("/routing/cooldown/reset"):
                calls.append("reset")
                return {"status": "ok"}
            raise AssertionError("Unexpected synthetic endpoint")

        def wait(seconds):
            path = self.root / "runtime-quota" / "status.json"
            observed_statuses.append(json.loads(path.read_text())["status"])
            clock["monotonic"] += seconds

        fake_time = SimpleNamespace(monotonic=lambda: clock["monotonic"], time=lambda: 1000 + clock["monotonic"])
        with patch.object(recovery.launcher, "read_state", side_effect=[{"fixture": True}] * 3 + [None]), \
             patch.object(recovery.launcher, "state_is_live", return_value=True), \
             patch.object(recovery, "time", fake_time), \
             patch.object(accounts, "switch_lock", return_value=nullcontext()), \
             patch.object(accounts, "local_accounts", return_value=[selected]), \
             patch.object(accounts, "Management", return_value=owner), \
             patch.object(accounts, "request_json", side_effect=request), \
             patch.object(recovery.codex_usage, "active_bridge_auth", return_value=record):
            self.assertEqual(recovery.watch(self.root, wait=wait), 0)
        self.assertEqual(observed_statuses, ["probe_throttled", "probe_throttled", "recovered"])
        self.assertEqual(calls, ["usage", "reset"])

    def test_future_monotonic_clock_is_clamped_and_cannot_block_forever(self):
        path = recovery.initialize_probe_ledger(self.root, 0)
        recovery._write_probe_ledger(path, {IDENTITY_A: 1000})
        self.assertFalse(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 0))
        self.assertEqual(recovery._read_probe_ledger(path)[IDENTITY_A], 0)
        self.assertFalse(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 59))
        self.assertTrue(recovery.reserve_automatic_probe(self.root, IDENTITY_A, 60))

    def test_invalid_clock_values_do_not_create_probe_state(self):
        for value in (True, -1, float("inf"), float("nan"), 10**400):
            with self.subTest(value=str(value)[:16]), self.assertRaises(ValueError):
                recovery.initialize_probe_ledger(self.root, value)
        self.assertFalse((self.root / "runtime-quota" / "probe-ledger.json").exists())

    def test_malformed_duplicate_and_oversized_state_fail_closed(self):
        cases = [b'not json', b'{"version":1,"version":1,"entries":{}}',
                 b'{"version":1,"entries":{"bad":0}}',
                 b'{"version":1,"entries":{"aaaaaaaaaaaaaaaa":1e999}}',
                 b'x' * (recovery.PROBE_LEDGER_BYTES + 1)]
        for content in cases:
            with self.subTest(content=content[:40]):
                self.write_raw(content)
                with self.assertRaises((ValueError, RuntimeError)):
                    recovery.reserve_automatic_probe(self.root, IDENTITY_A, 10)

    def test_state_growth_after_metadata_check_remains_bounded(self):
        path = self.write_raw(b'{"version":1,"entries":{}}')
        real_safe_file = runtime_bundle.safe_file

        def grow_after_check(candidate, **kwargs):
            real_safe_file(candidate, **kwargs)
            with candidate.open("ab") as handle:
                handle.write(b"x" * (recovery.PROBE_LEDGER_BYTES + 1))

        with patch.object(runtime_bundle, "safe_file", side_effect=grow_after_check):
            with self.assertRaises(ValueError):
                recovery._read_probe_ledger(path)

    @unittest.skipIf(os.name == "nt", "Creating symlinks requires Windows privileges")
    def test_symlinked_state_is_refused(self):
        directory = self.root / "runtime-quota"
        runtime_bundle.private_dir(directory)
        outside = self.root / "elsewhere"
        outside.write_text("{}", encoding="utf-8")
        (directory / "probe-ledger.json").symlink_to(outside)
        with self.assertRaises(RuntimeError):
            recovery.reserve_automatic_probe(self.root, IDENTITY_A, 10)
        self.assertEqual(outside.read_text(encoding="utf-8"), "{}")

    def test_full_live_ledger_fails_closed(self):
        path = recovery.initialize_probe_ledger(self.root, 0)
        entries = {f"{index:016x}": 1 for index in range(recovery.PROBE_LEDGER_LIMIT)}
        recovery._write_probe_ledger(path, entries)
        with self.assertRaises(RuntimeError):
            recovery.reserve_automatic_probe(self.root, IDENTITY_A, 2)

    def test_independent_processes_serialize_the_same_reservation(self):
        # The production caller holds this same cross-process account lock.
        with accounts.switch_lock(self.root):
            recovery.initialize_probe_ledger(self.root, 100)
        source = Path(recovery.__file__).resolve().parent
        program = ("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                   "import accounts,quota_recovery; root=Path(sys.argv[2]); "
                   "exec('with accounts.switch_lock(root, wait_seconds=20):\\n "
                   "print(\\\"yes\\\" if quota_recovery.reserve_automatic_probe(root, \\\"aaaaaaaaaaaaaaaa\\\", 160) else \\\"no\\\")')")
        allowed = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG")
        environment = {name: os.environ[name] for name in allowed if name in os.environ}
        environment.update(HOME=str(self.root), USERPROFILE=str(self.root))
        children = [subprocess.Popen([sys.executable, "-I", "-c", program, str(source), str(self.root)],
                                     env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outputs = []
        try:
            for child in children:
                out, err = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, "Synthetic reservation process failed")
                self.assertEqual(err, "")
                outputs.append(out.strip())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=10)
        self.assertCountEqual(outputs, ["yes", "no"])

    def test_crash_during_fake_usage_request_does_not_erase_reservation(self):
        # Start after the monitor's initial quiet period in this fixture.
        recovery._write_probe_ledger(recovery._probe_ledger(self.root), {})
        source = Path(recovery.__file__).resolve().parent
        program = r'''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import accounts,quota_recovery
root=Path(sys.argv[2])
record={"account_id":"fixture","email":"fixture@example.invalid","access_token":"synthetic"}
selected={"name":"fixture.json","record":record,"fingerprint":accounts.fingerprint(record)}
row={"name":"fixture.json","provider":"codex","auth_index":"synthetic-index",
     "cooldowns":[{"scope":"credential","reason":"credential_quota"}]}
owner=type("Owner",(),{"state":{"pid":10,"port":12345},"key":"synthetic-management"})()
accounts.local_accounts=lambda _root:[selected]
accounts.Management=lambda _root:owner
def request(url,**_options):
    if url.endswith("/credentials"):
        return {"files":[row]}
    if url==quota_recovery.USAGE_URL:
        os._exit(42)
    raise AssertionError("Unexpected synthetic endpoint")
accounts.request_json=request
quota_recovery.recover_once(root,attempts={},now=0)
raise AssertionError("Synthetic crash did not occur")
'''
        allowed = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG")
        environment = {name: os.environ[name] for name in allowed if name in os.environ}
        environment.update(HOME=str(self.root), USERPROFILE=str(self.root))
        result = subprocess.run([sys.executable, "-I", "-c", program, str(source), str(self.root)],
                                env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 42, "Synthetic crash fixture failed")
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        identity = accounts.fingerprint({"account_id": "fixture", "email": "fixture@example.invalid"})
        with accounts.switch_lock(self.root, wait_seconds=10):
            self.assertFalse(recovery.reserve_automatic_probe(self.root, identity, 30))
            self.assertTrue(recovery.reserve_automatic_probe(self.root, identity, 60))

    def test_failed_network_attempt_is_saved_before_retry_with_a_fresh_map(self):
        recovery._write_probe_ledger(recovery._probe_ledger(self.root), {})
        record = {"account_id": "fixture", "email": "fixture@example.invalid", "access_token": "synthetic"}
        identity = accounts.fingerprint(record)
        selected = {"name": "fixture.json", "record": record, "fingerprint": identity}
        row = {"name": "fixture.json", "provider": "codex", "auth_index": "synthetic-index",
               "cooldowns": [{"scope": "credential", "reason": "credential_quota"}]}
        owner = type("Owner", (), {"state": {"pid": 10, "port": 12345}, "key": "synthetic-management"})()
        calls = []

        def request(url, **_options):
            if url.endswith("/credentials"):
                return {"files": [row]}
            self.assertEqual(url, recovery.USAGE_URL)
            calls.append("usage")
            ledger = recovery._read_probe_ledger(self.root / "runtime-quota" / "probe-ledger.json")
            self.assertIn(identity, ledger)
            raise OSError("synthetic network failure")

        with patch.object(accounts, "switch_lock", return_value=nullcontext()), \
             patch.object(accounts, "local_accounts", return_value=[selected]), \
             patch.object(accounts, "Management", return_value=owner), \
             patch.object(accounts, "request_json", side_effect=request):
            with self.assertRaises(OSError):
                recovery.recover_once(self.root, attempts={}, now=0)
            self.assertEqual(recovery.recover_once(self.root, attempts={}, now=30), {"status": "probe_throttled"})
            self.assertEqual(calls, ["usage"])
            same_process_attempts = {}
            with self.assertRaises(OSError):
                recovery.recover_once(self.root, attempts=same_process_attempts, now=60)
            self.assertEqual(calls, ["usage", "usage"])
            self.assertEqual(recovery.recover_once(self.root, attempts=same_process_attempts, now=1),
                             {"status": "probe_throttled"})
            self.assertEqual(recovery._read_probe_ledger(self.root / "runtime-quota" / "probe-ledger.json")[identity], 1)
            self.assertEqual(recovery.recover_once(self.root, attempts={}, now=60), {"status": "probe_throttled"})
            with self.assertRaises(OSError):
                recovery.recover_once(self.root, attempts={}, now=61)

    def test_invalid_probe_state_prevents_external_request_and_reset(self):
        self.write_raw(b'{"version":1,"entries":{"fixture":0}}')
        record = {"account_id": "fixture", "email": "fixture@example.invalid", "access_token": "synthetic"}
        selected = {"name": "fixture.json", "record": record, "fingerprint": accounts.fingerprint(record)}
        row = {"name": "fixture.json", "provider": "codex", "auth_index": "synthetic-index",
               "cooldowns": [{"scope": "credential", "reason": "credential_quota"}]}
        owner = type("Owner", (), {"state": {"pid": 10, "port": 12345}, "key": "synthetic-management"})()

        def request(url, **_options):
            self.assertTrue(url.endswith("/credentials"))
            return {"files": [row]}

        with patch.object(accounts, "switch_lock", return_value=nullcontext()), \
             patch.object(accounts, "local_accounts", return_value=[selected]), \
             patch.object(accounts, "Management", return_value=owner), \
             patch.object(accounts, "request_json", side_effect=request) as network:
            with self.assertRaises(recovery.ProbeLedgerError):
                recovery.recover_once(self.root, attempts={}, now=10)
        network.assert_called_once()

    def test_status_is_read_only_and_drops_private_metadata(self):
        directory = self.root / "runtime-quota"
        runtime_bundle.private_dir(directory)
        status = directory / "status.json"
        runtime_bundle.write_file(status, json.dumps({"status": "probe_throttled", "checked_at": 123,
            "monitor_pid": 91234, "account_id": "SYNTHETIC_PRIVATE_ID", "error": "SYNTHETIC_PRIVATE_ERROR"}).encode(), 0o600)
        recovery.initialize_probe_ledger(self.root, 100)
        before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
        result = recovery.monitor_status(self.root)
        self.assertEqual(result, {"last_status": "probe_throttled", "checked_at": 123,
                                  "monitor_protocol": None, "liveness": "unverified", "probe_ledger": "valid"})
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()})
        with patch("builtins.print") as display:
            self.assertEqual(recovery.main(["status"], root=self.root), 0)
        self.assertEqual(json.loads(display.call_args.args[0]), result)

    def test_status_with_no_monitor_creates_nothing(self):
        self.assertEqual(recovery.monitor_status(self.root), {"last_status": None, "checked_at": None,
            "monitor_protocol": None, "liveness": "unverified", "probe_ledger": "absent"})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_status_refuses_a_non_directory_runtime(self):
        (self.root / "runtime-quota").write_text("SYNTHETIC_PRIVATE", encoding="utf-8")
        result = recovery.monitor_status(self.root)
        self.assertEqual(result["probe_ledger"], "unavailable")
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))

    def test_status_reports_invalid_state_without_returning_raw_contents(self):
        self.write_raw(b'{"SYNTHETIC_PRIVATE":"do not reveal"}')
        result = recovery.monitor_status(self.root)
        self.assertEqual(result["probe_ledger"], "unavailable")
        self.assertIsNone(result["last_status"])
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))

    def test_status_reports_protocol_only_for_a_valid_last_report(self):
        directory = self.root / "runtime-quota"
        runtime_bundle.private_dir(directory)
        path = directory / "status.json"
        for version, expected in ((2, 2), (True, None), (999, None)):
            runtime_bundle.write_file(path, json.dumps({"status": "check_unavailable", "checked_at": 123,
                                                       "monitor_protocol": version}).encode(), 0o600)
            self.assertEqual(recovery.monitor_status(self.root)["monitor_protocol"], expected)


if __name__ == "__main__":
    unittest.main()
