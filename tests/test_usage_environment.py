"""Exercise quota-helper environment isolation with synthetic credentials only."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import codex_usage


class UsageEnvironmentTest(unittest.TestCase):
    def test_windows_environment_has_no_case_duplicate_proxies(self):
        with patch.object(codex_usage.os, "name", "nt"), \
             patch.object(codex_usage.os, "environ", {"HTTPS_PROXY": "http://proxy.invalid", "https_proxy": "http://other.invalid"}):
            environment = codex_usage.usage_environment()
        self.assertEqual(environment["HTTPS_PROXY"], "http://proxy.invalid")
        self.assertNotIn("https_proxy", environment)

    def test_background_refresh_does_not_inherit_unrelated_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "usage.json"
            supplied = {"OPENAI_API_KEY": "SYNTHETIC_PRIVATE_KEY", "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_TOKEN",
                        "AWS_SECRET_ACCESS_KEY": "SYNTHETIC_AWS", "PYTHONPATH": "SYNTHETIC_IMPORT_PATH",
                        "NODE_OPTIONS": "SYNTHETIC_NODE_OPTIONS", "DYLD_INSERT_LIBRARIES": "SYNTHETIC_LIBRARY",
                        "CODEX_HOME": str(Path(temporary) / "codex"), "HTTPS_PROXY": "http://proxy.invalid:1234",
                        "NO_PROXY": "localhost,example.invalid"}
            with patch.dict(os.environ, supplied), patch.object(codex_usage, "read_cache", return_value=None), \
                 patch.object(codex_usage, "selected_account_id", return_value="synthetic"), \
                 patch.object(codex_usage.platform, "system", return_value="Linux"), \
                 patch.object(codex_usage.subprocess, "Popen") as spawn:
                codex_usage.start_refresh(cache)
            command = spawn.call_args.args[0]
            env = spawn.call_args.kwargs["env"]
            self.assertEqual(command[1], "-I")
            self.assertEqual(env["CODEX_HOME"], supplied["CODEX_HOME"])
            self.assertEqual(env["HTTPS_PROXY"], supplied["HTTPS_PROXY"])
            self.assertEqual(env["NO_PROXY"], supplied["NO_PROXY"])
            for key in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH",
                        "NODE_OPTIONS", "DYLD_INSERT_LIBRARIES"):
                self.assertNotIn(key, env)
            self.assertNotIn("SYNTHETIC_PRIVATE_KEY", json.dumps(command))

    def test_fake_codex_rpc_receives_only_allowlisted_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / "fake_codex.py"
            fake.write_text('''import json,os,sys
for line in sys.stdin:
    message=json.loads(line)
    if message.get("id")==1:
        result={}
    elif message.get("id")==2:
        result={"accountId":"synthetic", "isolated":all(name not in os.environ for name in
            ("OPENAI_API_KEY","ANTHROPIC_AUTH_TOKEN","AWS_SECRET_ACCESS_KEY","PYTHONPATH")),
            "codex_home_preserved":os.environ.get("CODEX_HOME")==sys.argv[1],
            "proxy_preserved":os.environ.get("HTTPS_PROXY")=="http://proxy.invalid:1234"}
    else:
        continue
    print(json.dumps({"id":message["id"],"result":result}),flush=True)
''', encoding="utf-8")
            native_home = str(root / "native-home")
            supplied = {"HOME": str(root), "USERPROFILE": str(root), "CODEX_HOME": native_home,
                        "OPENAI_API_KEY": "SYNTHETIC_PRIVATE_KEY", "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_TOKEN",
                        "AWS_SECRET_ACCESS_KEY": "SYNTHETIC_AWS", "PYTHONPATH": "SYNTHETIC_IMPORT_PATH",
                        "HTTPS_PROXY": "http://proxy.invalid:1234"}
            real_popen = subprocess.Popen
            children = []

            def fake_launch(command, **kwargs):
                self.assertEqual(command, ["fixture-codex", "app-server"])
                child = real_popen([sys.executable, "-I", str(fake), native_home], **kwargs)
                children.append(child)
                return child

            try:
                with patch.dict(os.environ, supplied), patch.object(codex_usage.shutil, "which", return_value="fixture-codex"), \
                     patch.object(codex_usage.subprocess, "Popen", side_effect=fake_launch):
                    result = codex_usage._rpc_rate_limits()
            finally:
                for child in children:
                    for stream in (child.stdin, child.stdout):
                        if stream is not None:
                            stream.close()
            self.assertEqual(result, {"accountId": "synthetic", "isolated": True,
                                      "codex_home_preserved": True, "proxy_preserved": True})

    def test_isolated_interpreter_can_import_local_helper_without_state(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"HOME": temporary, "USERPROFILE": temporary}):
            result = subprocess.run([sys.executable, "-I", codex_usage.__file__, "--invalid-fixture-option"],
                                    env=codex_usage.usage_environment(), capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((result.stdout, result.stderr), ("", ""))

    @unittest.skipIf(os.name == "nt" and not os.environ.get("BRIDGE_TEST_FAKE_CODEX_EXE"),
                     "The compiled Windows fake Codex fixture is unavailable")
    def test_detached_refresh_writes_only_a_synthetic_matching_account_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller, native, bin_dir, runtime = (root / name for name in ("controller", "native", "bin", "runtime"))
            for directory in (controller, native, bin_dir, runtime, controller / "auth"):
                directory.mkdir(mode=0o700)
            project = Path(codex_usage.__file__).resolve().parent
            for name in ("accounts.py", "api_profile.py", "windows_acl.py"):
                shutil.copyfile(project / name, controller / name)
            source = (project / "codex_usage.py").read_text(encoding="utf-8")
            # Refuse the fallback HTTPS path even if this fixture is malformed.
            guard = 'if __name__ == "__main__":\n    raise SystemExit(main())'
            self.assertEqual(source.count(guard), 1)
            source = source.replace(guard,
                                    '_bridge_rate_limits = lambda *_args: None\n\nif __name__ == "__main__":\n    raise SystemExit(main())')
            (controller / "codex_usage.py").write_text(source, encoding="utf-8")
            account = {"account_id": "fixture-account", "email": "fixture@example.invalid"}
            for path, value in ((controller / "auth" / "fixture.json", {"type": "codex", **account}),
                                (native / "auth.json", {"tokens": account})):
                path.write_text(json.dumps(value), encoding="utf-8")
                path.chmod(0o600)
            fake = bin_dir / ("codex.exe" if os.name == "nt" else "codex")
            if os.name == "nt":
                shutil.copyfile(os.environ["BRIDGE_TEST_FAKE_CODEX_EXE"], fake)
            else:
                fake.write_text(f'''#!{sys.executable}
import json,os,sys
safe=all(name not in os.environ for name in ("OPENAI_API_KEY","PYTHONPATH"))
safe=safe and os.environ.get("CODEX_HOME")=={str(native)!r} and os.environ.get("HTTPS_PROXY")=="http://proxy.invalid:1234"
for line in sys.stdin:
    message=json.loads(line)
    if message.get("id")==1:
        result={{}}
    elif message.get("id")==2:
        result={{"accountId":"fixture-account" if safe else "wrong-account", "rateLimitsByLimitId":{{"codex":{{
            "secondary":{{"windowDurationMins":10080,"usedPercent":12.5,"resetsAt":2000000000}}}}}}}}
    else:
        continue
    print(json.dumps({{"id":message["id"],"result":result}}),flush=True)
''', encoding="utf-8")
                fake.chmod(0o700)
            cache = runtime / "usage.json"
            environment = {"PATH": str(bin_dir), "HOME": str(root), "CODEX_HOME": str(native),
                           "HTTPS_PROXY": "http://proxy.invalid:1234", "OPENAI_API_KEY": "SYNTHETIC_PRIVATE_KEY",
                           "PYTHONPATH": "SYNTHETIC_IMPORT_PATH"}
            if os.name == "nt":
                for name in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
                    if name in os.environ:
                        environment[name] = os.environ[name]
                environment["USERPROFILE"] = str(root)
                environment["PATH"] += os.pathsep + str(Path(os.environ["SYSTEMROOT"]) / "System32")
            command = [sys.executable, "-I", "-c",
                       "import sys; sys.path.insert(0,sys.argv[1]); import codex_usage; from pathlib import Path; codex_usage.start_refresh(Path(sys.argv[2]))",
                       str(controller), str(cache)]
            result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual((result.stdout, result.stderr), ("", ""))
            deadline = time.monotonic() + 12
            lock = cache.with_suffix(".lock")
            while time.monotonic() < deadline and (lock.exists() or not cache.exists()):
                time.sleep(0.05)
            self.assertFalse(lock.exists())
            value = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(set(value), {"fetched_at", "account_fingerprint", "windows"})
            self.assertEqual(value["windows"]["seven_day"]["used_percentage"], 12.5)


if __name__ == "__main__":
    unittest.main()
