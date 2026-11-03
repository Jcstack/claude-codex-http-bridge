"""Exercise model-helper launch isolation without real accounts or network."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import launcher


class ModelHelperEnvironmentTest(unittest.TestCase):
    def test_helper_ignores_python_customization_and_unrelated_credentials(self):
        with tempfile.TemporaryDirectory(prefix="catalog-helper-fixture-") as temporary:
            root = Path(temporary)
            custom = root / "custom"
            custom.mkdir()
            marker = root / "customization-ran"
            (custom / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
            (root / "fixture_sibling.py").write_text("VALUE = 'trusted-sibling'\n", encoding="utf-8")
            helper = root / "bridge_models.py"
            helper.write_text('''import json,os,sys
import fixture_sibling
print(json.dumps({"sibling":fixture_sibling.VALUE,
    "ignore_python_env":bool(sys.flags.ignore_environment),"no_site":bool(sys.flags.no_site),
    "has_unrelated_secret":any(name in os.environ for name in
        ("OPENAI_API_KEY","ANTHROPIC_AUTH_TOKEN","AWS_SECRET_ACCESS_KEY","PYTHONPATH")),
    "port":os.environ.get("CODEX_BRIDGE_PORT"),"key_file":os.environ.get("CODEX_BRIDGE_KEY_FILE"),
    "route":os.environ.get("CODEX_BRIDGE_ROUTE"),"metadata":os.environ.get("CODEX_BRIDGE_API_METADATA_FILE"),
    "pdf_guard":os.environ.get("CLAUDE_CODEX_PDF_GUARD"),"operation":sys.argv[1]}))
''', encoding="utf-8")
            environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
            environment.update({"PYTHONPATH": str(custom), "OPENAI_API_KEY": "SYNTHETIC_OPENAI",
                "ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_ANTHROPIC", "AWS_SECRET_ACCESS_KEY": "SYNTHETIC_AWS",
                "CODEX_BRIDGE_PORT": "12345", "CODEX_BRIDGE_KEY_FILE": "fixture-key-path",
                "CODEX_BRIDGE_ROUTE": "api", "CODEX_BRIDGE_API_METADATA_FILE": "fixture-metadata-path",
                "CLAUDE_CODEX_PDF_GUARD": "0"})
            # Control: the previous child launch inherits the synthetic secret
            # and runs sitecustomize from an unrelated PYTHONPATH directory.
            baseline = subprocess.run([sys.executable, str(helper), "catalog"], env=environment,
                                      capture_output=True, text=True, timeout=15, check=True)
            self.assertTrue(marker.is_file())
            self.assertTrue(json.loads(baseline.stdout)["has_unrelated_secret"])
            marker.unlink()
            with patch.object(launcher, "HELPER", helper):
                result = json.loads(launcher.helper_output("catalog", environment=environment))
            self.assertFalse(marker.exists())
            self.assertEqual(result, {"sibling": "trusted-sibling", "ignore_python_env": True, "no_site": True,
                "has_unrelated_secret": False, "port": "12345", "key_file": "fixture-key-path", "route": "api",
                "metadata": "fixture-metadata-path", "pdf_guard": "0", "operation": "catalog"})

    def test_default_environment_is_also_filtered(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SYNTHETIC_OPENAI", "PYTHONPATH": "synthetic-import",
                                     "CODEX_BRIDGE_PORT": "23456", "CLAUDE_CODEX_PDF_GUARD": "0"}):
            environment = launcher.model_helper_environment()
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(environment["CODEX_BRIDGE_PORT"], "23456")
        self.assertEqual(environment["CLAUDE_CODEX_PDF_GUARD"], "0")

    def test_filter_does_not_strip_plugin_environment_from_claude(self):
        supplied = {"CLAUDE_PLUGIN_FIXTURE": "enabled", "PYTHONPATH": "synthetic-plugin-path"}
        with patch.dict(os.environ, supplied):
            helper = launcher.model_helper_environment()
            harness = launcher.claude_environment(12345, "synthetic-key", "gpt-fixture")
        for name, value in supplied.items():
            self.assertNotIn(name, helper)
            self.assertEqual(harness[name], value)

    def test_real_helper_discovers_a_synthetic_catalog_with_unicode_paths(self):
        slug = "gpt-9.2-sol"
        row = {"slug": slug, "context_window": 272_000, "max_context_window": 872_000,
               "input_modalities": ["text", "image"], "supports_search_tool": True,
               "supported_reasoning_levels": [{"effort": value} for value in
                                               ("low", "medium", "high", "xhigh", "max")]}

        class Catalog(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.paths.append(self.path)
                if self.headers.get("Authorization") != "Bearer fixture-local-key":
                    self.send_error(401)
                    return
                value = ({"data": [{"id": slug}]} if self.path == "/v1/models" else
                         {"models": [row]} if self.path == "/v1/models?client_version=cpa" else None)
                if value is None:
                    self.send_error(404)
                    return
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Catalog)
        server.paths = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="catalog-helper-fixture-") as temporary:
                root = Path(temporary) / "模型-fixture"
                root.mkdir()
                source = Path(launcher.__file__).resolve().parent
                for name in ("bridge_models.py", "windows_acl.py"):
                    shutil.copyfile(source / name, root / name)
                key = root / "synthetic-key"
                key.write_text("fixture-local-key\n", encoding="utf-8")
                environment = {**os.environ, "OPENAI_API_KEY": "SYNTHETIC_OPENAI",
                    "CODEX_BRIDGE_PORT": str(server.server_port), "CODEX_BRIDGE_KEY_FILE": str(key),
                    "CLAUDE_CODEX_PDF_GUARD": "0"}
                environment.pop("CODEX_BRIDGE_ROUTE", None)
                environment.pop("CODEX_BRIDGE_API_METADATA_FILE", None)
                with patch.object(launcher, "HELPER", root / "bridge_models.py"):
                    counts = json.loads(launcher.helper_output("catalog", environment=environment))
                    snapshot = json.loads(launcher.helper_output("snapshot", environment=environment))
                    selection = launcher.helper_output("prepare", "sol", environment=environment).split("\t")
                self.assertEqual(counts, {"chat": 1, "eligible": 1})
                self.assertEqual(snapshot[slug]["context_window"], 872_000)
                self.assertEqual(snapshot[slug]["auto_compact_window"], 784_800)
                self.assertEqual(selection[:3], [slug, "872000", "784800"])
                settings = json.loads(Path(selection[3]).read_text(encoding="utf-8"))
                self.assertEqual(settings["modelSettings"][slug]["autoCompactWindow"], 784_800)
                self.assertNotIn("hooks", settings)
                self.assertEqual(json.loads(selection[4]), {slug: 872_000})
                metadata = root / "synthetic-api-metadata.json"
                api_spec = {name: value for name, value in row.items() if name != "max_context_window"}
                metadata.write_text(json.dumps({slug: {**api_spec, "context_window": 333_333}}), encoding="utf-8")
                metadata.chmod(0o600)
                if os.name == "nt":
                    import windows_acl
                    windows_acl.lock([metadata])
                environment.update(CODEX_BRIDGE_ROUTE="api", CODEX_BRIDGE_API_METADATA_FILE=str(metadata))
                with patch.object(launcher, "HELPER", root / "bridge_models.py"):
                    api_snapshot = json.loads(launcher.helper_output("snapshot", environment=environment))
                self.assertEqual(api_snapshot[slug]["context_window"], 333_333)
                self.assertEqual(len(server.paths), 8)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
