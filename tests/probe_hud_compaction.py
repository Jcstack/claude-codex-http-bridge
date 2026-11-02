"""Observe an explicitly supplied Claude HUD build with synthetic status frames.

No real session or provider is contacted. The supplied plugin's JavaScript is
executed with a temporary HOME and Claude config, which is not an OS sandbox.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from claude_fixture import isolated_environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import statusline_adapter  # noqa: E402


NODE_PROBE = r"""
import { pathToFileURL } from 'node:url';
import path from 'node:path';
const hud = process.argv[1], home = process.argv[2];
const { getContextPercent } = await import(pathToFileURL(path.join(hud, 'dist', 'stdin.js')));
const { applyContextWindowFallback } = await import(pathToFileURL(path.join(hud, 'dist', 'context-cache.js')));
let raw = ''; for await (const part of process.stdin) raw += part;
const frames = JSON.parse(raw), now = Date.now();
const deps = { homeDir: () => home, now: () => now, random: () => 1 };
const transcript = path.join(home, 'fixture.jsonl');
frames.before.transcript_path = transcript;
applyContextWindowFallback(frames.before, deps);
const before = getContextPercent(frames.before);
const withoutHint = getContextPercent(frames.after);
frames.after.transcript_path = transcript;
applyContextWindowFallback(frames.after, deps, undefined,
  { lastCompactBoundaryAt: new Date(now + 1000), lastCompactPostTokens: 51271 });
console.log(JSON.stringify({before, withoutHint, afterHint: getContextPercent(frames.after),
  afterUsageStillUnknown: frames.after.context_window.current_usage === null}));
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hud-dir", type=Path, required=True, help="Explicit local Claude HUD package directory")
    args = parser.parse_args()
    hud = args.hud_dir.resolve()
    package = json.loads((hud / "package.json").read_text(encoding="utf-8"))
    version = package.get("version") if isinstance(package, dict) and package.get("name") == "claude-hud" else None
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.+-]{1,40}", version):
        raise RuntimeError("The supplied package is not a recognizable Claude HUD build")
    model = "gpt-fixture"
    windows = {model: 872_000}
    initial = {"model": {"id": model}, "context_window": {"current_usage": {
        "input_tokens": 5_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 82_000}}}
    compacted = {"model": {"id": model}, "context_window": {"current_usage": None, "used_percentage": 50}}
    frames = {"before": statusline_adapter.corrected_payload(initial, windows),
              "after": statusline_adapter.corrected_payload(compacted, windows)}
    if frames["after"]["context_window"]["used_percentage"] is not None:
        raise RuntimeError("The adapter invented a post-compact percentage")
    with tempfile.TemporaryDirectory(prefix="bridge-hud-compact-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        env = isolated_environment(root)
        env["CLAUDE_CONFIG_DIR"] = str(root / "claude-config")
        result = subprocess.run(["node", "--input-type=module", "-e", NODE_PROBE, str(hud), str(root)],
                                input=json.dumps(frames), text=True, capture_output=True,
                                cwd=root, env=env, timeout=15, check=True)
        observed = json.loads(result.stdout)
    if not isinstance(observed, dict) or any(type(observed.get(key)) is not int for key in ("before", "withoutHint", "afterHint")):
        raise RuntimeError("The supplied HUD returned an unexpected synthetic result")
    print(json.dumps({"synthetic_only": True, "hud_version": version, "adapter_reports_unknown": True,
                      "before_percent": observed["before"], "without_compact_hint_percent": observed["withoutHint"],
                      "with_compact_hint_percent": observed["afterHint"],
                      "hint_keeps_usage_unknown": observed.get("afterUsageStillUnknown") is True}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic HUD compaction observation failed; raw output withheld") from None
