"""Account changes use private OAuth staging and do not restart the user's service."""

import base64
from contextlib import contextmanager
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
    def test_isolated_validator_does_not_inherit_storage_or_shell_credentials(self):
        from types import SimpleNamespace
        import launcher
        import windows_acl
        sentinel = "SYNTHETIC_PRIVATE_FIXTURE"
        environment = {"GITSTORE_GIT_URL": sentinel, "PGSTORE_DSN": sentinel,
                       "OBJECTSTORE_ENDPOINT": sentinel, "OPENAI_API_KEY": sentinel,
                       "https_proxy": "http://127.0.0.1:8001"}
        profiles = {name: sentinel for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")}
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {**environment, **profiles}), \
             patch.object(windows_acl, "lock"), patch.object(windows_acl, "verify"), \
             patch.object(accounts.subprocess, "run", return_value=SimpleNamespace(stdout="{}")) as helper, \
             patch.object(accounts.subprocess, "Popen") as spawn, \
             patch.object(launcher, "model_helper_command", side_effect=AssertionError("old launcher"), create=True), \
             patch.object(launcher, "model_helper_environment", side_effect=AssertionError("old launcher"), create=True), \
             patch.object(launcher, "wait_for_private_listener"), patch.object(launcher, "wait_for_catalog"), \
             patch.object(launcher, "stop_process"):
            record = {"type": "codex", "access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}
            with accounts.isolated_account(Path(temporary), record):
                child = spawn.call_args.kwargs["env"]
                for name in environment: self.assertNotIn(name, child)
                home = spawn.call_args.kwargs["cwd"]
                self.assertEqual(child["HOME"], str(home))
                if os.name == "nt":
                    self.assertEqual(child["USERPROFILE"], str(home))
                    self.assertEqual(child["APPDATA"], str(home / "AppData" / "Roaming"))
                    self.assertEqual(child["LOCALAPPDATA"], str(home / "AppData" / "Local"))
                else:
                    for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                        self.assertNotIn(name, child)
                self.assertEqual(helper.call_count, 2)
                for call in helper.call_args_list:
                    self.assertEqual(call.args[0][1:3], ["-E", "-S"])
                    for name in environment: self.assertNotIn(name, call.kwargs["env"])

    def test_disposable_validator_child_sees_its_temporary_home(self):
        from types import SimpleNamespace
        import launcher
        import rolling_runtime
        import windows_acl
        child_script = '''import json,os\nfrom pathlib import Path\nhome=Path(os.environ["HOME"])\ncwd=Path.cwd()\nprofile_ok=(os.name!="nt" or (Path(os.environ["USERPROFILE"])==home and Path(os.environ["APPDATA"]).is_relative_to(home) and Path(os.environ["LOCALAPPDATA"]).is_relative_to(home)))\nresult={"home_matches_cwd":home.resolve()==cwd.resolve(),"user_home_marker_visible":(home/"user-home-marker").exists(),"credential_inherited":"OPENAI_API_KEY" in os.environ,"proxy_inherited":"HTTPS_PROXY" in os.environ,"ca_setting_preserved":os.environ.get("SSL_CERT_FILE")=="synthetic-ca-path","profile_ok":profile_ok}\n(cwd/"child-observation.json").write_text(json.dumps(result),encoding="utf-8")\n'''
        real_popen = subprocess.Popen
        child_roots = []

        def spawn_fixture(_command, **kwargs):
            child_roots.append(kwargs["cwd"])
            child = real_popen([sys.executable, "-I", "-c", child_script], **kwargs)
            self.assertEqual(child.wait(timeout=10), 0)
            return child

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user_home = root / "user-home"
            user_home.mkdir()
            (user_home / "user-home-marker").write_text("synthetic", encoding="utf-8")
            environment = {"HOME": str(user_home), "USERPROFILE": str(user_home),
                           "APPDATA": str(user_home), "LOCALAPPDATA": str(user_home),
                           "OPENAI_API_KEY": "SYNTHETIC_PRIVATE_FIXTURE",
                           "HTTPS_PROXY": "http://proxy.invalid:1234", "SSL_CERT_FILE": "synthetic-ca-path"}
            with patch.dict(os.environ, environment):
                control = subprocess.run([sys.executable, "-I", "-c",
                                          'import os,pathlib; print((pathlib.Path(os.environ["HOME"])/"user-home-marker").exists())'],
                                         env=rolling_runtime.process_environment(), capture_output=True,
                                         text=True, timeout=10, check=True)
            self.assertEqual(control.stdout.strip(), "True")
            with patch.dict(os.environ, environment), patch.object(windows_acl, "lock"), \
                 patch.object(windows_acl, "verify"), \
                 patch.object(accounts.subprocess, "run", return_value=SimpleNamespace(stdout="{}")), \
                 patch.object(accounts.subprocess, "Popen", side_effect=spawn_fixture), \
                 patch.object(launcher, "wait_for_private_listener"), patch.object(launcher, "wait_for_catalog"), \
                 patch.object(launcher, "stop_process"):
                record = {"type": "codex", "access_token": "synthetic-access"}
                with accounts.isolated_account(root, record):
                    observation = json.loads((child_roots[0] / "child-observation.json").read_text())
                    self.assertEqual(observation, {"home_matches_cwd": True, "user_home_marker_visible": False,
                                                  "credential_inherited": False, "proxy_inherited": False,
                                                  "ca_setting_preserved": True, "profile_ok": True})

    def test_temporary_home_validator_can_use_real_model_helper_against_fake_proxy(self):
        from types import SimpleNamespace
        import launcher
        import windows_acl
        fake_proxy = '''from pathlib import Path\ndef mark(value): (Path.cwd()/"startup-stage").write_text(value,encoding="utf-8")\nmark("entered")\nimport json,os,re,sys\nmark("stdlib")\nfrom http.server import BaseHTTPRequestHandler,HTTPServer\nimport socket,socketserver\ndef reject_reverse_lookup(*args): raise AssertionError("synthetic fixture must not perform reverse DNS")\nsocket.getfqdn=reject_reverse_lookup\nmark("http-imported")\nconfig=Path(sys.argv[-1]).read_text(encoding="utf-8")\nport=int(re.search(r"(?m)^  port: (\\d+)$",config).group(1))\nmark("config-read")\nhome=Path(os.environ["HOME"])\n(Path.cwd()/"home-check").write_text(str(home.resolve()==Path.cwd().resolve()),encoding="utf-8")\nrow={"slug":"gpt-9.2-sol","context_window":272000,"max_context_window":872000,"input_modalities":["text"],"supports_search_tool":True,"supported_reasoning_levels":[{"effort":v} for v in ("low","medium","high","xhigh","max")]}\nclass Handler(BaseHTTPRequestHandler):\n def log_message(self,*args): pass\n def do_GET(self):\n  data={"data":[{"id":row["slug"]}]} if self.path=="/v1/models" else {"models":[row]}\n  body=json.dumps(data).encode(); self.send_response(200); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)\nclass FixtureHTTPServer(HTTPServer):\n def server_bind(self):\n  socketserver.TCPServer.server_bind(self); self.server_name="127.0.0.1"; self.server_port=self.server_address[1]\nmark("before-bind")\nserver=FixtureHTTPServer(("127.0.0.1",port),Handler)\nmark("bound")\nserver.serve_forever()\n'''
        real_run, real_popen = subprocess.run, subprocess.Popen
        with tempfile.TemporaryDirectory(prefix="validator-helper-") as temporary:
            root = Path(temporary) / "模型-fixture"
            root.mkdir()
            source = Path(accounts.__file__).resolve().parent
            for name in ("bridge_models.py", "windows_acl.py"):
                shutil.copyfile(source / name, root / name)
            fake_binary = root / "bin" / ("cli-proxy-api.exe" if os.name == "nt" else "cli-proxy-api")
            child_roots = []

            def launch(command, **kwargs):
                if Path(command[0]) == fake_binary:
                    child_roots.append(kwargs["cwd"])
                    return real_popen([sys.executable, "-I", "-c", fake_proxy, *command[1:]], **kwargs)
                return real_popen(command, **kwargs)

            def run(command, **kwargs):
                if command[-1] == "verify":
                    return SimpleNamespace(stdout="", returncode=0)
                return real_run(command, **kwargs)

            def wait_for_fake_listener(process, port):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Synthetic validation proxy exited before readiness")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.2):
                            return
                    except OSError:
                        time.sleep(.1)
                stage_file = child_roots[0] / "startup-stage"
                stage = stage_file.read_text(encoding="utf-8") if stage_file.is_file() else "not-entered"
                if stage not in ("not-entered", "entered", "stdlib", "http-imported", "config-read", "before-bind", "bound"):
                    stage = "unknown"
                raise RuntimeError("Synthetic validation proxy did not become ready; stage=" + stage)

            with patch.object(windows_acl, "lock"), patch.object(windows_acl, "verify"), \
                 patch.object(accounts.subprocess, "Popen", side_effect=launch), \
                 patch.object(accounts.subprocess, "run", side_effect=run), \
                 patch.object(launcher, "HELPER", root / "bridge_models.py"), \
                 patch.object(launcher, "wait_for_private_listener", side_effect=wait_for_fake_listener):
                with accounts.isolated_account(root, {"type": "codex", "access_token": "synthetic-access"}) as probe:
                    self.assertEqual(probe["models"]["gpt-9.2-sol"]["context_window"], 872000)
                    self.assertEqual((child_roots[0] / "home-check").read_text(), "True")

    def test_installed_inactive_validation_cannot_become_a_second_refresh_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            inactive = {**accounts.token_record(tokens("synthetic-inactive")),
                        "siwc": {"refresh_token": "synthetic-nested-refresh"}}
            active = {**accounts.token_record(tokens("synthetic-active")), "disabled": False}
            for name, row in (("inactive", inactive), ("active", active)):
                accounts.api_profile.write_private(root / "auth" / (name + ".json"), json.dumps(row))
            original = json.dumps(inactive, sort_keys=True)

            @contextmanager
            def isolated(_root, candidate):
                self.assertIsNot(candidate, inactive)
                self.assertNotIn("refresh_token", candidate)
                self.assertNotIn("siwc", candidate)
                self.assertEqual(candidate["access_token"], inactive["access_token"])
                candidate["access_token"] = "synthetic-validator-change"
                yield {"models": {}}

            with patch.object(accounts, "isolated_account", side_effect=isolated), \
                    patch.object(accounts, "Management", side_effect=AssertionError("selection must not change")):
                with accounts.validation_route(root, inactive):
                    pass
            self.assertEqual(json.dumps(inactive, sort_keys=True), original)
            self.assertEqual(json.dumps(accounts.private_json(root / "auth/inactive.json"), sort_keys=True), original)

    def test_new_staged_grant_can_refresh_without_an_installed_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            staged = accounts.token_record(tokens())

            @contextmanager
            def isolated(_root, candidate):
                self.assertIs(candidate, staged)
                self.assertEqual(candidate["refresh_token"], "synthetic-refresh")
                yield {"models": {}}
                candidate["refresh_token"] = "synthetic-new-generation"

            with patch.object(accounts, "isolated_account", side_effect=isolated):
                with accounts.validation_route(root, staged):
                    pass
            self.assertEqual(staged["refresh_token"], "synthetic-new-generation")

    def test_rotated_refresh_checkpoint_recovers_without_changing_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            record = accounts.token_record(tokens())
            path = root / "auth/old.json"
            accounts.api_profile.write_private(path, json.dumps(record))
            alias = "account-" + accounts.fingerprint(record)

            @contextmanager
            def validate(_root, value):
                try:
                    yield {"models": {"gpt-6.1-sol": {}}}
                finally:
                    value.update(refresh_token="synthetic-rotated-refresh", access_token="synthetic-rotated-access")

            class Manager:
                failed = True
                def files(self): return [{"name": "old.json", "disabled": True}]
                def call(self, method, resource, body):
                    if self.failed:
                        raise RuntimeError("synthetic interrupted persistence")
                    self_test.assertTrue(body["disabled"])
                    accounts.api_profile.write_private(path, json.dumps(body))

            self_test = self
            manager = Manager()
            with patch.object(accounts, "validation_route", side_effect=validate), \
                    patch.object(accounts, "Management", return_value=manager), \
                    patch.object(accounts, "inference", return_value={"status": "completed", "output": [{}]}):
                with self.assertRaisesRegex(RuntimeError, "interrupted persistence"):
                    accounts.check_account(root, alias)
                checkpoint, = (root / "accounts").glob("refresh-*.pending")
                with self.assertRaisesRegex(RuntimeError, "account recover"):
                    accounts.select_record(root, alias)
                manager.failed = False
                with redirect_stdout(StringIO()):
                    accounts.recover_account(root)
            self.assertFalse(checkpoint.exists())
            saved = accounts.select_record(root, alias)
            self.assertEqual(saved["refresh_token"], "synthetic-rotated-refresh")
            self.assertEqual(saved["access_token"], "synthetic-rotated-access")
            self.assertTrue(saved["disabled"])

    def test_refresh_recovery_never_overwrites_a_newer_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            record = accounts.token_record(tokens())
            accounts.api_profile.write_private(root / "auth/old.json", json.dumps({
                **record, "refresh_token": "synthetic-newer-refresh"}))
            checkpoint = accounts.stage_path(root, "secondary").parent / (
                "refresh-" + accounts.fingerprint(record) + ".pending")
            import hashlib
            accounts.api_profile.write_private(checkpoint, json.dumps({
                "name": "old.json", "record": {**record, "refresh_token": "synthetic-pending-refresh"},
                "before_token_hash": hashlib.sha256(record["refresh_token"].encode()).hexdigest()}))
            with patch.object(accounts, "Management") as manager:
                with self.assertRaisesRegex(RuntimeError, "newer refresh"):
                    accounts.recover_account(root)
                manager.assert_not_called()
            self.assertTrue(checkpoint.exists())
            self.assertEqual(accounts.private_json(root / "auth/old.json")["refresh_token"],
                             "synthetic-newer-refresh")

    def test_inactive_validation_does_not_lose_rotated_refresh_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            record = accounts.token_record(tokens())
            path = root / "auth/old.json"
            accounts.api_profile.write_private(path, json.dumps(record))
            alias = "account-" + accounts.fingerprint(record)
            @contextmanager
            def validate(_root, value):
                try:
                    yield {"models": {"gpt-6.1-sol": {}}}
                finally:
                    value.update(refresh_token="synthetic-rotated-refresh", access_token="synthetic-rotated-access")
            class Manager:
                def files(self): return [{"name": "old.json", "disabled": True}]
                def call(self, method, resource, body):
                    self_test.assertEqual(method, "POST")
                    self_test.assertTrue(body["disabled"])
                    accounts.api_profile.write_private(path, json.dumps(body))
            self_test = self
            with patch.object(accounts, "validation_route", side_effect=validate), \
                    patch.object(accounts, "Management", return_value=Manager()), \
                    patch.object(accounts, "inference", return_value={"status": "completed", "output": [{}]}):
                accounts.check_account(root, alias)
            saved = accounts.private_json(path)
            self.assertEqual(saved["refresh_token"], "synthetic-rotated-refresh")
            self.assertEqual(saved["access_token"], "synthetic-rotated-access")
            self.assertTrue(saved["disabled"])

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
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accounts.private_directory(root / "auth")
            record = {**accounts.token_record(tokens()), "disabled": False}
            accounts.api_profile.write_private(root / "auth/new.json", json.dumps(record))
            session = root / "runtime/session-fixture"
            session.mkdir(parents=True)
            (session / "key").write_text("synthetic-loopback-key")
            (root / "bridge_models.py").write_text("print('{}')\n", encoding="utf-8")
            state = {"pid": 1234, "port": 12345, "session": "session-fixture"}
            with patch.object(accounts, "Management", return_value=SimpleNamespace(state=state)), \
                    patch.object(accounts, "isolated_account") as isolated:
                with accounts.validation_route(root, record) as route:
                    self.assertEqual(route["url"], "http://127.0.0.1:12345")
                    self.assertEqual(route["key"], "synthetic-loopback-key")
                isolated.assert_not_called()


if __name__ == "__main__":
    unittest.main()
