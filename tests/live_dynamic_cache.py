"""Measure dynamic-tool cache counters through an existing subscription bridge.

This opt-in live experiment consumes quota. It sends only synthetic content,
performs no tools, starts no services, and prints no keys, replies or session IDs.
It is not a provider-wide cache or privacy certification.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_profile  # noqa: E402
import bridge_models  # noqa: E402
import codex_usage  # noqa: E402
import launcher  # noqa: E402
import rolling_runtime  # noqa: E402
from live_support import existing_subscription  # noqa: E402


def tool(name: str, *, deferred: bool = False) -> dict:
    result = {"name": name, "description": f"Synthetic {name} fixture",
              "input_schema": {"type": "object", "properties": {"value": {"type": "string"}},
                               "required": ["value"]}}
    if deferred:
        result["defer_loading"] = True
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--model", default="sol")
    parser.add_argument("--confirm-live", action="store_true", help="Send six small synthetic requests")
    args = parser.parse_args()
    if not args.confirm_live:
        raise RuntimeError("Explicit --confirm-live is required for a quota-consuming experiment")
    root = args.root.expanduser().absolute()
    with patch.dict(os.environ, {"CODEX_BRIDGE_ROUTE": "subscription"}):
        upstream = existing_subscription(root)
    models = upstream["models"]
    model = bridge_models.aliases(models).get(args.model, args.model)
    if model not in models or not bridge_models.eligible(models[model]):
        raise RuntimeError("Requested model is absent or ineligible")
    identity = codex_usage.selected_fingerprint(root)
    if not identity:
        raise RuntimeError("Exactly one selected account is required")
    managed = rolling_runtime.state(root)
    if not managed:
        raise RuntimeError("A running managed bridge is required; no service was started")
    rolling_runtime.endpoint_status(root, managed)
    runtime = root / "runtime-managed"
    key = rolling_runtime.private_key(runtime / "front-key")
    context = ssl.create_default_context(cafile=str(runtime / "ca.pem"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
             urllib.request.HTTPSHandler(context=context), api_profile.NoRedirect())

    synthetic_id = str(uuid.uuid4())
    reference = "Synthetic cache reference " + synthetic_id + "\n" + "\n".join(
        f"item{i:04}=VALUE_{i * 17:04}" for i in range(256))
    base = {"model": model, "max_tokens": 64, "stream": False,
            "output_config": {"effort": "low"},
            "metadata": {"user_id": json.dumps({"session_id": synthetic_id})},
            "system": "Use only the synthetic reference. " + reference,
            "tools": [tool("ToolSearch"), tool("Read"), tool("mcp__demo__lookup", deferred=True)]}
    before = [{"role": "user", "content": "Search the synthetic lookup tool, then report item0255."}]
    revealed = [*before, {"role": "assistant", "content": [{
        "type": "tool_use", "id": "toolu_synthetic_search", "name": "ToolSearch",
        "input": {"query": "mcp__demo__lookup"}}]}, {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "toolu_synthetic_search", "content": [
                {"type": "tool_reference", "tool_name": "mcp__demo__lookup"}]},
            {"type": "text", "text": "Report item0255 without calling any tools."}]}]
    removed = [*revealed, {"role": "system", "content": [{"type": "tool_removal", "tool": {
        "type": "tool_reference", "name": "mcp__demo__lookup"}}]}]

    for phase, messages in (("before", before), ("revealed", revealed), ("removed", removed)):
        for repeat in (1, 2):
            if codex_usage.selected_fingerprint(root) != identity:
                raise RuntimeError("Account selection changed during the experiment; stopped")
            payload = {**base, "messages": copy.deepcopy(messages)}
            request = urllib.request.Request(f"https://127.0.0.1:{managed['port']}/v1/messages",
                data=json.dumps(payload).encode(), method="POST",
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                         "anthropic-version": "2023-06-01"})
            start = time.monotonic()
            try:
                with opener.open(request, timeout=60) as response:
                    raw = response.read(2_000_001)
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
                print(json.dumps({"phase": phase, "repeat": repeat, "http_status": status,
                                  "response_body_withheld": True}), flush=True)
                raise RuntimeError("Synthetic request was rejected") from None
            if len(raw) > 2_000_000:
                raise RuntimeError("Synthetic response exceeded the safety limit")
            if codex_usage.selected_fingerprint(root) != identity:
                raise RuntimeError("Account selection changed during the request; result discarded")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("type") != "message":
                raise RuntimeError("Synthetic request did not return a message")
            usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
            content = value.get("content") if isinstance(value.get("content"), list) else []
            answer = "".join(item.get("text", "") for item in content
                             if isinstance(item, dict) and item.get("type") == "text")
            stop = value.get("stop_reason")
            row = {"phase": phase, "repeat": repeat, "model": model,
                   "stop_reason": stop if stop in ("end_turn", "tool_use", "max_tokens") else "other",
                   "reference_answer_present": "VALUE_4335" in answer,
                   "seconds": round(time.monotonic() - start, 3)}
            for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
                value = usage.get(name)
                row[name] = value if type(value) is int and value >= 0 else None
            print(json.dumps(row, sort_keys=True), flush=True)
    if codex_usage.selected_fingerprint(root) != identity:
        raise RuntimeError("Account selection changed before the experiment completed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, urllib.error.URLError, TimeoutError):
        raise SystemExit("Synthetic dynamic-cache experiment stopped; raw state withheld") from None
