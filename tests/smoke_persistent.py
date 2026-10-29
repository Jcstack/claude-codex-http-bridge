"""Verify the installed proxy survives the launcher and can be reused and stopped."""

import os
from pathlib import Path
import sys


root = Path(sys.argv[1]).absolute()
os.environ["CLAUDE_CODEX_BRIDGE_HOME"] = str(root)
sys.path.insert(0, str(root))

import bridge_models  # noqa: E402
import launcher  # noqa: E402


bridge_models.verify_install()
runtime, state, created = launcher.ensure_service(require_catalog=False)
try:
    assert created and launcher.state_is_live(runtime, state)
    runtime_again, state_again, created_again = launcher.ensure_service(require_catalog=False)
    assert not created_again and runtime_again == runtime and state_again == state
finally:
    assert launcher.bridge_stop() == 0
assert not (runtime / "service.json").exists()
print("Persistent bridge start, reuse, and stop: PASS")
