"""Private temporary translator, using only an already-running localhost route."""

from contextlib import contextmanager
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile
import bridge_models
import launcher


def existing_subscription(installed: Path) -> dict:
    runtime = installed / "runtime"
    state = launcher.read_state(runtime)
    if state is None or not launcher.state_is_live(runtime, state):
        raise RuntimeError("An existing healthy subscription service is required; no service was started")
    _, _, key_path, _ = launcher.state_paths(runtime, state)
    with patch.dict(os.environ, {"CODEX_BRIDGE_PORT": str(state["port"]),
                                 "CODEX_BRIDGE_KEY_FILE": str(key_path)}):
        models, _ = bridge_models.discover()
    return {"state": state, "key": key_path.read_text().strip(), "models": models}


@contextmanager
def temporary_translator(binary: Path, upstream: dict, model: str, *, models: dict | None = None):
    with tempfile.TemporaryDirectory(prefix="bridge-live-fixture-") as temporary:
        root = Path(temporary)
        runtime = root / "runtime-api"
        runtime.mkdir(mode=0o700)
        api_profile.set_key(root, upstream["key"])
        with patch.object(api_profile, "discover_models", return_value=models or {model: upstream["models"][model]}):
            port, _, config, key, _ = api_profile.make_session(root, runtime)
        source = config.read_text()
        target = 'base-url: "https://api.openai.com/v1"'
        if source.count(target) != 1:
            raise RuntimeError("Unexpected temporary upstream configuration")
        source = source.replace(target, f'base-url: "http://127.0.0.1:{upstream["state"]["port"]}/v1"')
        if "api.openai.com" in source:
            raise RuntimeError("Public API route is not permitted in this test")
        config.write_text(source)
        process = subprocess.Popen([str(binary), "-config", str(config)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, cwd=root)
        try:
            launcher.wait_for_private_listener(process, port)
            environment = os.environ.copy()
            environment.update({"CODEX_BRIDGE_PORT": str(port), "CODEX_BRIDGE_KEY_FILE": str(key),
                                "CODEX_BRIDGE_ROUTE": "api",
                                "CODEX_BRIDGE_API_METADATA_FILE": str(runtime / "catalog.json")})
            launcher.wait_for_catalog(process, environment, True, "api")
            yield port, key.read_text().strip()
        finally:
            launcher.stop_process(process)
