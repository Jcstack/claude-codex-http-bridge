"""Measure synthetic large-message relay overhead without a model account.

Starts only disposable loopback services in a temporary directory. Bytes are
not tokens, and this fixture does not measure provider or model latency.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from live_compare_translators import stream
from smoke_managed_runtime import MODEL, SPEC, Upstream
import api_profile
import launcher
import rolling_runtime
import runtime_bundle


def main() -> None:
    if len(sys.argv) != 2:
        raise RuntimeError("Usage: probe_large_payload_latency.py /path/to/reviewed-bundle")
    bundle = Path(sys.argv[1]).resolve()
    runtime_bundle.verify(bundle)
    Upstream.requests = []
    Upstream.admitted.clear()
    Upstream.release.set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bridge-large-payload-") as temporary:
            root = Path(temporary)
            runtime_bundle.private_dir(root)
            base_runtime = root / "runtime"
            runtime_bundle.private_dir(base_runtime)
            source = base_runtime / "session-fixture"
            runtime_bundle.private_dir(source)
            for name, value in (("config.yaml", "synthetic"), ("key", "a" * 64), ("proxy.log", "")):
                api_profile.write_private(source / name, value)
            base = {"pid": os.getpid(), "port": server.server_port, "session": source.name}
            with patch.object(launcher, "ROOT", root):
                runtime_bundle.activate(root, bundle)
                value = None
                try:
                    value = rolling_runtime.ensure(root, base_runtime, base, {MODEL: SPEC})
                    managed = root / "runtime-managed"
                    key = rolling_runtime.private_key(managed / "front-key")
                    rows = []
                    for size in (100_000, 1_000_000, 4_000_000):
                        pattern = "SYNTHETIC_CONTEXT_0123456789abcdef\n"
                        content = (pattern * (size // len(pattern) + 1))[:size]
                        payload = {"model": MODEL, "max_tokens": 64,
                                   "messages": [{"role": "user", "content": content}]}
                        blocks, metrics = stream(value["port"], key, payload, ca_file=managed / "ca.pem")
                        received = Upstream.requests[-1].get("input", [])
                        texts = [part.get("text") for item in received if isinstance(item, dict)
                                 for part in item.get("content", []) if isinstance(part, dict)]
                        if content not in texts or not any(block.get("text") == "FIXTURE" for block in blocks):
                            raise RuntimeError("Synthetic payload or response was not preserved")
                        rows.append({"synthetic_input_bytes": len(content.encode()),
                                     "relay_seconds": metrics["seconds"],
                                     "first_delta_seconds": metrics["first_delta_seconds"],
                                     "content_preserved": True})
                    print(json.dumps({"mock_only": True, "provider_latency_measured": False,
                                      "input_sizes_are_bytes_not_tokens": True, "cases": rows}, sort_keys=True))
                finally:
                    Upstream.release.set()
                    if rolling_runtime.state(root) is not None:
                        rolling_runtime.stop(root)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError):
        raise SystemExit("Synthetic large-payload probe failed; raw state withheld") from None
