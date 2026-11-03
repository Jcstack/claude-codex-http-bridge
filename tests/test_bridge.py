"""Security and model-selection checks with no live credential or network use."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO, TextIOWrapper
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
import mcp_name_audit
import mcp_definition_audit
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
    def test_subagent_and_alias_models_must_be_in_live_catalog(self):
        catalog = {"gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra"}
        with patch.dict(os.environ, {}, clear=True):
            env = launcher.claude_environment(12345, "mock-local-key", "gpt-6-astra", available_models=catalog)
            self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gpt-6-astra")
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
        with patch.dict(os.environ, {"ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-6.1-sol",
                                    "CLAUDE_CODE_SUBAGENT_MODEL": "gpt-6.1-terra",
                                    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}, clear=True):
            env = launcher.claude_environment(12345, "mock-local-key", "gpt-6-astra", available_models=catalog)
            self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gpt-6.1-sol")
            self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "gpt-6-astra")
            self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "gpt-6.1-terra")
            self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"], "1")
        with patch.dict(os.environ, {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}, clear=True):
            env = launcher.claude_environment(12345, "mock-local-key", "gpt-6-astra", available_models=catalog)
            self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "sonnet")
        for name in ("CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL"):
            with self.subTest(variable=name), patch.dict(os.environ, {name: "PRIVATE_INVALID_VALUE"}, clear=True):
                with self.assertRaises(ValueError) as failure:
                    launcher.claude_environment(12345, "mock-local-key", "gpt-6-astra", available_models=catalog)
                self.assertIn(name, str(failure.exception))
                self.assertNotIn("PRIVATE_INVALID_VALUE", str(failure.exception))

    def test_native_compaction_requires_reviewed_runtime_and_keeps_other_headers(self):
        manifest = {"source": {"files": {"upstream/patches/native-compaction-v2.patch": "fixture"}}}
        with patch.dict(os.environ, {}, clear=True):
            env = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1", "ANTHROPIC_CUSTOM_HEADERS": "X-Local-Trace: fixture\nx-codex-bridge-native-compaction: 0"}
            args = launcher.native_compaction_options(env, ["--native-compaction", "--effort", "max"], "subscription", manifest)
            self.assertEqual(args, ["--effort", "max"])
            self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], "X-Local-Trace: fixture\nX-Codex-Bridge-Native-Compaction: 1")
            launcher.native_compaction_options(env, ["--no-native-compaction"], "subscription", manifest)
            self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], "X-Local-Trace: fixture\nX-Codex-Bridge-Native-Compaction: 0")
            for route, bundle in (("api", manifest), ("subscription", None), ("subscription", {})):
                with self.assertRaisesRegex(RuntimeError, "reviewed subscription runtime"):
                    launcher.native_compaction_options({}, ["--native-compaction"], route, bundle)
            args = launcher.native_compaction_options({}, ["--", "--native-compaction"], "subscription", None)
            self.assertEqual(args, ["--", "--native-compaction"])
            with self.assertRaisesRegex(RuntimeError, "HINT_HEADERS"):
                launcher.native_compaction_options({}, ["--native-compaction"], "subscription", manifest)
        with patch.dict(os.environ, {"CLAUDE_CODEX_NATIVE_COMPACTION": "1"}):
            env = {"CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1"}
            launcher.native_compaction_options(env, [], "subscription", manifest)
            self.assertIn("Compaction: 1", env["ANTHROPIC_CUSTOM_HEADERS"])

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

    def test_catalog_snapshot_includes_the_effective_compact_window(self) -> None:
        row = bridge_models.context_metadata({**model(272_000), "max_context_window": 872_000,
                                             "auto_compact_token_limit": None})
        name = "gpt-fixture"
        snapshot = bridge_models.catalog_snapshot({name: row})
        self.assertEqual(snapshot[name]["context_window"], 872_000)
        self.assertEqual(snapshot[name]["auto_compact_window"], 784_800)
        self.assertEqual(snapshot[name]["input_modalities"], ["text", "image"])
        limited = bridge_models.catalog_snapshot({name: {**row, "auto_compact_token_limit": 700_123}})
        self.assertEqual(limited[name]["auto_compact_window"], 700_123)
        self.assertNotEqual(snapshot, limited)

    def test_prepared_settings_must_match_catalog_context_and_compaction(self) -> None:
        name = "gpt-fixture"
        models = {name: {"context_window": 872_000, "auto_compact_window": 784_800}}
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "settings.json"
            api_profile.write_private(settings, json.dumps({"modelSettings": {
                name: {"autoCompactWindow": 784_800}}}))
            windows = json.dumps({name: 872_000})
            launcher.verify_prepared_model_settings(windows, settings, models)
            for changed in ({name: {**models[name], "context_window": 900_000}},
                            {name: {**models[name], "auto_compact_window": 700_123}},
                            {name: {"context_window": 872_000}}):
                with self.assertRaisesRegex(RuntimeError, "changed during session preparation"):
                    launcher.verify_prepared_model_settings(windows, settings, changed)
            api_profile.write_private(settings, "not-json")
            with self.assertRaisesRegex(RuntimeError, "changed during session preparation"):
                launcher.verify_prepared_model_settings(windows, settings, models)

    def test_real_model_preparation_detects_compact_metadata_drift(self) -> None:
        name = "gpt-6.1-sol"
        row = bridge_models.context_metadata({**model(272_000), "max_context_window": 872_000,
                                             "auto_compact_token_limit": None})
        chat = {name: row}
        output = StringIO()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(bridge_models, "ROOT", Path(temporary)), \
                patch.object(bridge_models, "discover", return_value=(chat, [])), \
                patch.object(sys, "argv", ["bridge_models.py", "prepare", name]), redirect_stdout(output):
            self.assertEqual(bridge_models.main(), 0)
            _, _, _, settings, windows = output.getvalue().strip().split("\t")
            launcher.verify_prepared_model_settings(windows, Path(settings), bridge_models.catalog_snapshot(chat))
            changed = {name: {**row, "auto_compact_token_limit": 700_123}}
            with self.assertRaisesRegex(RuntimeError, "changed during session preparation"):
                launcher.verify_prepared_model_settings(windows, Path(settings), bridge_models.catalog_snapshot(changed))

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

    def test_installer_checks_command_conflict_before_installation_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "existing"
            bin_dir = root / "bin"
            bin_dir.mkdir(parents=True)
            api_profile.write_private(root / install.INSTALL_MARKER, "claude-codex-http-bridge\n")
            api_profile.write_private(root / "launcher.py", "existing controller")
            command, _ = install.command_spec(root, bin_dir)
            api_profile.write_private(command, "unrelated command")
            with patch.object(sys, "argv", ["install.py", "--root", str(root), "--bin-dir", str(bin_dir)]), \
                    patch.object(install, "install_proxy") as install_proxy:
                with self.assertRaisesRegex(RuntimeError, "Existing"):
                    install.main()
            self.assertEqual((root / "launcher.py").read_text(), "existing controller")
            self.assertEqual(command.read_text(), "unrelated command")
            self.assertFalse((root / "auth").exists())
            self.assertFalse((root / "runtime-source-lock.json").exists())
            install_proxy.assert_not_called()

    def test_installer_preflight_accepts_matching_or_explicit_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "install"
            bin_dir = root / "bin"
            bin_dir.mkdir(parents=True)
            command, wrapper = install.command_spec(root, bin_dir)
            command.write_bytes(wrapper)
            self.assertTrue(install.preflight_command(root, bin_dir, False)[2])
            command.write_bytes(b"other command")
            self.assertFalse(install.preflight_command(root, bin_dir, True)[2])
            with self.assertRaisesRegex(RuntimeError, "Existing"):
                install.preflight_command(root, bin_dir, False)

    def test_installer_upgrades_only_an_exact_legacy_windows_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(install.platform, "system", return_value="Windows"):
            root = Path(directory) / "install"
            bin_dir = root / "bin"
            bin_dir.mkdir(parents=True)
            command, legacy = install.command_spec(root, bin_dir, legacy_windows=True)
            command.write_bytes(legacy)
            self.assertFalse(install.preflight_command(root, bin_dir, False)[2])
            install.install_command(root, bin_dir, False)
            self.assertEqual(command.read_bytes(), install.command_spec(root, bin_dir)[1])
            command.write_bytes(legacy + b"unexpected modification")
            with self.assertRaisesRegex(RuntimeError, "Existing"):
                install.preflight_command(root, bin_dir, False)

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
        self.assertEqual(launcher.parse_args(["mcp-audit", "--show-names"]),
                         ("mcp-audit", "", ["--show-names"]))
        self.assertEqual(launcher.parse_args(["mcp-definitions", "--session-file", "fixture"]),
                         ("mcp-definitions", "", ["--session-file", "fixture"]))
        self.assertEqual(
            launcher.parse_args(["--model", "gpt-6.1-sol", "--effort", "max"]),
            ("claude", "gpt-6.1-sol", ["--effort", "max"]),
        )
        self.assertEqual(launcher.parse_args(["models"]), ("models", "", []))
        self.assertEqual(launcher.parse_args(["usage"]), ("usage", "", []))
        self.assertEqual(launcher.parse_args(["--background"]), ("claude", "sol", ["--background"]))
        self.assertEqual(launcher.parse_args(["bridge", "stop"]), ("bridge-stop", "", []))
        self.assertEqual(launcher.parse_args(["bridge", "rollback"]), ("bridge-rollback", "", []))

    def test_mcp_audit_dispatch_does_not_start_or_verify_a_service(self) -> None:
        with patch.object(sys, "argv", ["launcher.py", "mcp-audit", "--show-names"]), \
             patch.object(launcher, "helper_output", side_effect=AssertionError("unexpected service helper")), \
             patch.object(mcp_name_audit, "main", return_value=0) as audit:
            self.assertEqual(launcher.main(), 0)
            audit.assert_called_once_with(["--show-names"])

    def test_mcp_definition_dispatch_does_not_start_or_verify_a_service(self) -> None:
        with patch.object(sys, "argv", ["launcher.py", "mcp-definitions", "--session-file", "fixture"]), \
             patch.object(launcher, "helper_output", side_effect=AssertionError("unexpected service helper")), \
             patch.object(mcp_definition_audit, "main", return_value=0) as audit:
            self.assertEqual(launcher.main(), 0)
            audit.assert_called_once_with(["--session-file", "fixture"])

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

    def test_statusline_cache_extension_is_opt_in_and_omits_assumed_ttl(self) -> None:
        payload = {"prompt_cache": {"caching_observed": True, "requests": 2, "hit_ratio": .375,
                   "ttl": "5m", "warm": True, "expires_at": 123, "misses": 3, "unknown": "private"}}
        default = statusline_adapter.corrected_payload(payload, {})
        self.assertNotIn("prompt_cache", default)
        self.assertNotIn("claude_codex_bridge", default)
        corrected = statusline_adapter.corrected_payload(payload, {}, expose_cache_metrics=True)
        self.assertNotIn("prompt_cache", corrected)
        self.assertEqual(corrected["claude_codex_bridge"]["reported_main_cache"],
                         {"caching_observed": True, "requests": 2, "hit_ratio": .375})
        self.assertIn("ttl", payload["prompt_cache"])
        for bad in ({"caching_observed": 1, "requests": True, "hit_ratio": float("nan")},
                    {"caching_observed": "true", "requests": -1, "hit_ratio": 1.1},
                    {"hit_ratio": float("inf")}, {"ttl": "5m"}):
            self.assertNotIn("claude_codex_bridge", statusline_adapter.corrected_payload(
                {"prompt_cache": bad}, {}, expose_cache_metrics=True))
        zero = statusline_adapter.corrected_payload({"prompt_cache": {"caching_observed": False,
                                                     "requests": 0, "hit_ratio": 0}}, {}, expose_cache_metrics=True)
        self.assertEqual(zero["claude_codex_bridge"]["reported_main_cache"],
                         {"caching_observed": False, "requests": 0, "hit_ratio": 0})
        existing = statusline_adapter.corrected_payload({"prompt_cache": payload["prompt_cache"],
                    "claude_codex_bridge": {"existing": True}}, {}, expose_cache_metrics=True)
        self.assertEqual(existing["claude_codex_bridge"], {"existing": True})

    def test_cache_telemetry_preference_refreshes_existing_statusline(self) -> None:
        payload = {"prompt_cache": {"caching_observed": True, "requests": 2, "hit_ratio": .375,
                                   "ttl": "5m", "warm": True}}
        with patch.dict(os.environ, {"CLAUDE_CODEX_STATUSLINE_CACHE_METRICS": ""}), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile.json"
            profile.write_text(json.dumps({"context_windows": {}, "route": "subscription"}))

            def invoke() -> dict:
                source = TextIOWrapper(BytesIO(json.dumps(payload).encode()), encoding="utf-8")
                data = BytesIO()
                target = TextIOWrapper(data, encoding="utf-8", write_through=True)
                with patch.object(statusline_adapter, "ROOT", root), patch.object(sys, "stdin", source), \
                        patch.object(sys, "stdout", target), patch.object(sys, "argv", ["adapter", str(profile)]):
                    self.assertEqual(statusline_adapter.main(), 0)
                return json.loads(data.getvalue())

            self.assertNotIn("claude_codex_bridge", invoke())
            with redirect_stdout(StringIO()):
                statusline_adapter.cache_metrics_command(root, ["telemetry", "enable"])
            self.assertEqual(invoke()["claude_codex_bridge"]["reported_main_cache"]["hit_ratio"], .375)
            self.assertFalse(statusline_adapter.cache_metrics_enabled({"route": "api"}, root))
            self.assertFalse(statusline_adapter.cache_metrics_enabled({"expose_cache_metrics": False}, root))
            with redirect_stdout(StringIO()):
                statusline_adapter.cache_metrics_command(root, ["telemetry", "disable"])
            self.assertNotIn("claude_codex_bridge", invoke())
            self.assertTrue(statusline_adapter.cache_metrics_enabled({"expose_cache_metrics": True}, root))
            pref = root / statusline_adapter.CACHE_PREFERENCE
            api_profile.write_private(pref, '{"version": true, "enabled": 1}')
            self.assertFalse(statusline_adapter.cache_metrics_preference(root))
            if os.name != "nt":
                api_profile.write_private(pref, '{"version": 1, "enabled": true}')
                pref.chmod(0o644)
                self.assertFalse(statusline_adapter.cache_metrics_preference(root))
                pref.unlink()
                target = root / "other.json"
                target.write_text('{"version": 1, "enabled": true}')
                pref.symlink_to(target)
                self.assertFalse(statusline_adapter.cache_metrics_preference(root))
                with self.assertRaises(RuntimeError):
                    statusline_adapter.cache_metrics_command(root, ["telemetry", "enable"])
                self.assertEqual(target.read_text(), '{"version": 1, "enabled": true}')
                pref.unlink()
                os.mkfifo(pref)
                self.assertFalse(statusline_adapter.cache_metrics_preference(root))

    def test_legacy_cache_environment_override_in_adapter_subprocess(self) -> None:
        payload = {"prompt_cache": {"caching_observed": True, "requests": 2, "hit_ratio": .375}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            api_profile.write_private(root / statusline_adapter.CACHE_PREFERENCE,
                                      '{"version": 1, "enabled": true}\n')
            profile = root / "profile.json"
            bootstrap = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                         "import statusline_adapter as a;a.ROOT=Path(sys.argv[2]);"
                         "sys.argv=['adapter',sys.argv[3]];raise SystemExit(a.main())")
            env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if name in os.environ}
            env["HOME"] = directory
            if os.name == "nt": env["USERPROFILE"] = directory
            source = str(Path(statusline_adapter.__file__).resolve().parent)
            for inherited, explicit, expected in ((None, None, True), ("0", None, False), ("1", None, True),
                                                  ("0", True, True), ("1", False, False), ("other", None, True)):
                with self.subTest(inherited=inherited, explicit=explicit):
                    current = {"context_windows": {}, "route": "subscription"}
                    if explicit is not None: current["expose_cache_metrics"] = explicit
                    profile.write_text(json.dumps(current))
                    local_env = dict(env)
                    if inherited is not None: local_env["CLAUDE_CODEX_STATUSLINE_CACHE_METRICS"] = inherited
                    result = subprocess.run([sys.executable, "-I", "-B", "-c", bootstrap, source, str(root), str(profile)],
                        input=json.dumps(payload), text=True, capture_output=True, timeout=10, check=True,
                        env=local_env, cwd=directory)
                    value = json.loads(result.stdout)
                    self.assertNotIn("prompt_cache", value)
                    self.assertEqual("claude_codex_bridge" in value, expected)

    def test_cache_telemetry_command_parsing(self) -> None:
        self.assertEqual(launcher.parse_args(["cache", "telemetry", "enable"]),
                         ("cache", "", ["telemetry", "enable"]))

    def test_custom_launch_settings_are_extracted_without_forwarding_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"env":{"FIXTURE_COLOR":"blue"}}', encoding="utf-8")
            settings, rest = launcher.extract_launch_settings(["--settings", "{}", "--verbose", "--settings=" + str(path),
                                                               "--effort", "high", "--", "--settings", "literal"])
            self.assertEqual(settings, {"env": {"FIXTURE_COLOR": "blue"}})
            self.assertEqual(rest, ["--verbose", "--effort", "high", "--", "--settings", "literal"])
            self.assertEqual(launcher.extract_launch_settings(["-p", "--settings", "{}", "hello"]),
                             ({}, ["-p", "hello"]))
            self.assertEqual(launcher.parse_args(["--settings", "--model", "-p", "hi"])[2],
                             ["--settings", "--model", "-p", "hi"])
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                launcher.extract_launch_settings(["--append-system-prompt", "--settings", str(path)])
            with self.assertRaisesRegex(ValueError, "Missing value"):
                launcher.extract_launch_settings(["--settings"])

    def test_custom_launch_settings_fail_closed_without_disclosing_values(self) -> None:
        cases = ['{"env":{"ANTHROPIC_BASE_URL":"fixture-private-value"}}',
                 '{"env":{"https_proxy":"fixture-private-value"}}',
                 '{"env":{"CLAUDE_CODE_AUTO_COMPACT_WINDOW":"fixture-private-value"}}',
                 '{"apiKeyHelper":"fixture-private-value"}',
                 '{"env":{"FIXTURE":1,"FIXTURE":2}}',
                 '{"env":{"FIXTURE":NaN}}', '{"fixture":1e999}', '{"env":[]}', '{"model":null}']
        for value in cases:
            with self.subTest(case=value[:35]), patch.object(launcher, "ensure_service", side_effect=AssertionError("must not start")):
                with self.assertRaises(ValueError) as failure:
                    launcher.run_session("claude", "sol", ["--settings", value], "fixture-client")
                self.assertNotIn("fixture-private-value", str(failure.exception))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = root / "large.json"
            oversized.write_bytes(b"x" * (launcher.SETTINGS_LIMIT + 1))
            with self.assertRaises(ValueError): launcher.load_launch_settings(str(oversized))
            with self.assertRaises(ValueError): launcher.load_launch_settings(str(root))
            if hasattr(os, "mkfifo"):
                pipe = root / "pipe"
                os.mkfifo(pipe)
                with self.assertRaises(ValueError): launcher.load_launch_settings(str(pipe))

    def test_stop_removes_private_composed_settings_but_not_unrelated_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ui = root / "ui-fixture"; ui.mkdir()
            composed = ui / "composed-settings.json"; composed.write_text('{"fixture":"private"}')
            unrelated = ui / "unrelated-fixture"; unrelated.write_text("keep")
            state = {"port": 12345}
            with patch.object(launcher, "service_lock", return_value=nullcontext(root)), \
                    patch.object(launcher, "read_state", return_value=state), \
                    patch.object(launcher, "state_is_live", return_value=False), \
                    patch.object(launcher, "listener_pids", return_value=set()), \
                    patch.object(launcher, "remove_state_files"), redirect_stdout(StringIO()):
                self.assertEqual(launcher.bridge_stop("api"), 0)
            self.assertFalse(composed.exists())
            self.assertTrue(unrelated.exists())

    def test_invalid_compact_window_is_rejected_before_a_session_key_is_copied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); runtime = root / "runtime"; runtime.mkdir()
            base = root / "base.json"
            base.write_text('{"modelSettings":{"fixture-model":{"autoCompactWindow":300000}}}')
            prepared = "\t".join(["fixture-model", "500000", "300000", str(base), '{"fixture-model":500000}'])
            with patch.object(launcher, "ROOT", root), \
                    patch.object(launcher, "ensure_service", return_value=(runtime, {"port": 12345}, False)), \
                    patch.object(launcher, "service_environment", return_value={}), \
                    patch.object(launcher, "helper_output", return_value=prepared):
                with self.assertRaisesRegex(ValueError, "autoCompactWindow"):
                    launcher.run_session("claude", "fixture-model", ["--settings", '{"autoCompactWindow":99999}'],
                                         "fixture-client", route="api")
            self.assertEqual(list(runtime.glob("ui-*")), [])

    def test_custom_settings_preserve_hooks_permissions_models_and_route(self) -> None:
        bridge_hook = {"matcher": "WebFetch", "hooks": [{"type": "command", "command": "bridge-hook"}]}
        user_hook = {"matcher": "*", "hooks": [{"type": "command", "command": "user-hook"}]}
        base = {"env": {"ANTHROPIC_BASE_URL": "https://127.0.0.1:1234", "ANTHROPIC_AUTH_TOKEN": "fixture-token"},
                "modelPicker": {"options": [{"model": "fixture-a"}], "replaceBuiltInOptions": True},
                "modelSettings": {"fixture-a": {"autoCompactWindow": 500000}, "fixture-b": {"autoCompactWindow": 300000}},
                "hooks": {"PostToolUse": [bridge_hook]}}
        custom = {"env": {"FIXTURE_THEME": "dark"}, "permissions": {"deny": ["Bash"]},
                  "hooks": {"PostToolUse": [user_hook], "Stop": []},
                  "modelSettings": {"fixture-a": {"effortLevel": "high", "autoCompactWindow": 250000},
                                    "future-model": {"effortLevel": "low"}},
                  "unknownFutureSetting": {"fixture": True}}
        combined = launcher.compose_launch_settings(base, custom)
        self.assertEqual(combined["env"]["ANTHROPIC_AUTH_TOKEN"], "fixture-token")
        self.assertEqual(combined["env"]["FIXTURE_THEME"], "dark")
        self.assertEqual(combined["hooks"]["PostToolUse"], [bridge_hook, user_hook])
        self.assertEqual(combined["permissions"], custom["permissions"])
        self.assertEqual(combined["modelPicker"], base["modelPicker"])
        self.assertEqual(combined["modelSettings"]["fixture-a"], {"autoCompactWindow": 250000, "effortLevel": "high"})
        self.assertEqual(combined["modelSettings"]["fixture-b"]["autoCompactWindow"], 300000)
        self.assertEqual(combined["unknownFutureSetting"], {"fixture": True})
        self.assertEqual(base["modelSettings"]["fixture-a"]["autoCompactWindow"], 500000)
        custom_picker = {"options": [{"model": "fixture-b", "label": "My choice"}], "replaceBuiltInOptions": True}
        self.assertEqual(launcher.compose_launch_settings(base, {"modelPicker": custom_picker})["modelPicker"], custom_picker)
        global_window = launcher.compose_launch_settings(base, {"autoCompactWindow": 200000})
        self.assertEqual(global_window["modelSettings"]["fixture-a"]["autoCompactWindow"], 200000)
        self.assertEqual(global_window["modelSettings"]["fixture-b"]["autoCompactWindow"], 200000)
        auto = launcher.compose_launch_settings(base, {"modelSettings": {"fixture-a": {"autoCompactWindow": "auto"}}})
        self.assertEqual(auto["modelSettings"]["fixture-a"]["autoCompactWindow"], 500000)
        for override in ({"autoCompactWindow": 400000}, {"autoCompactWindow": "auto"},
                         {"autoCompactWindow": 99999},
                         {"modelSettings": {"fixture-a": {"autoCompactWindow": 500001}}},
                         {"modelSettings": {"future-model": {"autoCompactWindow": 100000}}},
                         {"modelSettings": {"future-model": {"autoCompactWindow": "auto"}}},
                         {"hooks": {"PostToolUse": None}}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                launcher.compose_launch_settings(base, override)

    def test_explicit_model_flag_outranks_custom_settings_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(launcher, "ROOT", root), \
                    patch.object(launcher, "ensure_service", return_value=(root, {"port": 12345}, False)), \
                    patch.object(launcher, "service_environment", return_value={}), \
                    patch.object(launcher, "helper_output", side_effect=RuntimeError("synthetic stop")) as helper:
                with self.assertRaisesRegex(RuntimeError, "synthetic stop"):
                    launcher.run_session("claude", "explicit-model", ["--settings", '{"model":"settings-model"}'],
                                         "fixture-client", route="api", explicit_model=True)
            self.assertEqual(helper.call_args.args, ("prepare", "explicit-model"))

    def test_custom_statusline_has_launch_precedence_and_keeps_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); user = root / "user"; user.mkdir()
            (user / "settings.json").write_text('{"statusLine":{"type":"command","command":"echo global"}}')
            base = root / "base.json"
            base.write_text('{"statusLine":{"type":"command","command":"echo launch","refreshInterval":17}}')
            session = root / "session"; session.mkdir()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(user)}, clear=True):
                result, _ = launcher.session_settings(base, {"fixture": 500000}, session)
            status = json.loads(result.read_text())["statusLine"]
            self.assertIn(" | echo launch", status["command"])
            self.assertNotIn("echo global", status["command"])
            self.assertEqual(status["refreshInterval"], 17)
            base.write_text('{"statusLine":null}')
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(user)}, clear=True):
                result, files = launcher.session_settings(base, {}, root / "unused")
            self.assertEqual(result, base)
            self.assertEqual(files, [])

    def test_statusline_does_not_promote_a_disabled_user_scope(self) -> None:
        cases = [([], True), (["--restricted"], False), (["--bare"], False), (["--safe-mode"], False),
                 (["--setting-sources", "project,local"], False), (["--setting-sources=project,user"], True),
                 (["--setting-sources=project", "--setting-sources", "user"], True),
                 (["--setting-sources=user", "--setting-sources", "local"], False),
                 (["--setting-sources"], False), (["--", "--restricted"], True)]
        with patch.dict(os.environ, {}, clear=True):
            for arguments, expected in cases:
                with self.subTest(arguments=arguments):
                    self.assertIs(launcher.user_status_line_allowed(arguments), expected)
        for name in ("CLAUDE_CODE_RESTRICTED", "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE"):
            for value in ("1", "true", "YES", "On"):
                with self.subTest(variable=name, value=value), patch.dict(os.environ, {name: value}, clear=True):
                    self.assertFalse(launcher.user_status_line_allowed([]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); user = root / "user"; user.mkdir()
            (user / "settings.json").write_text('{"statusLine":{"type":"command","command":"echo global"}}')
            base = root / "base.json"; base.write_text('{}')
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(user)}, clear=True):
                result, files = launcher.session_settings(base, {}, root / "unused", include_user_status_line=False)
                self.assertEqual((result, files), (base, []))
                base.write_text('{"statusLine":{"type":"command","command":"echo explicit"}}')
                session = root / "session"; session.mkdir()
                result, _ = launcher.session_settings(base, {}, session, include_user_status_line=False)
            self.assertIn(" | echo explicit", json.loads(result.read_text())["statusLine"]["command"])

    def test_project_and_worktree_statusline_precedence_is_detected_privately(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory); main = root / "main"; main.mkdir()
            os.environ["HOME"] = str(root)
            if os.name == "nt": os.environ["USERPROFILE"] = str(root)
            git = main / ".git"; git.mkdir(); nested = main / "nested"; nested.mkdir()
            project = nested / ".claude"; project.mkdir()
            shared = project / "settings.json"
            shared.write_text('{"permissions":{"deny":["Bash"]}}')
            self.assertFalse(launcher.project_status_line_may_override([], nested))
            shared.write_text('{"statusLine":{"type":"command","command":"fixture"}}')
            self.assertTrue(launcher.project_status_line_may_override([], nested))
            self.assertFalse(launcher.project_status_line_may_override(["--setting-sources", "user"], nested))
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(project)}, clear=True):
                self.assertFalse(launcher.project_status_line_may_override([], nested))
            shared.write_text('{}')
            local = main / ".claude"; local.mkdir()
            (local / "settings.local.json").write_text('{"statusLine":{}}')
            self.assertEqual(launcher.project_status_line_may_override([], nested), os.name != "nt")
            self.assertFalse(launcher.project_status_line_may_override(["--setting-sources", "user,project"], nested))
            nested_local = project / "settings.local.json"
            nested_local.write_text('{"statusLine":{}}')
            self.assertTrue(launcher.project_status_line_may_override([], nested))
            nested_local.unlink()
            worktree = root / "worktree"; worktree.mkdir()
            gitdir = git / "worktrees" / "fixture"; gitdir.mkdir(parents=True)
            (gitdir / "commondir").write_text('../..')
            (worktree / ".git").write_text('gitdir: ' + str(gitdir))
            self.assertEqual(launcher.project_status_line_may_override([], worktree), os.name != "nt")
            (local / "settings.local.json").unlink()
            self.assertFalse(launcher.project_status_line_may_override([], worktree))
            shared.write_text('{broken')
            self.assertTrue(launcher.project_status_line_may_override([], nested))
            shared.write_bytes(b'x' * (launcher.SETTINGS_LIMIT + 1))
            self.assertTrue(launcher.project_status_line_may_override([], nested))
            if hasattr(os, "mkfifo"):
                shared.unlink(); os.mkfifo(shared)
                self.assertTrue(launcher.project_status_line_may_override([], nested))
            with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", side_effect=RuntimeError("fixture")):
                self.assertTrue(launcher.project_status_line_may_override([], nested))
                self.assertIsNone(launcher.configured_status_line())

    def test_launcher_passes_statusline_scope_to_session_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); runtime = root / "runtime"; runtime.mkdir()
            base = root / "base.json"; base.write_text('{}')
            prepared = "\t".join(["fixture-model", "500000", "300000", str(base), '{}'])
            environment = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "TEMP", "TMP")
                           if name in os.environ}
            for arguments, expected in ((["--restricted"], False), (["--setting-sources", "project"], False),
                                        (["--", "--restricted"], True), ([], True)):
                with self.subTest(arguments=arguments), patch.dict(os.environ, environment, clear=True), \
                        patch.object(launcher, "ROOT", root), \
                        patch.object(launcher, "ensure_service", return_value=(runtime, {"port": 12345}, False)), \
                        patch.object(launcher, "service_environment", return_value={}), \
                        patch.object(launcher, "helper_output", return_value=prepared), \
                        patch.object(launcher, "project_status_line_may_override", return_value=False), \
                        patch.object(launcher, "session_settings", side_effect=RuntimeError("fixture stop")) as session:
                    with self.assertRaisesRegex(RuntimeError, "fixture stop"):
                        launcher.run_session("claude", "fixture-model", arguments, "fixture-client", route="api")
                    self.assertIs(session.call_args.kwargs["include_user_status_line"], expected)
            import rolling_runtime
            (runtime / "session-fixture").mkdir()
            (runtime / "session-fixture" / "key").write_text("fixture-local-key")
            state = {"port": 12345, "session": "session-fixture"}
            base.write_text('{"modelSettings":{"fixture-model":{"autoCompactWindow":300000}}}')
            with patch.dict(os.environ, environment, clear=True), patch.object(launcher, "ROOT", root), \
                    patch.object(launcher, "ensure_service", return_value=(runtime, state, False)), \
                    patch.object(launcher, "service_environment", return_value={}), \
                    patch.object(launcher, "helper_output", return_value=prepared), \
                    patch.object(rolling_runtime, "client_settings", return_value=base), \
                    patch.object(launcher, "session_settings", side_effect=RuntimeError("fixture stop")) as session:
                with self.assertRaisesRegex(RuntimeError, "fixture stop"):
                    launcher.run_session("claude", "fixture-model", ["--settings", "{}", "--restricted"],
                                         "fixture-client", route="api")
                self.assertIs(session.call_args.kwargs["include_user_status_line"], False)

            with patch.dict(os.environ, environment, clear=True), patch.object(launcher, "ROOT", root), \
                    patch.object(launcher, "ensure_service", return_value=(runtime, state, False)), \
                    patch.object(launcher, "service_environment", return_value={}), \
                    patch.object(launcher, "helper_output", return_value=prepared), \
                    patch.object(launcher, "project_status_line_may_override", return_value=True), \
                    patch.object(launcher, "configured_status_line", return_value={"type":"command", "command":"fixture"}), \
                    patch.object(launcher, "session_settings", side_effect=RuntimeError("fixture stop")) as session, \
                    redirect_stderr(StringIO()) as error:
                with self.assertRaisesRegex(RuntimeError, "fixture stop"):
                    launcher.run_session("claude", "fixture-model", [], "fixture-client", route="api")
                self.assertIs(session.call_args.kwargs["include_user_status_line"], False)
                self.assertIn("context, cost, quota, or cache corrections", error.getvalue())

    def test_statusline_preserves_unknown_context_after_compaction(self) -> None:
        name = "gpt-6-astra"
        for usage in (None, {}, {"input_tokens": 100},
                      {"input_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": -1},
                      {"input_tokens": True, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}):
            with self.subTest(usage=usage):
                payload = {"model": {"id": name}, "context_window": {"context_window_size": 1_000_000,
                           "used_percentage": 50, "remaining_percentage": 50, "current_usage": usage}}
                result = statusline_adapter.corrected_payload(payload, {name: 872_000})
                self.assertEqual(result["context_window"]["context_window_size"], 872_000)
                self.assertIsNone(result["context_window"]["used_percentage"])
                self.assertIsNone(result["context_window"]["remaining_percentage"])
                self.assertEqual(payload["context_window"]["used_percentage"], 50)
        values = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        payload = {"model": {"id": name}, "context_window": {"current_usage": values}}
        known_zero = statusline_adapter.corrected_payload(payload, {name: 872_000})
        self.assertEqual(known_zero["context_window"]["used_percentage"], 0)
        self.assertEqual(known_zero["context_window"]["remaining_percentage"], 100)
        values["cache_read_input_tokens"] = 10**400
        huge = statusline_adapter.corrected_payload(payload, {name: 872_000})
        self.assertEqual(huge["context_window"]["used_percentage"], 100)

    def test_statusline_cache_extension_flows_through_the_real_adapter_process(self) -> None:
        payload = {"prompt_cache": {"caching_observed": True, "requests": 2, "hit_ratio": .375,
                   "ttl": "5m", "warm": True, "expires_at": 123}}
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            environment = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if name in os.environ}
            environment["HOME"] = directory
            if os.name == "nt": environment["USERPROFILE"] = directory
            for opted in (False, True):
                profile.write_text(json.dumps({"context_windows": {}, "expose_cache_metrics": opted}))
                result = subprocess.run([sys.executable, str(Path(statusline_adapter.__file__)), str(profile)],
                                        input=json.dumps(payload), text=True, capture_output=True, timeout=10, check=True,
                                        env=environment, cwd=directory)
                result = json.loads(result.stdout)
                self.assertNotIn("prompt_cache", result)
                if opted:
                    self.assertEqual(result["claude_codex_bridge"]["reported_main_cache"],
                                     {"caching_observed": True, "requests": 2, "hit_ratio": .375})
                else:
                    self.assertNotIn("claude_codex_bridge", result)

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
                self.assertIsNone(codex_usage.matching_account_id(root))
                bridge_file.write_text(json.dumps({"account_id": "account-b", "email": "fixture@example.invalid"}))
                native_file.write_text(json.dumps({"tokens": {"account_id": "account-b", "email": "fixture@example.invalid"}}))
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
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir),
                                        "CLAUDE_CODEX_STATUSLINE_CACHE_METRICS": "0"}, clear=False):
                settings_path, files = launcher.session_settings(base, {"gpt-6.1-sol": 500_000}, session_dir)
            result = json.loads(settings_path.read_text())
            self.assertIn("statusline_adapter.py", result["statusLine"]["command"])
            self.assertIn(" | echo HUD", result["statusLine"]["command"])
            self.assertEqual(result["statusLine"]["refreshInterval"], 30)
            self.assertEqual(json.loads(files[0].read_text())["context_windows"], {"gpt-6.1-sol": 500_000})
            self.assertEqual(json.loads(files[0].read_text())["usage_cache"], str(root / "codex-usage.json"))
            self.assertEqual(json.loads(user_settings.read_text())["statusLine"]["command"], "echo HUD")
            self.assertIs(json.loads(files[0].read_text())["expose_cache_metrics"], False)
            default_dir = root / "default-session"
            default_dir.mkdir()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir)}, clear=True):
                _, default_files = launcher.session_settings(base, {"gpt-6.1-sol": 500_000}, default_dir)
            self.assertNotIn("expose_cache_metrics", json.loads(default_files[0].read_text()))
            opted_dir = root / "opted-session"
            opted_dir.mkdir()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir),
                                        "CLAUDE_CODEX_STATUSLINE_CACHE_METRICS": "1"}, clear=False):
                _, opted_files = launcher.session_settings(base, {"gpt-6.1-sol": 500_000}, opted_dir)
            self.assertIs(json.loads(opted_files[0].read_text())["expose_cache_metrics"], True)
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
