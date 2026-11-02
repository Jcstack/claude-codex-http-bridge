"""Observe interactive effort flags using a fake upstream and temporary proxy.

The scenario declares only baseline effort for the Sonnet alias. It records
what the real client sends after /effort, not what any real model supports.
"""

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

from claude_fixture import isolated_environment
from live_support import temporary_translator
from probe_effort_roundtrip import Handler, LEVELS, interactive


def main():
    if os.name == "nt":
        print(json.dumps({"synthetic_only": True, "skipped": "Windows interactive console not covered"}))
        return
    if len(sys.argv) != 2:
        raise RuntimeError("A candidate translator path is required")
    binary = Path(sys.argv[1]).resolve()
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude or not binary.is_file():
        raise RuntimeError("The locked Claude fixture and translator are required")
    model = "gpt-6.1-sol"
    with tempfile.TemporaryDirectory(prefix="bridge-effort-capability-tui-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        work, config = root / "work", root / "config"
        work.mkdir()
        config.mkdir(mode=0o700)
        settings = root / "settings.json"
        settings.write_text(json.dumps({"modelPicker": {"options": [{"model": model, "label": "Fixture",
            "behavesAs": "claude-sonnet-5-5"}], "replaceBuiltInOptions": True}}), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.model, server.requests, server.bodies = model, [], []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = isolated_environment(root)
        env.update(CLAUDE_CONFIG_DIR=str(config), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1", ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES="effort,thinking")
        spec = {"context_window": 500000, "supports_search_tool": True, "input_modalities": ["text"],
                "supported_reasoning_levels": [{"effort": value} for value in LEVELS]}
        upstream = {"state": {"port": server.server_port}, "key": "synthetic-local-upstream", "models": {model: spec}}
        try:
            with temporary_translator(binary, upstream, model) as (port, key):
                env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}", ANTHROPIC_AUTH_TOKEN=key,
                           ANTHROPIC_DEFAULT_SONNET_MODEL=model)
                observations = interactive(claude, settings, work, config, env, server, model)
                print(json.dumps({"synthetic_only": True, "restricted_capability_flag": "effort,thinking",
                    "interactive_requested_levels_reached_fake_upstream": {name: observations[name] for name in LEVELS},
                    "production_gate_changed": False}, sort_keys=True))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Synthetic effort capability TUI probe failed; raw output withheld") from None
