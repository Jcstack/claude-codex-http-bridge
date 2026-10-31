"""Exercise the installed opt-in launcher, /compact, resume and fork.

Consumes quota using synthetic state in a disposable Claude configuration.
Never resumes, compacts or restarts a user's conversation.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from live_compaction import INITIAL, UPDATES
from native_client import NativeClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    root = parser.parse_args().root.resolve()
    client = NativeClient(root)
    origin_before = (root / "runtime/service.json").read_bytes()
    ingress_before = (client.state["pid"], client.state["port"])
    with tempfile.TemporaryDirectory(prefix="bridge-installed-compact-") as temporary:
        base = Path(temporary); base.chmod(0o700)
        work = base / "work"; work.mkdir()
        config = base / "claude"; config.mkdir(mode=0o700)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
        env.update(CLAUDE_CODEX_BRIDGE_HOME=str(root), CLAUDE_CONFIG_DIR=str(config),
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")
        sid = str(uuid.uuid4())
        expected = {**INITIAL, **UPDATES}
        padding = "\n".join(f"Sample {i}: unchanged historical observation." for i in range(100))
        stages = [("seed", "Remember the exact current synthetic configuration: " + json.dumps(expected) + "\n" + padding + "\nDo nothing. Reply READY only."),
                  ("compact", "/compact Preserve every current configuration key, restriction, unresolved issue and next action exactly."),
                  ("recall", "Return only JSON with all twelve current configuration keys and their string values. No markdown."),
                  ("fork", "Return only JSON with all twelve current configuration keys and their string values. No markdown.")]
        for stage, prompt in stages:
            command = [sys.executable, str(root / "launcher.py"), "--native-compaction", "--no-native-tools",
                "--model", "sol", "--effort", "max", "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--tools", "", "--permission-mode", "dontAsk", "--output-format", "json",
                "--session-id" if stage == "seed" else "--resume", sid]
            if stage == "fork": command.append("--fork-session")
            command.append("-p")
            started = time.monotonic()
            process = subprocess.run(command, input=prompt, cwd=work, env=env,
                                     capture_output=True, text=True, timeout=240)
            if process.returncode:
                raise RuntimeError("Installed synthetic compact client failed; raw output withheld")
            result = json.loads(process.stdout)
            if result.get("is_error"):
                raise RuntimeError("Installed synthetic compact response failed; raw output withheld")
            metric = {"stage": stage, "seconds": round(time.monotonic() - started, 3)}
            if stage in ("recall", "fork"):
                try: answer = json.loads(result.get("result", ""))
                except ValueError: answer = {}
                correct = sum(isinstance(answer, dict) and answer.get(k) == v for k, v in expected.items())
                if correct != len(expected): raise RuntimeError("Installed compact continuation lost synthetic state")
                metric["state_fields_correct"] = correct
                if stage == "fork" and (not isinstance(result.get("session_id"), str) or not result["session_id"] or result["session_id"] == sid):
                    raise RuntimeError("Native client did not fork the synthetic session")
            print(json.dumps(metric), flush=True)
        native_carrier = any("codex-native-context-v1:" in path.read_text(encoding="utf-8")
                             for path in (config / "projects").glob("*/*.jsonl"))
        if not native_carrier: raise RuntimeError("Installed launcher did not use native compaction")
        after = NativeClient(root)
        assert ingress_before == (after.state["pid"], after.state["port"])
        assert origin_before == (root / "runtime/service.json").read_bytes()
        print(json.dumps({"installed_native_launcher": True, "native_carrier": True, "resume": True,
            "fork": True, "state_fields_total": len(expected), "origin_and_ingress_preserved": True}))


if __name__ == "__main__": main()
