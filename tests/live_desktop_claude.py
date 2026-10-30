"""Let a real subscribed Claude harness drive only an owned ordinary fixture.

Temporarily selects a window in an isolated workspace, then restores selection.
No user window is captured, no microphone/key is read, no service is restarted.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_fixture import fixture
import desktop_adapter
import desktop_scope
import runtime_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(); root = args.root.resolve()
    desktop_adapter.executable(root)
    selection_path = root/"desktop.json"
    if selection_path.exists():
        # Existing sessions could own that target. Do not temporarily replace
        # even a disabled user selection just for this optional live probe.
        raise RuntimeError("This probe requires no existing desktop selection; it will not replace one")
    with tempfile.TemporaryDirectory(prefix="bridge-desktop-claude-") as temporary:
        base = Path(temporary); base.chmod(0o700)
        work = base/"workspace"; work.mkdir(); config = base/"claude"; config.mkdir(mode=0o700)
        with fixture() as (_process, state, value):
            identity = desktop_scope.inventory().identity(value["id"])
            assert identity["pid"] == value["pid"]
            choice = {"format": 1, "enabled": True, "workspace": str(work.resolve()), "identity": identity}
            encoded = runtime_bundle.canonical(choice)
            runtime_bundle.write_file(selection_path, encoded, 0o600)
            env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
            env.update({"CLAUDE_CODEX_BRIDGE_HOME": str(root), "CLAUDE_CONFIG_DIR": str(config),
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"})
            names = ("mcp__codex_native__desktop_observe", "mcp__codex_native__desktop_action")
            prompt = ("Use ToolSearch to load the two codex_native desktop tools. Only the selected synthetic fixture window is authorized. "
                "Observe it, set the observed Fixture input field to exactly DESKTOP_MODEL_READY, observe again, click Save fixture once, "
                "and observe once more to verify the text. Use set_value and element click with the one-use observation. "
                "Do not use other tools, switch windows, activate anything, type into a terminal, record audio or retry an uncertain click. "
                "Report whether you completed those actions.")
            started = time.monotonic()
            try:
                run = subprocess.run([sys.executable, str(root/"launcher.py"), "--native-tools", "--model", "terra", "--effort", "high",
                    "--restricted", "--strict-mcp-config", "--tools", "ToolSearch", "--allowedTools", "ToolSearch", *names,
                    "--permission-mode", "dontAsk", "--max-turns", "12", "--output-format", "stream-json", "--verbose", "-p", prompt],
                    env=env, cwd=work, capture_output=True, text=True, timeout=180)
                if run.returncode: raise RuntimeError("Real Claude desktop process failed; raw output withheld")
                events = []
                for line in run.stdout.splitlines():
                    try: events.append(json.loads(line))
                    except ValueError: pass
                uses = [block for event in events for block in event.get("message", {}).get("content", [])
                    if isinstance(block, dict) and block.get("type") == "tool_use"]
                counts = {name: sum(block["name"] == name for block in uses) for name in names}
                actual = json.loads(state.read_text())
                assert actual["value"] == "DESKTOP_MODEL_READY" and actual["count"] == 1, "The actual fixture UI state was not updated exactly once"
                assert counts[names[0]] >= 3 and counts[names[1]] == 2, "Expected observe/action sequence was not used"
                print(json.dumps({"real_claude": True, "subscription_only": True, "owned_window_only": True,
                    "requested_model_alias": "terra", "tool_search": any(b["name"] == "ToolSearch" for b in uses),
                    "native_text_change": "pass", "native_button_invocations": actual["count"],
                    "desktop_observations": counts[names[0]], "desktop_actions": counts[names[1]],
                    "seconds": round(time.monotonic()-started,3)}))
            finally:
                # Do not clobber a new user selection made during the test.
                if selection_path.exists() and selection_path.read_bytes() == encoded: selection_path.unlink()


if __name__ == "__main__": main()
