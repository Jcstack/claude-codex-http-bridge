"""Resume the parent model without storing prompts, credentials or other projects."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import uuid
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace

import session_models
import launcher


class SessionModelTests(unittest.TestCase):
    def fixture(self, root, config, cwd, model, *, session=None, title=None, sidechain=None):
        session = session or str(uuid.uuid4())
        project = config / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))
        project.mkdir(parents=True, exist_ok=True)
        path = project / (session + ".jsonl")
        rows = [{"type": "assistant", "sessionId": session, "cwd": str(cwd),
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "message": {"model": model, "content": [{"type": "text", "text": "PRIVATE_PROMPT_AND_TOKEN"}]}}]
        if title:
            rows.append({"type": "custom-title", "customTitle": title, "sessionId": session})
        if sidechain:
            rows.append({"type": "assistant", "isSidechain": True, "message": {"model": sidechain}})
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return session, path

    def test_uuid_name_continue_and_parent_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); config = root / "claude"; cwd = root / "project"; cwd.mkdir()
            sid, _ = self.fixture(root, config, cwd, "gpt-6.1-terra", title="fixture", sidechain="gpt-6-luna")
            self.fixture(root, config, root / "another-project", "gpt-6-astra")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                for arguments in (["--resume", sid], ["--resume=" + sid], ["-r", sid],
                                  ["--continue"], ["-c"], ["--resume", "fixture"]):
                    self.assertEqual(session_models.restore(root, arguments, cwd, "subscription"),
                                     (True, "gpt-6.1-terra"))

    def test_later_hud_choice_wins_without_storing_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); config = root / "claude"; cwd = root / "project"; cwd.mkdir()
            sid, _ = self.fixture(root, config, cwd, "gpt-6.1-terra")
            payload = {"session_id": sid, "cwd": str(cwd), "model": {"id": "gpt-6-astra"},
                       "api_key": "PRIVATE_PROMPT_AND_TOKEN", "last_message": "PRIVATE_PROMPT_AND_TOKEN"}
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                session_models.record(root, payload, {"gpt-6-astra": 872000}, "subscription")
                self.assertEqual(session_models.restore(root, ["--resume", sid], cwd, "subscription"),
                                 (True, "gpt-6-astra"))
                self.assertEqual(session_models.restore(root, ["--resume", sid], cwd, "api"),
                                 (True, "gpt-6.1-terra"))
                path = session_models.ledger_directory(root, config, "subscription") / (sid + ".json")
                content = path.read_text()
                self.assertNotIn("PRIVATE_PROMPT_AND_TOKEN", content)
                self.assertNotIn(str(cwd), content)
                before = path.stat().st_mtime_ns
                session_models.record(root, payload, {"gpt-6-astra": 872000}, "subscription")
                self.assertEqual(path.stat().st_mtime_ns, before)

    def test_new_print_mode_response_supersedes_old_hud_choice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); config = root / "claude"; cwd = root / "project"; cwd.mkdir()
            sid, path = self.fixture(root, config, cwd, "gpt-6.1-terra")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                session_models.record(root, {"session_id": sid, "cwd": str(cwd), "model": {"id": "gpt-6-astra"}},
                                      {"gpt-6-astra": 872000}, "subscription")
                ledger = session_models.ledger_directory(root, config, "subscription") / (sid + ".json")
                os.utime(ledger, (1, 1))
                self.assertEqual(session_models.restore(root, ["--resume", sid], cwd, "subscription"),
                                 (True, "gpt-6.1-terra"))

    def test_picker_and_ambiguous_names_do_not_guess(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); config = root / "claude"; cwd = root / "project"; cwd.mkdir()
            self.fixture(root, config, cwd, "gpt-6.1-terra", title="same")
            self.fixture(root, config, cwd, "gpt-6-astra", title="same")
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
                for args in (["--resume"], ["--resume", "same"], ["--resume", "missing"]):
                    self.assertEqual(session_models.restore(root, args, cwd, "subscription"), (True, None))
            self.assertEqual(session_models.resume_selector(["--", "--resume"]), (None, None))

    def test_oversized_and_malformed_records_do_not_hide_later_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); config = root / "claude"; cwd = root / "project"; cwd.mkdir()
            sid, path = self.fixture(root, config, cwd, "gpt-6.1-terra")
            with path.open("ab") as output:
                output.write(b'x' * (session_models.TAIL_BYTES + 10) + b'\nnot json\n')
                output.write(json.dumps({"type": "assistant", "message": {"model": "gpt-6-astra"}}).encode() + b'\n')
            self.assertEqual(session_models.transcript_metadata(path)["model"], "gpt-6-astra")

    def test_launcher_restores_model_and_respects_explicit_override_and_picker(self):
        for explicit, remembered, expected in ((False, "gpt-6.1-terra", "gpt-6.1-terra"),
                                                (True, "gpt-6.1-terra", "gpt-6.1-sol"),
                                                (False, None, None)):
            with self.subTest(explicit=explicit, remembered=remembered), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); runtime = root / "runtime"; session = runtime / "session-fixture"
                session.mkdir(parents=True)
                (session / "key").write_text("synthetic-loopback-key")
                settings = root / "settings.json"; settings.write_text("{}")
                state = {"port": 12345, "pid": 123, "session": "session-fixture"}
                prepared = []
                def helper(action, selected, **kwargs):
                    prepared.append(selected)
                    return "\t".join((selected if selected.startswith("gpt-") else "gpt-6.1-sol",
                                      "872000", "784800", str(settings), "{}"))
                with patch.object(launcher, "ROOT", root), \
                        patch.object(launcher, "ensure_service", return_value=(runtime, state, False)), \
                        patch.object(launcher, "helper_output", side_effect=helper), \
                        patch.object(launcher, "session_settings", return_value=(settings, [])), \
                        patch.object(launcher, "platform", SimpleNamespace(system=lambda: "Darwin")), \
                        patch.object(session_models, "restore", return_value=(True, remembered)) as restore, \
                        patch.object(launcher.os, "execvpe", side_effect=RuntimeError("fixture-exec")) as execute, \
                        redirect_stderr(StringIO()):
                    with self.assertRaisesRegex(RuntimeError, "fixture-exec"):
                        launcher.run_session("claude", "sol", ["--resume"], "synthetic-claude",
                                             explicit_model=explicit)
                command = execute.call_args.args[1]
                if expected:
                    self.assertEqual(command[command.index("--model") + 1], expected)
                else:
                    self.assertNotIn("--model", command)
                if explicit:
                    restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
