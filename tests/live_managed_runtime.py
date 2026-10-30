"""Two real Claude turns across a managed backend reload on one subscription.

Consumes quota. Uses only a synthetic workspace and isolated Claude config.
Requires an already activated managed runtime; never starts/stops the legacy
service. No OAuth/project key or raw user transcript is read by this probe.
"""

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

from live_support import existing_subscription
import launcher
import rolling_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(); root = args.root.expanduser().resolve()
    existing_subscription(root)
    before = rolling_runtime.state(root)
    if before is None: raise RuntimeError("An activated managed runtime is required")
    legacy = (root / "runtime" / "service.json").read_bytes()
    with tempfile.TemporaryDirectory(prefix="bridge-managed-live-") as temporary:
        work = Path(temporary) / "workspace"; work.mkdir()
        config = Path(temporary) / "claude"; config.mkdir(mode=0o700)
        (work / "fixture.txt").write_text("VIOLET_73\n", encoding="utf-8")
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if k in os.environ}
        env.update({"CLAUDE_CODEX_BRIDGE_HOME": str(root), "CLAUDE_CONFIG_DIR": str(config),
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1"})
        command = [sys.executable, str(root / "launcher.py"), "--model", "sol", "--effort", "high",
            "--restricted", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--tools", "Read", "--allowedTools", "Read", "--permission-mode", "dontAsk",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "-p"]
        process = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        events = queue.Queue()
        def reader():
            for line in process.stdout:
                try: events.put(json.loads(line))
                except ValueError: pass
            events.put({"type": "fixture_eof"})
        thread = threading.Thread(target=reader, daemon=True); thread.start()

        def turn(prompt):
            start = time.monotonic()
            process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            process.stdin.flush()
            while True:
                event = events.get(timeout=120)
                if event.get("type") == "fixture_eof": raise RuntimeError("Live Claude client exited early")
                if event.get("type") == "result":
                    if event.get("is_error") or "VIOLET_73" not in event.get("result", ""):
                        raise RuntimeError("Live fixture recall failed; raw output withheld")
                    usage = event.get("usage", {})
                    return {"seconds": round(time.monotonic()-start, 3), "usage": {
                        key: usage[key] for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                        if type(usage.get(key)) is int}}
        try:
            first = turn("Read fixture.txt in this workspace with the Read tool. Reply with its exact value only.")
            transcripts = list((config / "projects").glob("*/*.jsonl"))
            read_seen = False
            for transcript in transcripts:
                for line in transcript.read_text(encoding="utf-8").splitlines():
                    record = json.loads(line)
                    if record.get("type") == "assistant":
                        read_seen |= any(b.get("type") == "tool_use" and b.get("name") == "Read"
                                         for b in record.get("message", {}).get("content", []) if isinstance(b, dict))
            if not read_seen: raise RuntimeError("The live harness did not exercise Read")
            reload = subprocess.run([sys.executable, str(root / "launcher.py"), "bridge", "reload"],
                                    env=env, cwd=work, capture_output=True, text=True, timeout=60)
            if reload.returncode: raise RuntimeError("Managed live reload failed; raw output withheld")
            after = rolling_runtime.state(root)
            if (before["pid"], before["port"]) != (after["pid"], after["port"]):
                raise RuntimeError("The stable endpoint was replaced")
            second = turn("Repeat the exact value you read in this conversation. Do not call any tools.")
            if legacy != (root / "runtime" / "service.json").read_bytes():
                raise RuntimeError("Legacy service state changed")
            print(json.dumps({"subscription_only": True, "same_live_claude_process": "pass", "read_tool": "pass",
                              "stable_endpoint_after_reload": "pass", "recall_after_reload": "pass",
                              "legacy_service_unchanged": True, "first": first, "second": second}))
        finally:
            launcher.stop_process(process); thread.join(timeout=2)


if __name__ == "__main__": main()
