"""Observe same-account cache counters across two synthetic sessions.

Five low-effort subscription requests consume quota. No user content, account
credentials, responses, or generated session identifiers are printed.
"""

import argparse
import json
import os
from pathlib import Path
import ssl
import sys
import time
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile
import bridge_models
import codex_usage
import rolling_runtime
from live_support import existing_subscription


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args()
    if not args.confirm_live:
        raise RuntimeError("Explicit --confirm-live is required for a quota-consuming experiment")
    root = args.root.expanduser().absolute()
    identity = codex_usage.selected_fingerprint(root)
    if not identity:
        raise RuntimeError("Exactly one selected account is required")
    with patch.dict(os.environ, {"CODEX_BRIDGE_ROUTE": "subscription"}):
        upstream = existing_subscription(root)
    model = bridge_models.aliases(upstream["models"]).get("sol")
    if model is None:
        raise RuntimeError("No eligible Sol model is available")
    managed = rolling_runtime.state(root)
    if managed is None:
        raise RuntimeError("A running managed bridge is required")
    rolling_runtime.endpoint_status(root, managed)
    runtime = root / "runtime-managed"
    key = rolling_runtime.private_key(runtime / "front-key")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), api_profile.NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(runtime / "ca.pem"))))
    sessions = {label: str(uuid.uuid4()) for label in ("A", "B")}
    reference = "Synthetic reference " + str(uuid.uuid4()) + "\n" + "\n".join(
        f"item{i:04}=VALUE_{i * 17:04}" for i in range(256))
    rows = []
    for label, phase in (("A", "first"), ("A", "repeat"), ("B", "first"),
                         ("B", "repeat"), ("A", "after_B")):
        if codex_usage.selected_fingerprint(root) != identity:
            raise RuntimeError("Selected account changed before a request")
        payload = {"model": model, "max_tokens": 64, "stream": False,
                   "output_config": {"effort": "low"},
                   "metadata": {"user_id": json.dumps({"session_id": sessions[label]})},
                   "system": "Use only this synthetic reference. " + reference,
                   "messages": [{"role": "user", "content": "Report item0255 exactly."}]}
        request = urllib.request.Request(f'https://127.0.0.1:{managed["port"]}/v1/messages',
            data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + key,
            "Content-Type": "application/json", "anthropic-version": "2023-06-01"})
        started = time.monotonic()
        try:
            with opener.open(request, timeout=60) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            print(json.dumps({"synthetic_session": label, "phase": phase, "http_status": status,
                              "response_body_withheld": True}), flush=True)
            raise RuntimeError("The synthetic request was rejected") from None
        if len(raw) > 2_000_000 or codex_usage.selected_fingerprint(root) != identity:
            raise RuntimeError("Selected account changed or the response was too large")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("type") != "message":
            raise RuntimeError("The synthetic request did not complete")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        content = result.get("content") if isinstance(result.get("content"), list) else []
        answer = "".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
        row = {"synthetic_session": label, "phase": phase, "seconds": round(time.monotonic() - started, 3),
               "reference_answer_present": "VALUE_4335" in answer}
        for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
            value = usage.get(name)
            row[name] = value if type(value) is int and value >= 0 else None
        rows.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)
    if codex_usage.selected_fingerprint(root) != identity:
        raise RuntimeError("Selected account changed during the experiment")
    print(json.dumps({"same_account": True, "synthetic_only": True, "model_alias": "sol", "effort": "low",
        "completed_cases": len(rows), "different_accounts_not_tested": True, "unreported_is_not_zero": True}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, urllib.error.URLError, TimeoutError):
        raise SystemExit("Synthetic cache-scope experiment stopped; raw state withheld") from None
