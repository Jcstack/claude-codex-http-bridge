"""Account changes use private OAuth staging and do not restart the user's service."""

import base64
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse
import urllib.request

import accounts
import codex_usage


def tokens(account="synthetic-account-b", user=None):
    claims = {"email": "fixture@example.invalid", "https://api.openai.com/auth": {
        "chatgpt_account_id": account, "chatgpt_plan_type": "pro"}}
    if user:
        claims["https://api.openai.com/auth"]["chatgpt_user_id"] = user
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return {"id_token": "fixture." + payload + ".fixture", "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh", "expires_in": 3600}


class AccountTests(unittest.TestCase):
    def test_oauth_is_loopback_only_staged_and_redacted(self):
        callback_errors = []
        exchanges = []
        def open_browser(url, new):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertTrue(url.startswith(accounts.AUTHORIZE_URL + "?"))
            self.assertEqual(query["prompt"], ["login"])
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertEqual(query["redirect_uri"], [accounts.REDIRECT_URI])
            def callback():
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    bad = "http://127.0.0.1:1455/auth/callback?state=wrong&code=ignored"
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        opener.open(bad, timeout=3)
                    caught.exception.close()
                    good = "http://127.0.0.1:1455/auth/callback?" + urllib.parse.urlencode({
                        "state": query["state"][0], "code": "synthetic-authorization-code"})
                    with opener.open(good, timeout=3) as response:
                        self.assertEqual(response.status, 200)
                        self.assertNotIn(b"synthetic-authorization-code", response.read())
                except BaseException as error:
                    callback_errors.append(error)
            threading.Thread(target=callback, daemon=True).start()
            return True
        def exchange(url, **kwargs):
            self.assertEqual(url, accounts.TOKEN_URL)
            body = urllib.parse.parse_qs(kwargs["data"].decode())
            self.assertEqual(body["code"], ["synthetic-authorization-code"])
            self.assertGreaterEqual(len(body["code_verifier"][0]), 43)
            exchanges.append(url)
            return tokens()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = root / "native-auth.json"
            native.write_text("untouched")
            output = StringIO()
            with patch.object(accounts.webbrowser, "open", side_effect=open_browser), \
                    patch.object(accounts, "request_json", side_effect=exchange), redirect_stdout(output):
                destination = accounts.oauth_login(root, "secondary", timeout=5)
            self.assertFalse(callback_errors, callback_errors)
            self.assertEqual(len(exchanges), 1)
            self.assertTrue(accounts.private_json(destination)["disabled"])
            self.assertFalse((root / "auth").exists())
            self.assertEqual(native.read_text(), "untouched")
            for private in ("synthetic-access", "synthetic-refresh", "fixture@example.invalid",
                            "synthetic-authorization-code", "synthetic-account-b"):
                self.assertNotIn(private, output.getvalue())

    def test_alias_traversal_and_symlink_are_denied(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for alias in ("../auth/current", "/tmp/account", "A", "a\\b", "a" * 33):
                with self.assertRaises(ValueError):
                    accounts.stage_path(root, alias)
            try:
                (root / "accounts").symlink_to(root, target_is_directory=True)
            except OSError:
                if os.name == "nt":
                    self.skipTest("Windows runner does not permit symlink creation")
                raise
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                accounts.stage_path(root, "secondary")

    def test_existing_alias_and_occupied_callback_never_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = accounts.stage_path(root, "secondary")
            path.write_text("keep")
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                accounts.oauth_login(root, "secondary")
            self.assertEqual(path.read_text(), "keep")
            with socket.socket() as blocker:
                blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                blocker.bind(("127.0.0.1", 1455))
                blocker.listen()
                with self.assertRaisesRegex(RuntimeError, "occupied"):
                    accounts.oauth_login(root, "another")

    def test_incomplete_tokens_fail_closed(self):
        for changes in ({"refresh_token": ""}, {"id_token": "bad"}, {"expires_in": 0}):
            with self.assertRaises(RuntimeError):
                accounts.token_record({**tokens(), **changes})

    def test_bootstrap_uses_loopback_flow_and_never_replaces_existing_account(self):
        for has_old in (False, True):
            with self.subTest(has_old=has_old), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                accounts.private_directory(root / "auth")
                if has_old:
                    accounts.api_profile.write_private(root / "auth/old.json", json.dumps({
                        **accounts.token_record(tokens("synthetic-old")), "disabled": False}))
                destination = accounts.stage_path(root, "primary")
                accounts.api_profile.write_private(destination, json.dumps(accounts.token_record(tokens())))
                original = list((root / "auth").glob("*.json"))
                with patch.object(accounts, "oauth_login", return_value=destination) as login, redirect_stdout(StringIO()):
                    accounts.initial_login(root)
                login.assert_called_once()
                entries = accounts.local_accounts(root)
                self.assertEqual(len(entries), 1)
                if has_old:
                    self.assertEqual([row["name"] for row in entries], [p.name for p in original])
                    self.assertEqual(entries[0]["record"]["account_id"], "synthetic-old")
                else:
                    self.assertFalse(entries[0]["record"]["disabled"])

    def test_switch_retains_old_auth_and_rolls_back_without_restart(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                accounts.private_directory(root / "auth")
                old = accounts.token_record(tokens("synthetic-account-a"))
                old["disabled"] = False
                accounts.api_profile.write_private(root / "auth/old.json", json.dumps(old))
                staged = accounts.stage_path(root, "secondary")
                new = accounts.token_record(tokens())
                accounts.api_profile.write_private(staged, json.dumps(new))
                accounts.api_profile.write_private(staged.with_suffix(".checked"), json.dumps({
                    "fingerprint": accounts.fingerprint(new), "checked_at": accounts.time.time()}))
                changes = []
                class Manager:
                    def files(self):
                        return [{"name": row["name"], "disabled": row["record"]["disabled"]}
                                for row in accounts.local_accounts(root)]
                    def call(self, method, path, body=None):
                        if method == "POST":
                            name = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["name"][0]
                            accounts.api_profile.write_private(root / "auth" / name, json.dumps(body))
                            return {"status": "ok"}
                        return {"models": [{"id": "synthetic-model"}]}
                    def set_disabled(self, name, disabled):
                        changes.append((name, disabled))
                        if fail and name == "old.json" and disabled:
                            raise RuntimeError("synthetic commit failure")
                        path = root / "auth" / name
                        record = accounts.private_json(path)
                        accounts.api_profile.write_private(path, json.dumps({**record, "disabled": disabled}))
                output = StringIO()
                with patch.object(accounts, "Management", return_value=Manager()), \
                        patch.object(accounts, "check_account") as check, redirect_stdout(output):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "restored"):
                            accounts.switch_account(root, "secondary")
                    else:
                        accounts.switch_account(root, "secondary")
                    check.assert_not_called()
                target = "codex-bridge-" + accounts.fingerprint(new) + ".json"
                self.assertEqual(changes[0], (target, False))
                self.assertTrue((root / "auth/old.json").exists())
                self.assertEqual(accounts.private_json(root / "auth/old.json")["disabled"], not fail)
                self.assertEqual(accounts.private_json(root / "auth" / target)["disabled"], fail)
                self.assertFalse((root / "accounts/switch.pending").exists())
                self.assertNotIn("synthetic-access", output.getvalue())
                self.assertNotIn("fixture@example.invalid", output.getvalue())

    def test_quota_follows_only_enabled_account_and_rejects_stale_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            old = {**accounts.token_record(tokens("synthetic-old")), "disabled": True}
            new = {**accounts.token_record(tokens()), "disabled": False}
            accounts.api_profile.write_private(root / "auth/old.json", json.dumps(old))
            accounts.api_profile.write_private(root / "auth/new.json", json.dumps(new))
            cache = root / "usage.json"
            accounts.api_profile.write_private(cache, json.dumps({"fetched_at": accounts.time.time(),
                "account_fingerprint": codex_usage.account_fingerprint(old["account_id"]),
                "windows": {"five_hour": {"used_percentage": 99}}}))
            self.assertEqual(codex_usage.selected_account_id(root), new["account_id"])
            self.assertIsNone(codex_usage.read_cache(cache, root))
            old["disabled"] = False
            accounts.api_profile.write_private(root / "auth/old.json", json.dumps(old))
            self.assertIsNone(codex_usage.selected_account_id(root))

    def test_new_account_quota_uses_fixed_official_host_and_no_ambient_proxy(self):
        from io import BytesIO
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            record = {**accounts.token_record(tokens()), "disabled": False}
            accounts.api_profile.write_private(root / "auth/new.json", json.dumps(record))
            body = {"rate_limit": {"primary_window": {"limit_window_seconds": 18000,
                "used_percent": 12.5, "reset_at": 1793774315}, "secondary_window": None}}
            response = BytesIO(json.dumps(body).encode())
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value = response
            with patch.object(codex_usage.urllib.request, "build_opener", return_value=opener) as build:
                result = codex_usage._bridge_rate_limits(root)
            handlers = build.call_args.args
            self.assertEqual(handlers[0].proxies, {})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/wham/usage")
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-access")
            windows = codex_usage.windows_from_response(result)
            self.assertEqual(windows["five_hour"]["used_percentage"], 12.5)
            self.assertNotIn("seven_day", windows)

    def test_shared_workspace_does_not_merge_people_or_native_quota(self):
        first = accounts.token_record(tokens("shared-workspace", "person-a"))
        second = {**accounts.token_record(tokens("shared-workspace", "person-b")), "disabled": False}
        self.assertNotEqual(accounts.fingerprint(first), accounts.fingerprint(second))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            accounts.api_profile.write_private(root / "auth/new.json", json.dumps(second))
            read_json = codex_usage._private_json
            native = {"tokens": {"account_id": first["account_id"], "id_token": first["id_token"]}}
            with patch.object(codex_usage, "_private_json", side_effect=lambda p: native if p.name == "auth.json" else read_json(p)):
                self.assertIsNone(codex_usage.matching_account_id(root))
            cache = root / "usage.json"
            accounts.api_profile.write_private(cache, json.dumps({"fetched_at": accounts.time.time(),
                "account_fingerprint": accounts.identity_fingerprint(first), "windows": {"five_hour": {}}}))
            self.assertIsNone(codex_usage.read_cache(cache, root))

    def test_selected_account_validation_reuses_existing_refresh_owner(self):
        import launcher
        from types import SimpleNamespace
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            record = {**accounts.token_record(tokens()), "disabled": False}
            accounts.api_profile.write_private(root / "auth/new.json", json.dumps(record))
            session = root / "runtime/session-fixture"
            session.mkdir(parents=True)
            (session / "key").write_text("synthetic-loopback-key")
            state = {"pid": 1234, "port": 12345, "session": "session-fixture"}
            with patch.object(accounts, "Management", return_value=SimpleNamespace(state=state)), \
                    patch.object(accounts, "isolated_account") as isolated, \
                    patch.object(accounts.subprocess, "run", return_value=CompletedProcess([], 0, "{}")):
                with accounts.validation_route(root, record) as route:
                    self.assertEqual(route["url"], "http://127.0.0.1:12345")
                    self.assertEqual(route["key"], "synthetic-loopback-key")
                isolated.assert_not_called()


if __name__ == "__main__":
    unittest.main()
