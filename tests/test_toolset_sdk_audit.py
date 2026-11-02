"""Negative controls for the static public-SDK inventory, with no SDK import."""

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.audit_toolset_sdk import audit


SDK = os.environ.get("BRIDGE_TOOLSET_SDK_CHECKOUT")


@unittest.skipUnless(SDK, "A separately verified public SDK checkout is required")
class StaticInventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="bridge-sdk-inventory-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        source = Path(SDK) / "src" / "anthropic"
        beta = cls.root / "src" / "anthropic" / "types" / "beta"
        beta.mkdir(parents=True)
        for family in ("browser", "computer"):
            for pattern in (f"beta_{family}_*tool_use_block.py", f"beta_{family}_*input.py",
                            f"beta_{family}_member_name.py"):
                for path in (source / "types" / "beta").glob(pattern):
                    shutil.copyfile(path, beta / path.name)
        metadata = cls.root / "src" / "anthropic" / "_data"
        metadata.mkdir()
        shutil.copyfile(source / "_data" / "browser_toolset_members.py", metadata / "browser_toolset_members.py")
        cls.beta = beta

    def replace(self, path, before, after):
        original = path.read_text(encoding="utf-8")
        self.assertEqual(original.count(before), 1)
        self.addCleanup(path.write_text, original, encoding="utf-8")
        path.write_text(original.replace(before, after), encoding="utf-8")

    def test_snapshot_counts_and_collision(self):
        result = audit(self.root)
        self.assertEqual((result["browser_members"], result["computer_members"]), (31, 17))
        self.assertEqual(len(result["shared_names_requiring_family_identity"]), 16)
        self.assertIn("scroll", result["shared_names_requiring_family_identity"])
        self.assertFalse(result["source_integrity_verified_by_fixture"])
        self.assertFalse(result["schemas_or_executor_permissions_verified"])

    def test_source_is_parsed_without_execution(self):
        path = self.beta / "beta_browser_navigate_input.py"
        self.replace(path, "from typing import Optional", "raise RuntimeError('executed')\nfrom typing import Optional")
        self.assertFalse(audit(self.root)["source_imported_or_executed"])

    def test_wrong_family_is_rejected(self):
        path = self.beta / "beta_browser_scroll_tool_use_block.py"
        self.replace(path, 'toolset_name: Literal["browser"]', 'toolset_name: Literal["computer"]')
        with self.assertRaises(ValueError):
            audit(self.root)

    def test_duplicate_member_is_rejected(self):
        path = self.beta / "beta_browser_scroll_tool_use_block.py"
        self.replace(path, 'name: Literal["scroll"]', 'name: Literal["wait"]')
        with self.assertRaises(ValueError):
            audit(self.root)

    def test_changed_defaults_are_rejected(self):
        path = self.root / "src" / "anthropic" / "_data" / "browser_toolset_members.py"
        self.replace(path, '"file_upload": {\n        "result": "none",\n        "confirmation": "Uploaded.",\n        "enabled_by_default": False,',
                     '"file_upload": {\n        "result": "none",\n        "confirmation": "Uploaded.",\n        "enabled_by_default": True,')
        with self.assertRaises(ValueError):
            audit(self.root)

    def test_missing_input_model_is_rejected(self):
        path = self.beta / "beta_browser_navigate_input.py"
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            audit(self.root)


if __name__ == "__main__":
    unittest.main()
