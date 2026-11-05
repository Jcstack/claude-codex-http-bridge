"""Exercise wrapper Python startup using temporary, synthetic configuration."""

import json
from itertools import product
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import venv
from unittest.mock import patch

import install


SOURCE = Path(__file__).resolve().parent.parent


class LauncherPythonEnvironmentTest(unittest.TestCase):
    def test_wrapper_preserves_plugin_env_but_ignores_pythonpath_at_startup(self):
        with tempfile.TemporaryDirectory(prefix="bridge-launcher-fixture-") as temporary:
            root = Path(temporary) / "launcher 模型 root"
            root.mkdir()
            custom = root / "untrusted-pythonpath"
            custom.mkdir()
            marker = root / "synthetic-sitecustomize-ran"
            (custom / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
            (root / "trusted_sibling.py").write_text("VALUE = 'fixture'\n", encoding="utf-8")
            (root / "launcher.py").write_text('''import json,os,sys
import trusted_sibling
print(json.dumps({"ignore_python_env":bool(sys.flags.ignore_environment),
    "no_user_site":bool(sys.flags.no_user_site),"utf8_mode":bool(sys.flags.utf8_mode),"plugin_env":os.environ.get("PLUGIN_FIXTURE"),
    "pythonpath_env":os.environ.get("PYTHONPATH"),"sibling":trusted_sibling.VALUE,
    "prefix":sys.prefix,"arguments":sys.argv[1:]}))
''', encoding="utf-8")
            allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG")
            environment = {key: os.environ[key] for key in allowed if key in os.environ}
            environment.update(HOME=str(root), USERPROFILE=str(root), PYTHONPATH=str(custom),
                               PLUGIN_FIXTURE="preserved", CLAUDE_CODEX_BRIDGE_HOME=str(root))
            baseline = subprocess.run([sys.executable, str(root / "launcher.py"), "fixture"], env=environment,
                                      capture_output=True, text=True, timeout=20, check=True)
            self.assertTrue(marker.exists())
            self.assertFalse(json.loads(baseline.stdout)["ignore_python_env"])
            marker.unlink()

            parent_venv = root / "selected-venv"
            venv.EnvBuilder(with_pip=False).create(parent_venv)
            python = parent_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            purelib = subprocess.run([str(python), "-E", "-s", "-X", "utf8", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                     env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20, check=True).stdout.strip()
            trusted_marker = root / "selected-interpreter-pth-ran"
            (Path(purelib) / "synthetic-fixture.pth").write_text(
                "import os; open(os.environ['TRUSTED_MARKER_FIXTURE'], 'w').close()\n", encoding="ascii")
            environment["TRUSTED_MARKER_FIXTURE"] = str(trusted_marker)

            if os.name == "nt":
                with patch.object(install.sys, "executable", str(python)):
                    command, wrapper = install.command_spec(root, root)
                command.write_bytes(wrapper)
                run = [environment["COMSPEC"], "/d", "/c", str(command), "fixture"]
            else:
                environment["CLAUDE_CODEX_PYTHON_BIN"] = str(python)
                run = ["/bin/bash", str(SOURCE / "run-claude-codex.sh"), "fixture"]
            result = subprocess.run(run, env=environment, capture_output=True, text=True, timeout=30, check=True)
            value = json.loads(result.stdout)
            self.assertFalse(marker.exists())
            # The selected interpreter's own site remains trusted. Keep any
            # platform diagnostics to booleans, never paths or environment.
            self.assertTrue(trusted_marker.exists(), json.dumps({"ignore_python_env": value["ignore_python_env"],
                "no_user_site": value["no_user_site"], "utf8_mode": value["utf8_mode"],
                "prefix_matches": Path(value["prefix"]).resolve() == parent_venv.resolve(),
                "pth_error": "Error processing line" in result.stderr}))
            self.assertTrue(value["ignore_python_env"])
            self.assertTrue(value["no_user_site"])
            self.assertTrue(value["utf8_mode"])
            self.assertEqual(value["plugin_env"], "preserved")
            self.assertEqual(value["pythonpath_env"], str(custom))
            self.assertEqual(value["sibling"], "fixture")
            self.assertEqual(Path(value["prefix"]).resolve(), parent_venv.resolve())
            self.assertEqual(value["arguments"], ["fixture"])

    @unittest.skipIf(os.name == "nt", "The tmux wrapper is POSIX-only")
    def test_tmux_wrapper_keeps_exit_status_and_restores_automatic_rename(self):
        for exit_code, command_name in product((0, 7), ("claude-codex", "harnesslift", "untrusted\nname")):
            with self.subTest(exit_code=exit_code, command_name=command_name), tempfile.TemporaryDirectory(prefix="bridge-tmux-fixture-") as temporary:
                root = Path(temporary)
                bin_dir = root / "bin"
                bin_dir.mkdir()
                state = root / "name"
                state.write_text("original", encoding="utf-8")
                restored = root / "restored"
                tmux = bin_dir / "tmux"
                tmux.write_text('''#!/bin/bash
case "$1" in
  display-message)
    if [[ "${!#}" == '#{window_id}' ]]; then printf '%s\\n' '@fixture'; else cat "$FIXTURE_TMUX_STATE"; fi ;;
  show-option) printf '%s\\n' 'on' ;;
  rename-window) printf '%s' "${!#}" > "$FIXTURE_TMUX_STATE" ;;
  set-window-option) printf '%s' "${!#}" > "$FIXTURE_TMUX_RESTORED" ;;
  *) exit 2 ;;
esac
''', encoding="utf-8")
                tmux.chmod(0o700)
                (root / "launcher.py").write_text(f"import sys\nraise SystemExit({exit_code})\n", encoding="utf-8")
                environment = {key: os.environ[key] for key in ("TMPDIR", "LANG") if key in os.environ}
                environment.update(PATH=str(bin_dir) + os.pathsep + os.defpath, HOME=str(root), TMUX_PANE="%fixture",
                    CLAUDE_CODEX_BRIDGE_HOME=str(root), CLAUDE_CODEX_PYTHON_BIN=sys.executable,
                    CLAUDE_CODEX_COMMAND_NAME=command_name,
                    FIXTURE_TMUX_STATE=str(state), FIXTURE_TMUX_RESTORED=str(restored))
                result = subprocess.run(["/bin/bash", str(SOURCE / "run-claude-codex.sh")], env=environment,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, exit_code)
                self.assertEqual(restored.read_text(encoding="utf-8"), "on")
                self.assertEqual(state.read_text(encoding="utf-8"),
                                 "harnesslift" if command_name == "harnesslift" else "claude-codex")


if __name__ == "__main__":
    unittest.main()
