"""Security and model-selection checks with no live credential or network use."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bridge_models
import install


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


if __name__ == "__main__":
    unittest.main()
