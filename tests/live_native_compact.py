"""Measure native compact/recall using synthetic state and the subscription.

Consumes quota. Never reads user transcripts, prints native state, or changes a
user session. Native compact availability is distinct from Claude /compact.
"""

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_client import NativeClient, NativeRouteError
from live_support import existing_subscription
from live_compaction import INITIAL, UPDATES
import bridge_models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--model", default="sol")
    args = parser.parse_args()
    upstream = existing_subscription(args.root)
    model = bridge_models.aliases(upstream["models"]).get(args.model, args.model)
    if model not in upstream["models"]:
        raise RuntimeError("Requested model is absent from the authenticated catalog")
    client = NativeClient(args.root)
    observations = "\n".join(f"Historical sample {i:04d}: parser fixture {i % 37} completed; latency bucket {i % 13}; no configuration change."
                             for i in range(600))
    original = [{"role": "user", "content": "Remember the exact authoritative configuration: " + json.dumps(INITIAL) + "\n" + observations},
                {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "Recorded."}]},
                {"role": "user", "content": "Replace only these values; preserve every other key: " + json.dumps(UPDATES)},
                {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "Updated."}]}]
    key = str(uuid.uuid4())
    started = time.monotonic()
    try:
        result = client.json("/v1/responses/compact", {"model": model, "input": original,
            "instructions": "Preserve the exact latest configuration, restrictions, unresolved issue and next action. Omit historical observations.",
            "reasoning": {"effort": "max"}, "prompt_cache_key": key})
    except NativeRouteError as error:
        print(json.dumps({"stage": "native_compact", "available": False, "status": error.status,
                          "category": error.category, "seconds": round(time.monotonic()-started, 3)}))
        raise SystemExit(3) from None
    output = result.get("output", [])
    compactions = [item for item in output if isinstance(item, dict) and item.get("type") == "compaction" and item.get("encrypted_content")]
    if not compactions:
        raise RuntimeError("Native compaction did not return continuation state")
    numeric = lambda usage: {k: value for k, value in usage.items() if k in ("input_tokens", "output_tokens", "total_tokens") and type(value) is int}
    print(json.dumps({"stage": "native_compact", "model": model, "effort": "max",
                      "seconds": round(time.monotonic()-started, 3), "compaction_items": len(compactions),
                      "usage": numeric(result.get("usage", {}))}), flush=True)
    started = time.monotonic()
    final = None
    for event in client.events("/v1/responses", {"model": model, "store": False, "prompt_cache_key": key,
        "reasoning": {"effort": "max"},
        "input": [*output, {"role": "user", "content": "Return only a JSON object with all twelve current configuration keys and string values. No markdown."}]}):
        if event.get("type") in ("error", "response.failed", "response.incomplete"):
            raise RuntimeError("Native continuation returned an error; raw response withheld")
        if event.get("type") == "response.completed": final = event.get("response", {})
    if final is None: raise RuntimeError("Native continuation did not complete")
    text = "".join(part.get("text", "") for item in final.get("output", []) if item.get("type") == "message"
                   for part in item.get("content", []) if part.get("type") == "output_text")
    try: answer = json.loads(text)
    except ValueError: answer = None
    expected = {**INITIAL, **UPDATES}
    correct = sum(isinstance(answer, dict) and answer.get(name) == value for name, value in expected.items())
    print(json.dumps({"stage": "native_recall", "model": model, "seconds": round(time.monotonic()-started, 3),
                      "state_fields_correct": correct, "state_fields_total": len(expected), "pass": correct == len(expected),
                      "usage": numeric(final.get("usage", {}))}), flush=True)
    if correct != len(expected): raise SystemExit(1)


if __name__ == "__main__": main()
