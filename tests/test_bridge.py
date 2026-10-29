"""Security and model-selection checks with no live credential or network use."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import platform
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import hashlib
import subprocess

import bridge_models
import install
import launcher
import statusline_adapter
import windows_acl


def model(context: int, search: bool = True) -> dict:
    return {
        "context_window": context,
        "auto_compact_token_limit": 400_000 if context == 500_000 else 200_000,
        "input_modalities": ["text", "image"],
        "supported_reasoning_levels": [
            {"effort": effort} for effort in ("low", "medium", "high", "xhigh", "max")
        ],
        "supports_search_tool": search,
    }


class BridgeTests(unittest.TestCase):
    def test_catalog_intersection_and_model_picker(self) -> None:
        rows = {
            "gpt-6.1-sol": model(500_000),
            "gpt-6-sol": model(272_000),
            "gpt-6.1-astra": model(500_000),
            "gpt-6.1-luna": model(272_000, search=False),
            "gpt-image-1": {"input_modalities": ["image"]},
            "gpt-6-hidden": model(500_000),
        }
        public = {"data": [{"id": slug} for slug in rows if slug != "gpt-6-hidden"]}
        rich = {"models": [{"slug": slug, **row} for slug, row in rows.items()]}
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            key.write_text("test-key\n")
            with patch.dict(os.environ, {"CODEX_BRIDGE_KEY_FILE": str(key)}), patch.object(
                bridge_models, "get_json", side_effect=[public, rich]
            ), patch.object(bridge_models, "ROOT", Path(directory)):
                chat, images = bridge_models.discover()
                self.assertEqual(set(chat), set(rows) - {"gpt-image-1", "gpt-6-hidden"})
                self.assertEqual(images, ["gpt-image-1"])
                self.assertEqual(bridge_models.aliases(chat)["sol"], "gpt-6.1-sol")
                self.assertFalse(bridge_models.eligible(chat["gpt-6.1-luna"]))
                settings = json.loads(bridge_models.runtime_settings(chat).read_text())
            options = settings["modelPicker"]["options"]
            self.assertEqual({row["model"] for row in options}, {
                "gpt-6.1-sol", "gpt-6-sol", "gpt-6.1-astra"
            })
            self.assertEqual(settings["modelSettings"]["gpt-6.1-sol"]["autoCompactWindow"], 400_000)
            self.assertEqual(settings["modelSettings"]["gpt-6-sol"]["autoCompactWindow"], 200_000)
            self.assertTrue(settings["modelPicker"]["replaceBuiltInOptions"])

    def test_session_key_and_config_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth"
            auth.mkdir(mode=0o700)
            template = (Path(install.__file__).parent / "config.example.yaml").read_text()
            config = template.replace("__LOCAL_KEY__", "sk-local-" + "a" * 64)
            config = config.replace("__MANAGEMENT_KEY__", "b" * 64)
            config = config.replace("__AUTH_DIR__", json.dumps(str(auth)))
            (root / "config.yaml").write_text(config)
            output = StringIO()
            with patch.object(bridge_models, "ROOT", root), redirect_stdout(output):
                bridge_models.make_session()
            port, session_dir, config_path, key_path, _ = output.getvalue().strip().split("\t")
            self.assertTrue(port.isdigit())
            if platform.system() == "Windows":
                import windows_acl
                windows_acl.verify([Path(session_dir), Path(config_path), Path(key_path)], protected=True)
            else:
                self.assertEqual(Path(session_dir).stat().st_mode & 0o777, 0o700)
                self.assertEqual(Path(config_path).stat().st_mode & 0o777, 0o600)
                self.assertEqual(Path(key_path).stat().st_mode & 0o777, 0o600)
            self.assertNotIn("a" * 64, Path(config_path).read_text())
            self.assertIn(Path(key_path).read_text().strip(), Path(config_path).read_text())

    def test_installer_refuses_unrelated_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "existing"
            root.mkdir(mode=0o755)
            (root / "user-file").write_text("keep")
            with self.assertRaisesRegex(RuntimeError, "unrelated nonempty directory"):
                install.install_root(root)
            if platform.system() != "Windows":
                self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / "user-file").read_text(), "keep")

    def test_installer_rejects_bad_release_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.tar.gz"
            archive.write_bytes(b"not an official release")
            with patch.object(install.platform, "system", return_value="Darwin"), patch.object(
                install.platform, "machine", return_value="arm64"
            ):
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    install.verified_archive(Path(directory), archive)

    def test_windows_zip_asset_and_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            archive = root / "release.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("cli-proxy-api.exe", b"fake binary")
                package.writestr("LICENSE", b"upstream license")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with patch.object(install, "ASSETS", {("Windows", "x86_64"): ("windows_amd64", digest)}), patch.object(
                install.platform, "system", return_value="Windows"
            ), patch.object(install.platform, "machine", return_value="AMD64"):
                install.install_proxy(root, archive)
            self.assertEqual((root / "bin" / "cli-proxy-api.exe").read_bytes(), b"fake binary")
            self.assertEqual((root / "UPSTREAM-LICENSE").read_bytes(), b"upstream license")

    def test_windows_listener_owner_parser(self) -> None:
        output = """\
  TCP    127.0.0.1:18317    0.0.0.0:0       LISTENING       1234
  TCP    127.0.0.1:18317    127.0.0.1:52144 ESTABLISHED     5678
  TCP    127.0.0.1:18318    0.0.0.0:0       LISTENING       9999
  TCP    [::1]:18317        [::]:0          LISTENING       4444
"""
        self.assertEqual(launcher.windows_listener_pids(output, 18317), {1234})

    def test_windows_sddl_local_admin_alias_is_exact(self) -> None:
        self.assertTrue(windows_acl.same_identity("LA", "S-1-5-21-42-500"))
        self.assertFalse(windows_acl.same_identity("LA", "S-1-5-21-42-1001"))
        self.assertFalse(windows_acl.same_identity("BA", "S-1-5-21-42-500"))

    def test_launcher_model_and_background_arguments(self) -> None:
        self.assertEqual(
            launcher.parse_args(["--model", "gpt-6.1-sol", "--effort", "max"]),
            ("claude", "gpt-6.1-sol", ["--effort", "max"]),
        )
        self.assertEqual(launcher.parse_args(["models"]), ("models", "", []))
        with self.assertRaisesRegex(ValueError, "Background sessions"):
            launcher.parse_args(["--background"])

    def test_statusline_uses_selected_route_and_hides_unrelated_billing(self) -> None:
        payload = {
            "model": {"id": "gpt-6.1-sol", "display_name": "GPT-6.1 Sol (Codex)"},
            "context_window": {
                "context_window_size": 1_000_000,
                "used_percentage": 12,
                "current_usage": {
                    "input_tokens": 5_000,
                    "cache_creation_input_tokens": 10_000,
                    "cache_read_input_tokens": 100_000,
                },
            },
            "cost": {"total_cost_usd": 0.537, "total_duration_ms": 120_000},
            "prompt_cache": {"caching_observed": True, "warm": True},
            "rate_limits": {"five_hour": {"used_percentage": 15}},
            "workspace": {"project_dir": "/safe/project"},
        }
        windows = {"gpt-6.1-sol": 500_000, "gpt-6-sol": 272_000}
        corrected = statusline_adapter.corrected_payload(payload, windows)
        self.assertEqual(corrected["context_window"]["context_window_size"], 500_000)
        self.assertEqual(corrected["context_window"]["used_percentage"], 23)
        self.assertNotIn("total_cost_usd", corrected["cost"])
        self.assertNotIn("prompt_cache", corrected)
        self.assertNotIn("rate_limits", corrected)
        self.assertEqual(corrected["workspace"], payload["workspace"])
        payload["model"]["id"] = "gpt-6-sol"
        switched = statusline_adapter.corrected_payload(payload, windows)
        self.assertEqual(switched["context_window"]["context_window_size"], 272_000)
        self.assertEqual(switched["context_window"]["used_percentage"], 42)

    def test_session_settings_wraps_statusline_without_changing_user_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude_dir = root / "claude"
            claude_dir.mkdir()
            user_settings = claude_dir / "settings.json"
            user_settings.write_text(json.dumps({"statusLine": {
                "type": "command", "command": "echo HUD", "refreshInterval": 30
            }}))
            base = root / "model-settings.json"
            base.write_text(json.dumps({"modelPicker": {"options": []}}))
            session_dir = root / "session"
            session_dir.mkdir()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir)}, clear=False):
                settings_path, files = launcher.session_settings(base, {"gpt-6.1-sol": 500_000}, session_dir)
            result = json.loads(settings_path.read_text())
            self.assertIn("statusline_adapter.py", result["statusLine"]["command"])
            self.assertIn(" | echo HUD", result["statusLine"]["command"])
            self.assertEqual(result["statusLine"]["refreshInterval"], 30)
            self.assertEqual(json.loads(files[0].read_text())["context_windows"], {"gpt-6.1-sol": 500_000})
            self.assertEqual(json.loads(user_settings.read_text())["statusLine"]["command"], "echo HUD")

    def test_launcher_waits_for_async_model_registration(self) -> None:
        class Proxy:
            def poll(self):
                return None

        replies = [
            subprocess.CompletedProcess([], 0, '{"chat":0,"eligible":0}'),
            subprocess.CompletedProcess([], 0, '{"chat":8,"eligible":8}'),
        ]
        with patch.object(launcher.subprocess, "run", side_effect=replies) as query, patch.object(
            launcher.time, "sleep"
        ):
            launcher.wait_for_catalog(Proxy(), {"CODEX_BRIDGE_PORT": "12345"}, True)
        self.assertEqual(query.call_count, 2)

    def test_statusline_filter_runs_in_host_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude_dir = root / "claude"
            claude_dir.mkdir()
            consumer = root / "consumer.py"
            consumer.write_text("import json,sys; print(json.load(sys.stdin)['context_window']['context_window_size'])\n")
            if platform.system() == "Windows":
                original = f'python "{consumer.as_posix()}"'
            else:
                original = shlex.join([sys.executable, str(consumer)])
            (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {
                "type": "command", "command": original
            }}))
            base = root / "base.json"
            base.write_text("{}")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir)}):
                settings, _ = launcher.session_settings(base, {"gpt-6.1-sol": 500_000}, root)
            command = json.loads(settings.read_text())["statusLine"]["command"]
            payload = json.dumps({"model": {"id": "gpt-6.1-sol"}, "context_window": {
                "context_window_size": 1_000_000, "current_usage": {"input_tokens": 100}
            }})
            execution = (["pwsh", "-NoProfile", "-NonInteractive", "-Command", command]
                         if platform.system() == "Windows" else command)
            result = subprocess.run(execution, shell=False if platform.system() == "Windows" else True,
                                    input=payload, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), "500000")
            if platform.system() == "Windows":
                git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "bin" / "bash.exe"
                if git_bash.is_file():
                    result = subprocess.run([str(git_bash), "-lc", command], input=payload,
                                            text=True, capture_output=True, check=True)
                    self.assertEqual(result.stdout.strip(), "500000")


if __name__ == "__main__":
    unittest.main()
