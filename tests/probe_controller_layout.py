"""Observe a naive versioned controller layout using only disposable files.

This is not a versioned installer, a migration, or a test of an existing
installation. It launches no bridge, reads no account, and calls no network.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from claude_fixture import isolated_environment


SOURCE = Path(__file__).resolve().parents[1]
MODULES = ("launcher.py", "bridge_models.py", "codex_usage.py", "statusline_adapter.py",
           "session_models.py", "api_profile.py", "autostart.py", "rolling_runtime.py",
           "runtime_bundle.py", "windows_acl.py", "accounts.py", "process_identity.py")


def main():
    with tempfile.TemporaryDirectory(prefix="bridge-controller-layout-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        root = Path(temporary).resolve()
        generation = root / "controllers" / "fixture-generation"
        generation.mkdir(parents=True, mode=0o700)
        config = root / "claude-config"
        config.mkdir(mode=0o700)
        for name in MODULES:
            shutil.copyfile(SOURCE / name, generation / name)
        (root / "runtime-source-lock.json").write_text('{"synthetic":true}\n', encoding="utf-8")
        environment = isolated_environment(root)
        environment.update(CLAUDE_CODEX_BRIDGE_HOME=str(root), CLAUDE_CONFIG_DIR=str(config), PYTHONDONTWRITEBYTECODE="1")
        inspector = r'''
import json, sys
from pathlib import Path
import launcher, bridge_models, codex_usage, statusline_adapter, autostart, runtime_bundle
root = Path(sys.argv[1]).resolve()
try:
    source_lock = runtime_bundle.source_lock() == {"synthetic": True}
except (OSError, ValueError):
    source_lock = False
print(json.dumps({"launcher_uses_install_root": launcher.ROOT.resolve() == root,
    "model_helper_uses_install_root": bridge_models.ROOT.resolve() == root,
    "usage_helper_uses_install_root": codex_usage.ROOT.resolve() == root,
    "statusline_uses_install_root": statusline_adapter.ROOT.resolve() == root,
    "autostart_accepts_install_root": Path(autostart.__file__).resolve().parent == root,
    "source_lock_available": source_lock}))
'''
        result = subprocess.run([sys.executable, "-c", inspector, str(root)], cwd=generation, env=environment,
                                capture_output=True, text=True, timeout=30, check=True)
        observation = json.loads(result.stdout)
        profile = root / "fixture-profile.json"
        profile.write_text(json.dumps({"context_windows": {"gpt-fixture": 100000}, "remember_model": True}),
                           encoding="utf-8")
        payload = {"session_id": "00000000-0000-0000-0000-000000000001", "model": {"id": "gpt-fixture"},
                   "cwd": str(root / "workspace"), "context_window": {}}
        rendered = subprocess.run([sys.executable, str(generation / "statusline_adapter.py"), str(profile)],
                                  cwd=generation, env=environment, input=json.dumps(payload),
                                  capture_output=True, text=True, timeout=30, check=True)
        if not isinstance(json.loads(rendered.stdout), dict):
            raise RuntimeError("Synthetic status-line fixture returned invalid JSON")
        observation.update(synthetic_only=True, statusline_recorded_under_generation=
                           (generation / "runtime-session-models").is_dir(),
                           statusline_recorded_under_install_root=(root / "runtime-session-models").is_dir())
        print(json.dumps(observation, sort_keys=True))
        if os.environ.get("GITHUB_ACTIONS") == "true" and not all(observation[key] for key in (
                "model_helper_uses_install_root", "usage_helper_uses_install_root", "statusline_uses_install_root",
                "autostart_accepts_install_root", "source_lock_available", "statusline_recorded_under_install_root")):
            print("::warning::Naive controller generations do not yet preserve all runtime roots; inspect the observation.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise SystemExit("Synthetic controller-layout observation failed; raw output withheld") from None
