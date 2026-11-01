"""One short synthetic subscription request through a disposable TLS bridge.

Consumes subscription quota. Never restarts the existing origin or ingress and
never prints credentials, response content, session IDs, or private paths.
"""

import argparse
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import time
from unittest.mock import patch
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts
import bridge_models
import launcher
import rolling_runtime
import runtime_bundle


def selected(root: Path) -> str:
    active = [row["fingerprint"] for row in accounts.local_accounts(root) if not row["record"].get("disabled")]
    if len(active) != 1:
        raise RuntimeError("Exactly one selected account is required")
    return active[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    if not args.confirm_live:
        raise RuntimeError("Explicit --confirm-live is required for this quota-consuming probe")
    root = args.root.resolve()
    identity = selected(root)
    management = accounts.Management(root)
    models = accounts.selected_catalog(root, management)
    model = bridge_models.aliases(models).get("sol")
    if not model:
        raise RuntimeError("No eligible Sol model is available")
    bundle_info = runtime_bundle.selected(root)
    if bundle_info is None:
        raise RuntimeError("No reviewed runtime bundle is installed")
    origin_before = launcher.read_state(root / "runtime")
    ingress_before = rolling_runtime.state(root)
    if management.state != origin_before or selected(root) != identity:
        raise RuntimeError("Selected account changed; no inference was started")
    original_config = rolling_runtime.backend_config

    def fixture_config(*values):
        return original_config(*values) + "\nrequests:\n  streaming:\n    keepalive-seconds: 1\n    bootstrap-retries: 0\n"

    with tempfile.TemporaryDirectory(prefix="bridge-live-keepalive-") as temporary:
        isolated = Path(temporary)
        runtime_bundle.private_dir(isolated)
        with patch.object(launcher, "ROOT", isolated), patch.object(rolling_runtime, "backend_config", side_effect=fixture_config):
            runtime_bundle.activate(isolated, bundle_info[0])
            try:
                value = rolling_runtime.ensure(isolated, root / "runtime", origin_before, models)
                if selected(root) != identity:
                    raise RuntimeError("Selected account changed; no inference was started")
                runtime = isolated / "runtime-managed"
                key = rolling_runtime.private_key(runtime / "front-key")
                payload = {"model": model, "max_tokens": 64, "stream": True,
                           "output_config": {"effort": "low"},
                           "messages": [{"role": "user", "content": "Reply only with OK."}]}
                request = urllib.request.Request(f'https://127.0.0.1:{value["port"]}/v1/messages',
                    data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + key,
                    "Content-Type": "application/json", "anthropic-version": "2023-06-01",
                    "x-claude-code-session-id": str(uuid.uuid4())})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                    urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(runtime / "ca.pem"))))
                counts = {"keepalive_comments": 0, "comments_before_content_delta": 0,
                          "message_stop": 0, "errors": 0, "data_events": 0}
                saw_content_delta = False
                started = time.monotonic()
                with opener.open(request, timeout=120) as response:
                    for line in response:
                        if line.startswith(b":"):
                            counts["keepalive_comments"] += 1
                            if not saw_content_delta:
                                counts["comments_before_content_delta"] += 1
                        elif line.startswith(b"data:"):
                            try:
                                event = json.loads(line[5:])
                            except ValueError:
                                continue
                            if isinstance(event, dict):
                                counts["data_events"] += 1
                                if event.get("type") == "content_block_delta": saw_content_delta = True
                                if event.get("type") == "message_stop": counts["message_stop"] += 1
                                if event.get("type") == "error": counts["errors"] += 1
                if selected(root) != identity:
                    raise RuntimeError("Selected account changed during the probe; result withheld")
                if counts["message_stop"] != 1 or counts["errors"]:
                    raise RuntimeError("The synthetic subscription stream did not complete")
                if launcher.read_state(root / "runtime") != origin_before:
                    raise RuntimeError("Origin state changed during the probe")
                current = rolling_runtime.state(root)
                if (current["pid"], current["port"]) != (ingress_before["pid"], ingress_before["port"]):
                    raise RuntimeError("Existing ingress changed during the probe")
                print(json.dumps({"route": "subscription", "model_alias": "sol", "effort": "low",
                    "synthetic_request": True, "elapsed_seconds": round(time.monotonic() - started, 3),
                    "stream": counts, "existing_origin_and_ingress_unchanged": True}, sort_keys=True))
            finally:
                if rolling_runtime.state(isolated) is not None:
                    rolling_runtime.stop(isolated)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, urllib.error.URLError):
        raise SystemExit("Live keep-alive probe unavailable or failed; raw details withheld") from None
