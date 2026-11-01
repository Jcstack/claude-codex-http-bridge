"""Check per-model effort caps with a disposable Claude and fake Messages API.

This probes client behavior only; it does not relax the production model gate.
"""

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from probe_claude_client import FixtureServer, Handler


def main() -> None:
    claude = os.environ.get("CLAUDE_CODEX_TEST_BIN") or shutil.which("claude")
    if not claude:
        raise RuntimeError("Claude client fixture is unavailable")
    with tempfile.TemporaryDirectory(prefix="bridge-effort-caps-") as temporary:
        root = Path(temporary)
        work = root / "work"
        work.mkdir()
        config = root / "config"
        config.mkdir(mode=0o700)
        settings = root / "settings.json"
        sol, terra = "gpt-6.1-sol", "gpt-6.1-terra"
        picker = {"options": [{"model": model, "label": "Synthetic fixture",
                               "behavesAs": "claude-sonnet-5-5"} for model in (sol, terra)],
                  "replaceBuiltInOptions": True}
        server = FixtureServer(("127.0.0.1", 0), Handler)
        server.requests = []
        server.work = work
        server.mode = "text"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC",
               "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if key in os.environ}
        env.update({"CLAUDE_CONFIG_DIR": str(config), "ANTHROPIC_AUTH_TOKEN": "fixture-only",
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": sol,
                    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "DISABLE_TELEMETRY": "1"})

        def run(model: str, effort: str, *, cap: str | None, environment_effort: str | None = None) -> str | None:
            value = {"modelPicker": picker}
            if cap:
                value["modelSettings"] = {sol: {"maxEffortLevel": cap}}
            settings.write_text(json.dumps(value), encoding="utf-8")
            request_env = dict(env)
            if environment_effort:
                request_env["CLAUDE_CODE_EFFORT_LEVEL"] = environment_effort
            before = len(server.requests)
            result = subprocess.run([claude, "--restricted", "--strict-mcp-config", "--mcp-config",
                '{"mcpServers":{}}', "--settings", str(settings), "--permission-mode", "dontAsk",
                "--output-format", "json", "--model", model, "--effort", effort, "--tools", "",
                "-p", "Return the fixture response."], cwd=work, env=request_env,
                capture_output=True, text=True, timeout=45)
            if result.returncode or json.loads(result.stdout).get("is_error"):
                raise RuntimeError("Isolated effort fixture failed; raw output withheld")
            requests = server.requests[before:]
            if len(requests) != 1 or requests[0].get("model") != model:
                raise RuntimeError("Isolated effort fixture sent an unexpected request")
            return (requests[0].get("output_config") or {}).get("effort")

        try:
            results = {"uncapped_max": run(sol, "max", cap=None),
                       "sol_cap_high_max_requested": run(sol, "max", cap="high"),
                       "sol_cap_high_medium_requested": run(sol, "medium", cap="high"),
                       "sol_cap_high_environment_max": run(sol, "max", cap="high", environment_effort="max"),
                       "terra_uncapped_max": run(terra, "max", cap="high")}
            expected = {"uncapped_max": "max", "sol_cap_high_max_requested": "high",
                        "sol_cap_high_medium_requested": "medium", "sol_cap_high_environment_max": "high",
                        "terra_uncapped_max": "max"}
            if results != expected:
                raise RuntimeError("Isolated effort cap behavior differed from the fixture expectation")
            print(json.dumps({"mock_only": True, "observed_request_efforts": results,
                "noncontiguous_supported_levels_need_additional_validation": True}, sort_keys=True))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
