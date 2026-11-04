"""Exercise the long-context benchmark without any model account or network."""

from contextlib import contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_compaction


class LiveCompactionFixtureTests(unittest.TestCase):
    def test_large_synthetic_fixture_uses_discovered_window_and_timeout(self):
        model = "gpt-6-astra"
        expected = {**live_compaction.INITIAL, **live_compaction.UPDATES}

        @contextmanager
        def fake_translator(*_args, **_kwargs):
            yield 12345, "synthetic-key"

        for native, context, expected_window in ((False, 500000, 450000), (True, 872000, 784800)):
            with self.subTest(native=native, context=context):
                catalog = {model: {"context_window": context, "supports_search_tool": True,
                                   "supported_reasoning_levels": [{"effort": effort} for effort in
                                                                  ("low", "medium", "high", "xhigh", "max")]}}
                calls = []

                def fake_run(command, *, input, cwd, env, capture_output, text, timeout):
                    self.assertEqual(timeout, 1800)
                    self.assertTrue(capture_output and text)
                    self.assertEqual(env.get("ANTHROPIC_CUSTOM_HEADERS") ==
                                     "X-Codex-Bridge-Native-Compaction: 1", native)
                    settings = json.loads(Path(command[command.index("--settings") + 1]).read_text())
                    self.assertEqual(settings["modelSettings"][model]["autoCompactWindow"], expected_window)
                    self.assertEqual(settings["modelPicker"]["options"][0]["behavesAs"], "claude-opus-5-5")
                    self.assertEqual(command[command.index("--effort") + 1], "xhigh")
                    calls.append(input)
                    sid_flag = "--session-id" if "--session-id" in command else "--resume"
                    sid = command[command.index(sid_flag) + 1]
                    if input.startswith("/compact"):
                        transcript = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture" / (sid + ".jsonl")
                        transcript.parent.mkdir(parents=True, exist_ok=True)
                        record = {"subtype": "compact_boundary", "compactMetadata": {"preTokens": 700000}}
                        if native:
                            record["synthetic_marker"] = "codex-native-context-v1:"
                        transcript.write_text(json.dumps(record) + "\n")
                    result = json.dumps(expected) if input.startswith("Return only a JSON") else "READY"
                    return type("Result", (), {"returncode": 0, "stdout": json.dumps({"result": result})})()

                arguments = ["live_compaction.py", "/fake/proxy", "--root", "/fake/root",
                             "--model", "astra", "--efforts", "xhigh", "--samples", "35000",
                             "--stage-timeout", "1800"] + (["--native"] if native else [])
                with patch.object(sys, "argv", arguments), \
                     patch.object(live_compaction, "existing_subscription", return_value={"models": catalog}), \
                     patch.object(live_compaction, "temporary_translator", fake_translator), \
                     patch.object(live_compaction.shutil, "which", return_value="/fake/claude"), \
                     patch.object(live_compaction.subprocess, "run", side_effect=fake_run), \
                     redirect_stdout(io.StringIO()):
                    live_compaction.main()
                self.assertEqual(len(calls), 4)
                self.assertIn("Historical sample 34999:", calls[0])


if __name__ == "__main__":
    unittest.main()
