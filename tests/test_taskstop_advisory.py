"""The experimental stop advisory never forwards private hook inputs."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import taskstop_advisory as advisory


class TaskStopAdvisoryTests(unittest.TestCase):
    def invoke(self, raw: bytes) -> dict:
        with tempfile.TemporaryDirectory(prefix="taskstop-advisory-") as temporary:
            env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if name in os.environ}
            env["HOME"] = temporary
            if os.name == "nt": env["USERPROFILE"] = temporary
            result = subprocess.run([sys.executable, "-I", "-B", str(Path(advisory.__file__))], input=raw,
                                    capture_output=True, timeout=10, check=True, cwd=temporary, env=env)
            self.assertEqual(result.stderr, b"")
            self.assertNotIn(b"PRIVATE", result.stdout)
            return json.loads(result.stdout)

    def test_only_ownership_shaped_task_stop_failure_adds_fixed_context(self):
        event = {"hook_event_name": "PostToolUseFailure", "tool_name": "TaskStop", "is_interrupt": False,
                 "tool_input": {"task_id": "PRIVATE_TASK"}, "transcript_path": "/PRIVATE_PATH",
                 "error": "Task PRIVATE_TASK is owned by PRIVATE_OWNER; agent PRIVATE_AGENT cannot stop it. PRIVATE_SECRET"}
        output = self.invoke(json.dumps(event).encode())
        self.assertEqual(output, {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": advisory.ADVISORY}})
        for changes in ({"tool_name": "Read"}, {"hook_event_name": "PostToolUse"}, {"is_interrupt": True}, {"is_interrupt": "unknown"},
                        {"error": "Task not found"}, {"error": None}):
            self.assertEqual(self.invoke(json.dumps({**event, **changes}).encode()), {})

    def test_malformed_or_oversized_input_is_ignored(self):
        self.assertEqual(self.invoke(b"{PRIVATE"), {})
        self.assertEqual(self.invoke(b"\xffPRIVATE"), {})
        self.assertEqual(self.invoke(b"x" * (advisory.MAX_INPUT_BYTES + 1)), {})


if __name__ == "__main__":
    unittest.main()
