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
import api_profile
import codex_usage
import install
import launcher
import statusline_adapter
import webfetch_pdf_guard
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
    def test_default_context_is_not_the_advertised_maximum(self) -> None:
        row = {**model(272_000), "max_context_window": 872_000, "auto_compact_token_limit": None}
        selected = bridge_models.context_metadata(row)
        self.assertEqual(selected["default_context_window"], 272_000)
        self.assertEqual(selected["context_window"], 872_000)
        self.assertEqual(selected["context_window_source"], "max_context_window")
        self.assertEqual(bridge_models.compact_window(selected), 784_800)
        self.assertEqual(row["context_window"], 272_000)  # no mutation of source metadata
        future = bridge_models.context_metadata({**row, "max_context_window": 960_000})
        self.assertEqual(bridge_models.compact_window(future), 864_000)
        native_limit = {**selected, "auto_compact_token_limit": 700_123}
        self.assertEqual(bridge_models.compact_window(native_limit), 700_123)
        absent_default = bridge_models.context_metadata({**row, "context_window": None})
        self.assertEqual(absent_default["context_window"], 872_000)
        for maximum in (True, "872000", 100_000, -1):
            invalid = bridge_models.context_metadata({**row, "max_context_window": maximum})
            self.assertFalse(bridge_models.eligible(invalid))
        huge = bridge_models.context_metadata({**row, "max_context_window": 2_000_000})
        self.assertEqual(huge["context_window"], 2_000_000)
        self.assertEqual(bridge_models.compact_window(huge), 1_000_000)  # client setting range only

    def test_gateway_tool_streaming_defaults_on_and_respects_opt_out(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            environment = launcher.claude_environment(12345, "mock-local-key", "gpt-6.1-sol")
            self.assertEqual(environment["CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"], "1")
            self.assertEqual(environment["CLAUDE_CODE_ATTRIBUTION_HEADER"], "0")
            self.assertEqual(environment["CLAUDE_CODE_GATEWAY_HINT_HEADERS"], "1")
            self.assertEqual(environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
            api_environment = launcher.claude_environment(12345, "mock-local-key",
                                                         "gpt-6.1-sol", "api")
            self.assertNotIn("CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING", api_environment)
        with patch.dict(os.environ, {"CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING": "0"}):
            environment = launcher.claude_environment(12345, "mock-local-key", "gpt-6.1-sol")
            self.assertEqual(environment["CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"], "0")
            api_environment = launcher.claude_environment(12345, "mock-local-key",
                                                         "gpt-6.1-sol", "api")
            self.assertEqual(api_environment["CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"], "0")
        with patch.dict(os.environ, {"CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING": "1"}):
            with self.assertRaisesRegex(RuntimeError, "API route"):
                launcher.claude_environment(12345, "mock-local-key", "gpt-6.1-sol", "api")
        with patch.dict(os.environ, {"CLAUDE_CODE_ATTRIBUTION_HEADER": "1", "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "0", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0"}):
            environment = launcher.claude_environment(12345, "mock-local-key", "gpt-6.1-sol")
            self.assertEqual(environment["CLAUDE_CODE_ATTRIBUTION_HEADER"], "1")
            self.assertEqual(environment["CLAUDE_CODE_GATEWAY_HINT_HEADERS"], "0")
            self.assertEqual(environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "0")
        with patch.dict(os.environ, {"CLAUDE_CODE_USE_ANTHROPIC_AWS": "1"}):
            self.assertNotIn("CLAUDE_CODE_USE_ANTHROPIC_AWS", launcher.claude_environment(12345,"mock-local-key","gpt-6.1-sol"))

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
            with patch.dict(os.environ, {"CODEX_BRIDGE_KEY_FILE": str(key), "CLAUDE_CODEX_PDF_GUARD": "1"}), patch.object(
                bridge_models, "get_json", side_effect=[public, rich]
            ), patch.object(bridge_models, "ROOT", Path(directory)):
                chat, images = bridge_models.discover()
                self.assertEqual(set(chat), set(rows) - {"gpt-image-1", "gpt-6-hidden"})
                self.assertEqual(images, ["gpt-image-1"])
                self.assertEqual(bridge_models.aliases(chat)["sol"], "gpt-6.1-sol")
                self.assertFalse(bridge_models.eligible(chat["gpt-6.1-luna"]))
                guarded_path = bridge_models.runtime_settings(chat)
                settings = json.loads(guarded_path.read_text())
                with patch.dict(os.environ, {"CLAUDE_CODEX_PDF_GUARD": "0"}):
                    unguarded_path = bridge_models.runtime_settings(chat)
                self.assertNotEqual(guarded_path, unguarded_path)
                self.assertIn("hooks", json.loads(guarded_path.read_text()))
                self.assertNotIn("hooks", json.loads(unguarded_path.read_text()))
            options = settings["modelPicker"]["options"]
            self.assertEqual({row["model"] for row in options}, {
                "gpt-6.1-sol", "gpt-6-sol", "gpt-6.1-astra"
            })
            self.assertEqual(settings["modelSettings"]["gpt-6.1-sol"]["autoCompactWindow"], 400_000)
            self.assertEqual(settings["modelSettings"]["gpt-6-sol"]["autoCompactWindow"], 200_000)
            self.assertTrue(settings["modelPicker"]["replaceBuiltInOptions"])
            hook = settings["hooks"]["PostToolUse"][0]
            self.assertEqual(hook["matcher"], "WebFetch")
            self.assertEqual(hook["hooks"][0]["command"], sys.executable)
            self.assertIn("webfetch_pdf_guard.py", hook["hooks"][0]["args"][0])

    def test_webfetch_pdf_guard_replaces_unreadable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory) / "tool-results"
            results.mkdir()
            saved = results / "webfetch-example.pdf"
            saved.write_bytes(b"%PDF-1.7\nfixture")
            original = f"A plausible but unverified quote.\n[Binary content (application/pdf, 19B) also saved to {saved}]"
            event = {"hook_event_name": "PostToolUse", "tool_name": "WebFetch",
                     "tool_response": {"content": original, "meta": {"ok": True}}}
            output = webfetch_pdf_guard.transform(event)
            self.assertIsNotNone(output)
            replacement = output["hookSpecificOutput"]["updatedToolOutput"]
            self.assertEqual(replacement["meta"], {"ok": True})
            self.assertNotIn("plausible", replacement["content"])
            self.assertIn(str(saved), replacement["content"])
            self.assertIn("unverified", replacement["content"])
            self.assertIsNone(webfetch_pdf_guard.transform({**event, "tool_name": "WebSearch"}))
            self.assertIsNone(webfetch_pdf_guard.transform({**event, "tool_response": "Ordinary HTML"}))

    def test_api_route_uses_official_ids_and_private_metadata(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "approved OpenAI endpoint"):
            api_profile.fetch_text("https://example.invalid/v1/models", "mock-project-key-123456789")
        with self.assertRaisesRegex(RuntimeError, "only be sent"):
            api_profile.fetch_text(api_profile.DOCS_CATALOG_URL, "mock-project-key-123456789")
        page = """# GPT-6.1 Sol
Model ID: `gpt-6.1-sol`
`reasoning.effort` supports `low`, `medium`, `high`, `xhigh`, and `max`.
## Model details
- Input modalities: text, image
- 1,050,000 context window
## Supported tools
- web_search
- computer_use
## Snapshots
"""
        sources = {
            api_profile.API_MODELS_URL: json.dumps({"data": [
                {"id": "gpt-6.1-sol"}, {"id": "gpt-image-2"},
                {"id": "gpt-6.1-not-documented"}, {"id": "gpt-7-nova"},
                {"id": "gpt-5.6-sol"},
            ]}),
            api_profile.DOCS_CATALOG_URL: ("[Sol](/api/docs/models/gpt-6.1-sol.md) "
                                            "[Nova](/api/docs/models/gpt-7-nova.md) "
                                            "[Older](/api/docs/models/gpt-5.6-sol.md)"),
            api_profile.DOCS_MODEL_BASE + "gpt-6.1-sol.md": page,
            api_profile.DOCS_MODEL_BASE + "gpt-7-nova.md": (
                page.replace("GPT-6.1 Sol", "GPT-7 Nova")
                    .replace("gpt-6.1-sol", "gpt-7-nova")
                    .replace("1,050,000", "2,000,000")
            ),
            api_profile.DOCS_MODEL_BASE + "gpt-5.6-sol.md": (
                page.replace("GPT-6.1 Sol", "GPT-5.6 Sol")
                    .replace("gpt-6.1-sol", "gpt-5.6-sol")
                    .replace("`reasoning.effort` supports `low`, `medium`, `high`, `xhigh`, and `max`.",
                             "Reasoning.effort supports: none, low, medium (default), high, xhigh, and max.")
            ),
        }
        with patch.object(api_profile, "fetch_text", side_effect=lambda url, key=None: sources[url]) as fetch:
            models = api_profile.discover_models("mock-project-key-123456789")
        self.assertEqual(list(models), ["gpt-5.6-sol", "gpt-6.1-sol", "gpt-7-nova"])
        self.assertEqual(models["gpt-6.1-sol"]["context_window"], 1_050_000)
        self.assertEqual(models["gpt-7-nova"]["context_window"], 2_000_000)
        self.assertIn("none", [x["effort"] for x in models["gpt-5.6-sol"]["supported_reasoning_levels"]])
        self.assertTrue(all(call.args[0] == api_profile.API_MODELS_URL or not call.args[1:]
                            for call in fetch.call_args_list))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime-api"
            runtime.mkdir(mode=0o700)
            if platform.system() == "Windows":
                windows_acl.lock([root, runtime])
            api_profile.set_key(root, "mock-project-key-123456789")
            with patch.object(api_profile, "discover_models", return_value=models):
                port, session_dir, config_path, local_key_path, _ = api_profile.make_session(root, runtime)
            self.assertTrue(1 <= port <= 65535)
            config = config_path.read_text()
            self.assertIn("https://api.openai.com/v1", config)
            self.assertIn("max-context-length: 1050000", config)
            self.assertIn("max-context-length: 2000000", config)
            self.assertIn('disable-image-generation: "passthrough"', config)
            self.assertIn('    request-retry: 2', config)
            self.assertIn('    transient-error-cooldown-seconds: -1', config)
            for setting in ('host: "127.0.0.1"', 'allow-remote: false',
                            'disable-control-panel: true', 'request-log: false',
                            'usage-statistics-enabled: false'):
                self.assertIn(setting, config)
            self.assertIn("mock-project-key-123456789", config)
            self.assertIn(json.dumps(str(root / "api-auth")), config)
            self.assertNotIn(json.dumps(str(root / "auth")), config)
            self.assertNotIn("mock-project-key-123456789", local_key_path.read_text())
            self.assertEqual(json.loads((runtime / "catalog.json").read_text()), models)
            if platform.system() == "Windows":
                windows_acl.verify([config_path, local_key_path, runtime / "catalog.json"], protected=True)
            else:
                self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(local_key_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual((runtime / "catalog.json").stat().st_mode & 0o777, 0o600)
            self.assertTrue(session_dir.is_dir())

            key = local_key_path.read_text().strip()
            public = {"data": [{"id": "gpt-6.1-sol"}]}
            rich = {"models": [{"slug": "gpt-6.1-sol", "context_window": 500_000,
                                "max_context_window": 872_000}]}
            with patch.dict(os.environ, {"CODEX_BRIDGE_ROUTE": "api",
                                      "CODEX_BRIDGE_API_METADATA_FILE": str(runtime / "catalog.json"),
                                      "CODEX_BRIDGE_KEY_FILE": str(local_key_path),
                                      "CODEX_BRIDGE_PORT": str(port)}), patch.object(
                bridge_models, "get_json", side_effect=[public, rich]
            ):
                chat, _ = bridge_models.discover()
            self.assertEqual(chat["gpt-6.1-sol"]["context_window"], 1_050_000)
            self.assertTrue(bridge_models.eligible(chat["gpt-6.1-sol"]))

    def test_session_key_and_config_are_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / "auth"
            auth.mkdir(mode=0o700)
            template = (Path(install.__file__).parent / "config.example.yaml").read_text()
            config = template.replace("__LOCAL_KEY__", "sk-local-" + "a" * 64)
            config = config.replace("__MANAGEMENT_KEY__", "b" * 64)
            config = config.replace("__AUTH_DIR__", json.dumps(str(auth)))
            config = config.replace("routing:\n  retry:\n    request-retry: 2\n"
                                    "  cooldown:\n    transient-error-cooldown-seconds: -1\n", "")
            (root / "config.yaml").write_text(config)
            output = StringIO()
            with patch.object(bridge_models, "ROOT", root), redirect_stdout(output):
                bridge_models.make_session()
            port, session_dir, config_path, key_path, _ = output.getvalue().strip().split("\t")
            self.assertTrue(port.isdigit())
            self.assertIn('    request-retry: 2', Path(config_path).read_text())
            self.assertIn('    transient-error-cooldown-seconds: -1', Path(config_path).read_text())
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

    @unittest.skipUnless(platform.system() == "Windows", "NTFS ACL inheritance requires Windows")
    def test_windows_oauth_file_inherits_private_acl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "auth"
            auth.mkdir()
            windows_acl.lock([auth])
            credential = auth / "dummy.json"
            credential.write_text("{}")
            windows_acl.verify([credential])

    def test_launcher_model_and_background_arguments(self) -> None:
        self.assertEqual(
            launcher.parse_args(["--model", "gpt-6.1-sol", "--effort", "max"]),
            ("claude", "gpt-6.1-sol", ["--effort", "max"]),
        )
        self.assertEqual(launcher.parse_args(["models"]), ("models", "", []))
        self.assertEqual(launcher.parse_args(["usage"]), ("usage", "", []))
        self.assertEqual(launcher.parse_args(["--background"]), ("claude", "sol", ["--background"]))
        self.assertEqual(launcher.parse_args(["bridge", "stop"]), ("bridge-stop", "", []))

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
        payload["model"]["id"] = "gpt-future-fixture"
        future = statusline_adapter.corrected_payload(payload, {"gpt-future-fixture": 4_000_000})
        self.assertEqual(future["context_window"]["context_window_size"], 4_000_000)
        self.assertEqual(future["context_window"]["used_percentage"], 3)

    def test_codex_quota_maps_only_real_windows_and_requires_matching_account(self) -> None:
        response = {"rateLimitsByLimitId": {"codex": {
            "primary": {"windowDurationMins": 10080, "usedPercent": 55, "resetsAt": 1793774315},
            "secondary": {"windowDurationMins": 300, "usedPercent": 18.5, "resetsAt": 1793200000},
        }}}
        windows = codex_usage.windows_from_response(response)
        self.assertEqual(windows["seven_day"]["used_percentage"], 55)
        self.assertEqual(windows["five_hour"]["used_percentage"], 18.5)
        response["rateLimitsByLimitId"]["codex"]["secondary"]["windowDurationMins"] = 60
        self.assertNotIn("five_hour", codex_usage.windows_from_response(response))
        payload = {"model": {"id": "gpt-6.1-sol"}, "rate_limits": {"five_hour": {"used_percentage": 90}}}
        corrected = statusline_adapter.corrected_payload(payload, {"gpt-6.1-sol": 500_000}, windows)
        self.assertEqual(corrected["rate_limits"], windows)
        unknown = statusline_adapter.corrected_payload({"model": {"id": "new-route"},
            "context_window": {"context_window_size": 1_000_000},
            "cost": {"total_cost_usd": 9}, "rate_limits": {"seven_day": {"used_percentage": 90}}}, {})
        self.assertNotIn("context_window_size", unknown["context_window"])
        self.assertNotIn("total_cost_usd", unknown["cost"])
        self.assertNotIn("rate_limits", unknown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "auth").mkdir(mode=0o700)
            codex_home = root / "codex"
            codex_home.mkdir(mode=0o700)
            bridge_file = root / "auth" / "account.json"
            native_file = codex_home / "auth.json"
            bridge_file.write_text(json.dumps({"account_id": "account-a"}))
            native_file.write_text(json.dumps({"tokens": {"account_id": "account-b"}}))
            bridge_file.chmod(0o600)
            native_file.chmod(0o600)
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                self.assertIsNone(codex_usage.matching_account_id(root))
                bridge_file.write_text(json.dumps({"account_id": "account-b"}))
                self.assertEqual(codex_usage.matching_account_id(root), "account-b")

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
            self.assertEqual(json.loads(files[0].read_text())["usage_cache"], str(root / "codex-usage.json"))
            self.assertEqual(json.loads(user_settings.read_text())["statusLine"]["command"], "echo HUD")
            api_dir = root / "api-session"
            api_dir.mkdir()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir)}, clear=False):
                _, api_files = launcher.session_settings(base, {"gpt-6.1-sol": 1_050_000},
                                                        api_dir, route="api")
            self.assertNotIn("usage_cache", json.loads(api_files[0].read_text()))

    def test_api_and_subscription_runtime_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, "ROOT", Path(directory)):
            subscription = launcher.runtime_directory()
            api = launcher.runtime_directory("api")
            self.assertNotEqual(subscription, api)
            self.assertEqual(subscription.name, "runtime")
            self.assertEqual(api.name, "runtime-api")

    def test_api_cli_does_not_use_subscription_login_or_quota(self) -> None:
        with patch.object(sys, "argv", ["launcher.py", "api", "key", "status"]), patch.object(
            launcher, "helper_output", return_value=""
        ), patch.object(api_profile, "key_command", return_value=0) as key_command:
            self.assertEqual(launcher.main(), 0)
            key_command.assert_called_once_with(launcher.ROOT, "status")
        with patch.object(sys, "argv", ["launcher.py", "api", "usage"]), patch.object(
            launcher, "helper_output", return_value=""
        ):
            with self.assertRaisesRegex(ValueError, "API route uses a project key"):
                launcher.main()

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
