"""Check Windows system-tool resolution with disposable fake executables."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import launcher
import windows_acl


SOURCE = Path(__file__).resolve().parent.parent


class WindowsSystemToolsTest(unittest.TestCase):
    def test_unapproved_tool_names_are_rejected(self):
        for name in ("cmd", "../whoami", "whoami.exe", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                windows_acl.system_tool(name)

    def test_windows_call_sites_request_explicit_system_tools(self):
        def system_tool(name):
            return str(Path("fixture-system") / (name + ".exe"))

        fake_sid = b'"fixture","S-1-5-21-1234"\r\n'
        with patch.object(windows_acl, "system_tool", side_effect=system_tool) as resolve, \
             patch.object(windows_acl.subprocess, "run") as run:
            run.return_value.stdout = fake_sid
            self.assertEqual(windows_acl.current_user_sid(), "S-1-5-21-1234")
            windows_acl.run_icacls("synthetic-argument")
        self.assertEqual([call.args[0] for call in resolve.call_args_list], ["whoami", "icacls"])
        self.assertEqual(run.call_args_list[0].args[0][0], system_tool("whoami"))
        self.assertEqual(run.call_args_list[1].args[0][0], system_tool("icacls"))
        with patch.object(launcher.platform, "system", return_value="Windows"), \
             patch.object(windows_acl, "system_tool", side_effect=system_tool) as resolve, \
             patch.object(launcher.subprocess, "check_output", return_value="") as check:
            self.assertEqual(launcher.listener_pids(12345), set())
        resolve.assert_called_once_with("netstat")
        self.assertEqual(check.call_args.args[0][0], system_tool("netstat"))

    @unittest.skipUnless(os.name == "nt" and os.environ.get("BRIDGE_TEST_FAKE_CODEX_EXE"),
                         "Requires the compiled disposable Windows executable")
    def test_windows_cwd_executables_do_not_replace_system_tools(self):
        with tempfile.TemporaryDirectory(prefix="bridge-win-tool-fixture-") as temporary:
            root = Path(temporary)
            work = root / "work"
            work.mkdir()
            secure = root / "secure"
            secure.mkdir()
            for name in ("whoami.exe", "icacls.exe", "netstat.exe"):
                shutil.copyfile(os.environ["BRIDGE_TEST_FAKE_CODEX_EXE"], work / name)
            program = r'''
import json,subprocess,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import launcher,windows_acl
controls=[subprocess.run([name,*args],capture_output=True).returncode==2 for name,args in
          (("whoami",["/user","/fo","csv","/nh"]),("netstat",["-ano","-p","tcp"]),("icacls",["/?"]))]
sid=windows_acl.current_user_sid()
windows_acl.lock([Path(sys.argv[2])])
windows_acl.verify([Path(sys.argv[2])],protected=True)
owners=launcher.listener_pids(1)
print(json.dumps({"fake_cwd_controls":controls,"system_sid_valid":sid.startswith("S-1-"),
                  "system_acl_verified":True,"listener_result_is_set":isinstance(owners,set)}))
'''
            allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE")
            environment = {name: os.environ[name] for name in allowed if name in os.environ}
            result = subprocess.run([sys.executable, "-I", "-c", program, str(SOURCE), str(secure)],
                                    cwd=work, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, "Synthetic Windows tool check failed")
            value = json.loads(result.stdout)
            if value["fake_cwd_controls"] != [True, True, True]:
                self.skipTest("Windows policy did not resolve all synthetic CWD controls")
            self.assertTrue(value["system_sid_valid"])
            self.assertTrue(value["system_acl_verified"])
            self.assertTrue(value["listener_result_is_set"])


if __name__ == "__main__":
    unittest.main()
