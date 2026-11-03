"""Quota resets release only verified stale local gates, never real limits."""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import accounts
import codex_usage
import launcher
import quota_recovery as recovery
import rolling_runtime


def usage(percent=0):
    return {"rate_limit": {"allowed": percent < 100, "limit_reached": percent >= 100,
                          "secondary_window": {"used_percent": percent}}}


class QuotaRecoveryTests(unittest.TestCase):
    def scenario(self, *, available=None, views=None, changed=False, attempts=None, now=0, unknown_person=False,
                 usage_error=None, local_error=None, usage_proxy_port=None):
        record = {"account_id": "fixture-account", "access_token": "PRIVATE_ACCESS", "refresh_token": "PRIVATE_REFRESH"}
        if not unknown_person:
            record["email"] = "fixture@example.invalid"
        row = {"name": "fixture.json", "record": record, "fingerprint": accounts.fingerprint(record)}
        entry = {"name": "fixture.json", "provider": "codex", "disabled": False, "auth_index": "PRIVATE_INDEX",
                 "cooldowns": views if views is not None else [{"scope": "credential", "reason": "credential_quota"}]}
        owner = type("Owner", (), {"state": {"pid": 10, "port": 12345}, "key": "PRIVATE_MANAGEMENT"})()
        requests = []

        def request(url, **options):
            requests.append((url, options))
            if url == recovery.USAGE_URL:
                self.assertNotIn("PRIVATE_REFRESH", json.dumps(options))
                self.assertEqual(options["headers"]["Authorization"], "Bearer PRIVATE_ACCESS")
                if usage_error is not None:
                    raise usage_error
                return usage() if available is None else available
            self.assertEqual(url.split("/v8/")[0], "http://127.0.0.1:12345")
            self.assertEqual(options["headers"]["Authorization"], "Bearer PRIVATE_MANAGEMENT")
            if local_error is not None:
                raise local_error
            return {"files": [entry]} if url.endswith("credentials") else {"status": "ok"}

        def proxy_usage(port, token, account):
            requests.append(("synthetic-proxy-usage", {"port": port}))
            self.assertEqual(token, "PRIVATE_ACCESS")
            self.assertEqual(account, "fixture-account")
            if usage_error is not None:
                raise usage_error
            return usage() if available is None else available

        with tempfile.TemporaryDirectory(prefix="quota-recovery-fixture-") as temporary, \
             patch.object(accounts, "switch_lock", return_value=nullcontext()), \
             patch.object(accounts, "local_accounts", return_value=[row]), \
             patch.object(accounts, "Management", return_value=owner), \
             patch.object(accounts, "request_json", side_effect=request), \
             patch.object(recovery, "_loopback_usage", side_effect=proxy_usage), \
             patch.object(codex_usage, "active_bridge_auth", return_value={**record, "account_id": "changed"} if changed else record):
            if attempts is not None:
                # Exercise the running-monitor path after its startup grace.
                recovery._write_probe_ledger(recovery._probe_ledger(Path(temporary)), {})
            result = recovery.recover_once(Path(temporary), attempts=attempts, now=now,
                                           usage_proxy_port=usage_proxy_port)
        self.assertNotIn("PRIVATE", json.dumps(result))
        return result, requests

    def test_early_reset_releases_exactly_the_selected_origin_gate(self):
        result, requests = self.scenario()
        self.assertEqual(result, {"status": "recovered"})
        posts = [(url, options) for url, options in requests if options.get("method") == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][0], "http://127.0.0.1:12345/v8/management/routing/cooldown/reset")
        self.assertEqual(json.loads(posts[0][1]["data"]), {"auth_index": "PRIVATE_INDEX"})

    def test_known_quota_status_representations_remain_recoverable(self):
        for status in (None, 0, 429):
            with self.subTest(status=status):
                views = [{"scope": "credential", "reason": "credential_quota", "http_status": status}]
                self.assertEqual(self.scenario(views=views)[0]["status"], "recovered")

    def test_exhausted_unknown_and_model_specific_limits_are_preserved(self):
        cases = [usage(100), {}, {"rate_limit": {"allowed": True, "limit_reached": False}},
                 {**usage(), "additional_rate_limits": [{"rate_limit": usage(100)["rate_limit"]}]},
                 {**usage(), "additional_rate_limits": "unknown"},
                 *[{"rate_limit": {**usage()["rate_limit"], "secondary_window": {"used_percent": value}}}
                   for value in (float("nan"), float("inf"), -1, 101, True, 10**400)]]
        for value in cases:
            with self.subTest(value=value):
                result, requests = self.scenario(available=value)
                self.assertEqual(result["status"], "upstream_not_available")
                self.assertFalse(any(options.get("method") == "POST" for _, options in requests))

    def test_auth_failures_and_model_only_throttles_are_never_cleared(self):
        cases = [[{"scope": "model", "reason": "quota", "http_status": 429}],
                 [{"scope": "credential", "reason": "credential_quota"}, {"reason": "unauthorized", "http_status": 401}],
                 [{"scope": "credential", "reason": "credential_quota", "http_status": 403}],
                 *[[{"scope": "credential", "reason": "credential_quota", "http_status": value}]
                   for value in (False, 0.0, 429.0, "429")], []]
        for views in cases:
            result, requests = self.scenario(views=views)
            self.assertEqual(result["status"], "no_stale_credential_quota")
            self.assertFalse(any(url == recovery.USAGE_URL or options.get("method") == "POST" for url, options in requests))

    def test_account_change_during_read_prevents_mutation(self):
        result, requests = self.scenario(changed=True)
        self.assertEqual(result["status"], "account_changed")
        self.assertFalse(any(options.get("method") == "POST" for _, options in requests))

    def test_failed_usage_request_is_categorized_without_reset_or_private_error(self):
        for failure in (OSError("PRIVATE network"), ValueError("PRIVATE parsing"), RuntimeError("PRIVATE response")):
            with self.subTest(kind=type(failure).__name__):
                result, requests = self.scenario(usage_error=failure)
                self.assertEqual(result, {"status": "usage_unavailable"})
                self.assertEqual(sum(url == recovery.USAGE_URL for url, _ in requests), 1)
                self.assertFalse(any(options.get("method") == "POST" for _, options in requests))
        # A local management failure is not misidentified as a usage failure.
        with self.assertRaisesRegex(RuntimeError, "PRIVATE local"):
            self.scenario(local_error=RuntimeError("PRIVATE local"))

    def test_failed_automatic_usage_request_keeps_probe_throttle(self):
        attempts = {}
        result, _ = self.scenario(usage_error=RuntimeError("PRIVATE network"), attempts=attempts, now=0)
        self.assertEqual(result["status"], "usage_unavailable")
        result, requests = self.scenario(attempts=attempts, now=30)
        self.assertEqual(result["status"], "probe_throttled")
        self.assertFalse(any(url == recovery.USAGE_URL for url, _ in requests))

    def test_selected_proxy_is_used_only_for_usage_not_local_management(self):
        result, requests = self.scenario(usage_proxy_port=7890)
        self.assertEqual(result, {"status": "recovered"})
        self.assertEqual(sum(url == "synthetic-proxy-usage" for url, _ in requests), 1)
        self.assertFalse(any(url == recovery.USAGE_URL for url, _ in requests))
        self.assertEqual(sum(options.get("method") == "POST" for _, options in requests), 1)
        result, requests = self.scenario(usage_proxy_port=7890, usage_error=RuntimeError("PRIVATE proxy"))
        self.assertEqual(result, {"status": "usage_unavailable"})
        self.assertFalse(any(options.get("method") == "POST" for _, options in requests))

    def test_proxy_port_is_an_explicit_recover_only_option(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(recovery, "recover_once", return_value={"status": "no_stale_credential_quota"}) as run, \
                 patch("builtins.print"):
                self.assertEqual(recovery.main(["recover", "--usage-proxy-port", "7890"], root=root), 0)
            run.assert_called_once_with(root, usage_proxy_port=7890)
            for args in (["status", "--usage-proxy-port", "7890"], ["recover", "--usage-proxy-port", "0"],
                         ["recover", "--usage-proxy-port", "65536"]):
                with self.subTest(args=args), patch.object(recovery, "recover_once") as run, \
                     patch("sys.stderr"), self.assertRaises(SystemExit):
                    recovery.main(args, root=root)
                run.assert_not_called()
            with patch.object(accounts, "local_accounts") as local, self.assertRaises(ValueError):
                recovery.recover_once(root, attempts={}, usage_proxy_port=7890)
            local.assert_not_called()

    def test_launcher_forwards_the_manual_proxy_option_without_starting_a_service(self):
        argv = ["launcher.py", "quota", "recover", "--usage-proxy-port", "7890"]
        with patch.object(sys, "argv", argv), patch.object(launcher, "helper_output"), \
             patch.object(launcher, "ensure_service") as service, \
             patch.object(recovery, "recover_once", return_value={"status": "no_stale_credential_quota"}) as run, \
             patch("builtins.print"):
            self.assertEqual(launcher.main(), 0)
        run.assert_called_once_with(launcher.ROOT, usage_proxy_port=7890)
        service.assert_not_called()

    def test_unknown_person_never_queries_or_resets_cooldown(self):
        result, requests = self.scenario(unknown_person=True)
        self.assertEqual(result["status"], "account_identity_unknown")
        self.assertEqual(requests, [])

    def test_blocked_probe_frequency_is_bounded_without_sleep(self):
        attempts = {}
        self.scenario(available=usage(100), attempts=attempts, now=0)
        result, requests = self.scenario(attempts=attempts, now=30)
        self.assertEqual(result["status"], "probe_throttled")
        self.assertFalse(any(url == recovery.USAGE_URL for url, _ in requests))
        result, _ = self.scenario(attempts=attempts, now=61)
        self.assertEqual(result["status"], "recovered")

    def test_monitor_is_single_owner_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "monitor"
            with recovery.monitor_lock(directory) as first:
                self.assertTrue(first)
                with recovery.monitor_lock(directory) as second:
                    self.assertFalse(second)
            with recovery.monitor_lock(directory) as third:
                self.assertTrue(third)

    def test_isolated_entrypoint_never_imports_workspace_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "accounts.py").write_text('raise RuntimeError("UNTRUSTED_CWD")')
            result = subprocess.run([sys.executable, "-I", str(Path(recovery.__file__).resolve())],
                                    cwd=temporary, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "")

    def test_monitor_child_does_not_inherit_credentials_or_workspace_proxies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); (root / "quota_recovery.py").write_text("# fixture")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "PRIVATE_KEY", "ANTHROPIC_AUTH_TOKEN": "PRIVATE_TOKEN",
                                         "HTTP_PROXY": "http://untrusted.invalid"}), \
                 patch.object(codex_usage, "active_bridge_auth", return_value={"account_id": "fixture"}), \
                 patch.object(accounts, "Management"), patch.object(subprocess, "Popen") as spawn:
                recovery.ensure_watch(root)
            environment = spawn.call_args.kwargs["env"]
            for key in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HTTP_PROXY"):
                self.assertNotIn(key, environment)
            self.assertNotIn("PRIVATE", json.dumps(spawn.call_args.args[0]))


if __name__ == "__main__": unittest.main()
