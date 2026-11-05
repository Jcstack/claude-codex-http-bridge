"""Verify brand entry points without touching accounts or installed services."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import install


class InstallerAliasesTest(unittest.TestCase):
    def test_either_name_conflict_is_rejected_before_installation_writes(self):
        for name in install.COMMAND_NAMES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "installation"
                bin_dir = root / "bin"
                bin_dir.mkdir(parents=True)
                command, _ = install.command_spec(root, bin_dir, name=name)
                command.write_bytes(b"unrelated command")
                before = set(root.rglob("*"))
                with patch.object(sys, "argv", ["install.py", "--root", str(root), "--bin-dir", str(bin_dir)]), \
                     patch.object(install, "install_proxy") as proxy:
                    with self.assertRaisesRegex(RuntimeError, "Existing"):
                        install.main()
                self.assertEqual(set(root.rglob("*")), before)
                self.assertEqual(command.read_bytes(), b"unrelated command")
                proxy.assert_not_called()

    def test_alias_conflict_cannot_partially_replace_the_other_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            legacy, _ = install.command_spec(root, bin_dir)
            legacy.write_bytes(b"unrelated command")
            branded, _ = install.command_spec(root, bin_dir, name="harnesslift")
            with self.assertRaisesRegex(RuntimeError, "Existing"):
                install.install_commands(root, bin_dir, False)
            self.assertFalse(branded.exists())
            self.assertEqual(legacy.read_bytes(), b"unrelated command")

    def test_both_wrappers_launch_the_same_root_and_preserve_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "installation 模型 % root"
            root.mkdir()
            bin_dir = root / "bin"
            shutil.copyfile(Path(install.__file__).parent / "run-claude-codex.sh", root / "run-claude-codex.sh")
            (root / "run-claude-codex.sh").chmod(0o700)
            (root / "launcher.py").write_text(
                'import json,os,sys\nprint(json.dumps({"root":os.environ["CLAUDE_CODEX_BRIDGE_HOME"],'
                '"brand":os.environ.get("CLAUDE_CODEX_COMMAND_NAME"),"arguments":sys.argv[1:],'
                '"isolated":bool(sys.flags.ignore_environment and sys.flags.no_user_site)}))\n',
                encoding="utf-8")
            install.install_commands(root, bin_dir, False)
            # Matching wrappers must also survive a subsequent installation.
            install.install_commands(root, bin_dir, False)
            environment = {key: os.environ[key] for key in
                           ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR", "LANG")
                           if key in os.environ}
            environment.update(HOME=str(root), USERPROFILE=str(root), CLAUDE_CODEX_PYTHON_BIN=sys.executable)
            for name in install.COMMAND_NAMES:
                with self.subTest(name=name):
                    command, _ = install.command_spec(root, bin_dir, name=name)
                    if os.name == "nt":
                        # /s /c removes an outer quote pair. Python's default
                        # list quoting targets the C runtime, not CMD's parser.
                        # Keep both the spaced path and the spaced argument.
                        # https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd
                        prefix = subprocess.list2cmdline([environment["COMSPEC"], "/d", "/v:off", "/s", "/c"])
                        run = prefix + f' ""{command}" --model "synthetic model""'
                    else:
                        run = [str(command), "--model", "synthetic model"]
                    result = subprocess.run(run, env=environment, capture_output=True, text=True,
                                            encoding="utf-8", timeout=20)
                    self.assertEqual(result.returncode, 0, json.dumps({
                        "command_not_found": "not recognized" in result.stderr.lower(),
                        "path_not_found": "cannot find" in result.stderr.lower(),
                        "python_path_error": "can't open file" in result.stderr.lower()}))
                    value = json.loads(result.stdout)
                    self.assertEqual(value["root"], str(root))
                    self.assertEqual(value["arguments"], ["--model", "synthetic model"])
                    self.assertEqual(value["brand"], "harnesslift" if name == "harnesslift" else None)
                    self.assertTrue(value["isolated"])

    def test_existing_legacy_wrapper_is_reused_when_adding_brand_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            legacy, wrapper = install.command_spec(root, bin_dir)
            legacy.write_bytes(wrapper)
            with patch.object(install, "write_file", wraps=install.write_file) as writer:
                install.install_commands(root, bin_dir, False)
            self.assertEqual(legacy.read_bytes(), wrapper)
            self.assertEqual(len(writer.call_args_list), 1)
            self.assertEqual(writer.call_args.args[0].stem, "harnesslift")


if __name__ == "__main__":
    unittest.main()
