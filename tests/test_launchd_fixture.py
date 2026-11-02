"""The opt-in launchd experiment must target only its own fake installation."""

import plistlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_launchd_fixture import PREFIX, definition  # noqa: E402


class LaunchdFixtureDefinitionTests(unittest.TestCase):
    def test_definition_uses_only_the_fixture_script_and_home(self):
        with tempfile.TemporaryDirectory(prefix="bridge-fixture-space ") as temporary:
            root = Path(temporary).resolve()
            value = plistlib.loads(definition(root, sys.executable, PREFIX + "a" * 32))
            self.assertEqual(value["Label"], PREFIX + "a" * 32)
            self.assertEqual(value["ProgramArguments"][3:], [str(root / "autostart.py"), "run", "--root", str(root)])
            self.assertEqual(value["EnvironmentVariables"]["HOME"], str(root / "home"))
            self.assertEqual(value["EnvironmentVariables"]["HTTP_PROXY"], "http://127.0.0.1:9")
            self.assertEqual(value["StartInterval"], 10)
            self.assertTrue(value["RunAtLoad"])

    def test_nonfixture_or_malformed_labels_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for label in ("org.claude-codex-http-bridge.live", PREFIX + "a" * 31,
                          PREFIX + "a" * 31 + "/", PREFIX + "A" * 32):
                with self.subTest(label=label), self.assertRaises(ValueError):
                    definition(root, sys.executable, label)


if __name__ == "__main__":
    unittest.main()
