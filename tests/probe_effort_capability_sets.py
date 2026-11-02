"""Observe per-alias capability flags with real Claude and a fake API.

This credential-free fixture does not change the production model gate. A
valid request to a fake server does not prove a real model supports an effort.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from claude_fixture import isolated_environment
from probe_claude_client import FixtureServer, Handler


SOL = "gpt-6.1-sol"
TERRA = "gpt-6.1-terra"


def main():
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("The locked Claude fixture is required")
    with tempfile.TemporaryDirectory(prefix="bridge-effort-capabilities-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary)
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        settings = root / "settings.json"
        picker = {"options": [{"model": model, "label": "Synthetic fixture",
                               "behavesAs": "claude-sonnet-5-5"} for model in (SOL, TERRA)],
                  "replaceBuiltInOptions": True}
        settings.write_text(json.dumps({"modelPicker": picker}), encoding="utf-8")
        server = FixtureServer(("127.0.0.1", 0), Handler)
        server.requests, server.work, server.mode = [], work, "text"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_AUTH_TOKEN="fixture-only",
                   ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                   ANTHROPIC_DEFAULT_SONNET_MODEL=SOL, CLAUDE_CODE_ATTRIBUTION_HEADER="0",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1")

        def run(model, requested, capabilities, selection=None, no_behaves_as=False):
            case_env = dict(env)
            if capabilities is not None:
                case_env["ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES"] = capabilities
            case_picker = picker if not no_behaves_as else {**picker, "options": [
                {key: value for key, value in option.items() if key != "behavesAs"} for option in picker["options"]]}
            settings.write_text(json.dumps({"modelPicker": case_picker}), encoding="utf-8")
            before = len(server.requests)
            process = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config",
                '{"mcpServers":{}}', "--settings", str(settings), "--permission-mode", "dontAsk",
                "--output-format", "json", "--model", selection or model, "--effort", requested, "--tools", "",
                "-p", "Return the synthetic fixture response."], cwd=work, env=case_env,
                capture_output=True, text=True, timeout=45)
            if process.returncode or json.loads(process.stdout).get("is_error"):
                raise RuntimeError("Synthetic capability case did not complete")
            rows = server.requests[before:]
            if len(rows) != 1 or rows[0].get("model") != model:
                raise RuntimeError("Synthetic capability case sent an unexpected request")
            return (rows[0].get("output_config") or {}).get("effort")

        try:
            cases = {
                "baseline_max": (SOL, "max", None),
                "effort_only_xhigh": (SOL, "xhigh", "effort,thinking"),
                "effort_only_max": (SOL, "max", "effort,thinking"),
                "max_without_xhigh_requested_xhigh": (SOL, "xhigh", "effort,max_effort,thinking"),
                "max_without_xhigh_requested_max": (SOL, "max", "effort,max_effort,thinking"),
                "xhigh_without_max_requested_max": (SOL, "max", "effort,xhigh_effort,thinking"),
                "all_levels_requested_max": (SOL, "max", "effort,xhigh_effort,max_effort,thinking"),
                "other_model_with_effort_only": (TERRA, "max", "effort,thinking"),
                "sonnet_alias_effort_only_max": (SOL, "max", "effort,thinking", "sonnet"),
                "sonnet_alias_max_without_xhigh": (SOL, "xhigh", "effort,max_effort,thinking", "sonnet"),
                "direct_without_behaves_as_effort_only_max": (SOL, "max", "effort,thinking", None, True),
            }
            results = {name: run(*values) for name, values in cases.items()}
            if results["baseline_max"] != "max":
                raise RuntimeError("Synthetic uncapped positive control failed")
            print(json.dumps({"synthetic_only": True, "fake_messages_without_translator": True,
                              "observed_efforts": results, "production_gate_changed": False}, sort_keys=True))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic effort-capabilities probe failed; raw output withheld") from None
