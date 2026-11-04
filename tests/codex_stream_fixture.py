"""Owned local Codex-executor proxy for synthetic stream observations only."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from claude_fixture import isolated_environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher


@contextmanager
def temporary_codex_translator(binary: Path, upstream_port: int, *, upstream_prefix: str = "",
                               upstream_key: str = "fixture-upstream"):
    if (type(upstream_port) is not int or not 1 <= upstream_port <= 65535 or not binary.is_file()
            or upstream_prefix not in ("", "/v1") or upstream_key not in ("fixture-upstream", "fixture-local")):
        raise ValueError("Invalid synthetic translator fixture")
    with tempfile.TemporaryDirectory(prefix="bridge-codex-stream-fixture-") as temporary:
        root = Path(temporary)
        auth = root / "auth"
        auth.mkdir(mode=0o700)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = root / "config.yaml"
        config.write_text(f'''config-version: 8
server: {{host: "127.0.0.1", port: {port}}}
access: {{api-keys: ["fixture-local"]}}
management: {{allow-remote: false, disable-control-panel: true, disable-auto-update-panel: true}}
oauth: {{auth-dir: {json.dumps(str(auth))}}}
api-keys:
  codex:
    - name: fixture
      base-url: "http://127.0.0.1:{upstream_port}{upstream_prefix}"
      models:
        - name: "gpt-6.1-sol"
          alias: "gpt-6.1-sol"
      keys:
        - api-key: "{upstream_key}"
''', encoding="utf-8")
        config.chmod(0o600)
        environment = isolated_environment(root)
        environment.update(CLAUDE_CODEX_STREAM_DIAGNOSTICS="1", HTTP_PROXY="http://127.0.0.1:9",
                           HTTPS_PROXY="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9",
                           NO_PROXY="127.0.0.1,localhost")
        logfile = root / "proxy.log"
        with logfile.open("w+b") as output:
            process = subprocess.Popen([str(binary), "--local-model", "-config", str(config)], cwd=root,
                                       env=environment, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
            try:
                launcher.wait_for_private_listener(process, port)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                                 headers={"Authorization": "Bearer fixture-local"})
                deadline = time.monotonic() + 12
                while True:
                    try:
                        with opener.open(request, timeout=1) as response:
                            value = json.load(response)
                        if any(row.get("id") == "gpt-6.1-sol" for row in value.get("data", []) if isinstance(row, dict)):
                            break
                    except (OSError, ValueError, urllib.error.URLError):
                        pass
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Synthetic Codex model did not become ready")
                    time.sleep(.1)
                yield port, "fixture-local", logfile
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
