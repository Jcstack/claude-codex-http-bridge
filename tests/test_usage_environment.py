"""Exercise quota-helper environment isolation with synthetic credentials only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import codex_usage


class UsageEnvironmentTest(unittest.TestCase):
    def test_background_refresh_does_not_inherit_unrelated_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "usage.json"
            supplied = {"OPENAI_API_KEY": "SYNTHETIC_PRIVATE_KEY", "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_TOKEN",
                        "AWS_SECRET_ACCESS_KEY": "SYNTHETIC_AWS", "PYTHONPATH": "SYNTHETIC_IMPORT_PATH",
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
            for key in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH"):
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


if __name__ == "__main__":
    unittest.main()
