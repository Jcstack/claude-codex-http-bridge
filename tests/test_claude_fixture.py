"""Privacy boundary for credential-free Claude client fixtures."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from claude_fixture import isolated_environment


class ClaudeFixtureEnvironmentTest(unittest.TestCase):
    def test_home_and_secrets_do_not_come_from_the_caller(self):
        private_value = "PRIVATE_FIXTURE_SENTINEL"
        parent = {name: private_value for name in
                  ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
        with tempfile.TemporaryDirectory(prefix="bridge-fixture-home-test-") as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, parent):
                environment = isolated_environment(root)
            fixture_home = root / "fixture-home"
            self.assertEqual(environment["HOME"], str(fixture_home))
            self.assertTrue(fixture_home.is_dir())
            self.assertNotIn("ANTHROPIC_API_KEY", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
                if name in environment:
                    self.assertTrue(Path(environment[name]).is_relative_to(fixture_home))
                self.assertNotEqual(environment.get(name), private_value)


if __name__ == "__main__":
    unittest.main()
