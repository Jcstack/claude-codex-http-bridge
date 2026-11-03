"""Observe a candidate's startup traffic through a synthetic local proxy.

Both executables use an empty temporary home and synthetic configuration. The
HTTP proxy refuses every request; it neither forwards traffic nor stores URLs.
The child is not an OS sandbox, and this bounded observation cannot prove that
other code paths or transports never connect externally.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from claude_fixture import isolated_environment


MANIFEST_HOST = "antigravity-hub-auto-updater-974169037036.us-central1.run.app:443"


class BlockingProxy(BaseHTTPRequestHandler):
    def log_message(self, *_arguments: object) -> None:
        pass

    def reject(self) -> None:
        self.server.manifest_mentions.append(self.path.lower() == MANIFEST_HOST)  # type: ignore[attr-defined]
        self.send_response(502)
        self.end_headers()

    do_CONNECT = reject
    do_GET = reject
    do_POST = reject


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def observe(binary: Path, root: Path, proxy: ThreadingHTTPServer, expect_manifest: bool) -> None:
    root.mkdir(mode=0o700)
    auth = root / "empty-auth"
    auth.mkdir(mode=0o700)
    port = free_port()
    config = root / "config.yaml"
    config.write_text(f'''config-version: 8
server:
  host: "127.0.0.1"
  port: {port}
management:
  allow-remote: false
  disable-control-panel: true
  disable-auto-update-panel: true
access:
  api-keys: ["synthetic-local-key"]
oauth:
  auth-dir: {json.dumps(str(auth))}
observability:
  logs:
    debug: false
    logging-to-file: false
    request-log: false
''', encoding="utf-8")
    environment = isolated_environment(root)
    proxy_url = f"http://127.0.0.1:{proxy.server_port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        environment[name] = proxy_url
    environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1,localhost"
    proxy.manifest_mentions.clear()  # type: ignore[attr-defined]
    process = subprocess.Popen([str(binary), "-config", str(config)], cwd=root,
                               env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise AssertionError("synthetic proxy exited before becoming ready")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("synthetic proxy did not become ready") from None
                time.sleep(.1)
        request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                                         headers={"Authorization": "Bearer synthetic-local-key"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            if response.status != 200:
                raise AssertionError("synthetic model catalog did not respond")
            response.read()
        deadline = time.monotonic() + (8 if expect_manifest else 3)
        while time.monotonic() < deadline:
            if expect_manifest and any(proxy.manifest_mentions):  # type: ignore[attr-defined]
                break
            if process.poll() is not None:
                raise AssertionError("synthetic proxy exited during observation")
            time.sleep(.05)
        observed = any(proxy.manifest_mentions)  # type: ignore[attr-defined]
        if observed != expect_manifest:
            raise AssertionError("synthetic manifest observation did not match the control")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: probe_antigravity_lazy.py /path/to/original /path/to/candidate")
    original, candidate = (Path(value).resolve(strict=True) for value in sys.argv[1:])
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), BlockingProxy)
    proxy.manifest_mentions = []  # type: ignore[attr-defined]
    worker = threading.Thread(target=proxy.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-lazy-refresh-") as temporary:
            root = Path(temporary)
            observe(original, root / "control", proxy, True)
            observe(candidate, root / "candidate", proxy, False)
    finally:
        proxy.shutdown()
        proxy.server_close()
        worker.join(timeout=5)
    print("bounded startup observation passed against the positive control")


if __name__ == "__main__":
    main()
