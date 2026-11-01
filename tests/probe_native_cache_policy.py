"""Synthetic subscription-cache discovery, never a manual-policy certificate.

Consumes quota. No user history, credential values or raw replies are printed.
"""

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge_models
from native_client import NativeClient, NativeRouteError
from live_support import existing_subscription


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--model", default="sol")
    parser.add_argument("--include-controls", action="store_true")
    args = parser.parse_args()
    catalog = existing_subscription(args.root)["models"]
    model = bridge_models.aliases(catalog).get(args.model, args.model)
    if model not in catalog or not bridge_models.eligible(catalog[model]):
        raise RuntimeError("Requested model is absent or ineligible")
    client = NativeClient(args.root)
    cases = ["automatic"]
    if args.include_controls:
        cases += ["implicit", "explicit_without_breakpoint", "explicit_with_breakpoint"]
    for mode in cases:
        identity = str(uuid.uuid4())
        reference = "Synthetic reference " + identity + "\n" + "\n".join(f"item{i:04}=VALUE_{i*17:04}" for i in range(256))
        block = {"type": "input_text", "text": reference}
        if mode == "explicit_with_breakpoint": block["prompt_cache_breakpoint"] = {"mode": "explicit"}
        payload = {"model": model, "instructions": "Use the synthetic reference. Reply only with the exact requested value.",
            "reasoning": {"effort": "low"}, "store": False, "stream": True, "prompt_cache_key": identity}
        if mode != "automatic":
            payload["prompt_cache_options"] = {"mode": "implicit" if mode == "implicit" else "explicit", "ttl": "30m"}
        for iteration, index in enumerate((255, 128), 1):
            body = {**payload, "input": [{"role": "developer", "content": [block]},
                {"role": "user", "content": f"Return the value of item{index:04} only."}]}
            started = time.monotonic(); first = None; response = None; text = []; failed = False
            try:
                for event in client.events("/v1/responses", body):
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        if first is None: first = time.monotonic() - started
                        text.append(event.get("delta", ""))
                    if kind in {"error", "response.failed", "response.incomplete"}: failed = True
                    if kind == "response.completed": response = event.get("response", {})
            except NativeRouteError as error:
                print(json.dumps({"case": mode, "repeat": iteration, "http_status": error.status,
                                  "category": error.category, "raw_error_withheld": True}), flush=True)
                break
            if response is None or failed: raise RuntimeError("Synthetic cache stream did not complete")
            answer = "".join(text).strip()
            if not answer:
                answer = "".join(part.get("text", "") for item in response.get("output", []) if isinstance(item, dict)
                                 for part in item.get("content", []) if isinstance(part, dict)).strip()
            if answer != f"VALUE_{index*17:04}": raise RuntimeError("Synthetic cache reference failed")
            usage = response.get("usage", {}); details = usage.get("input_tokens_details", {})
            metric = {"case": mode, "repeat": iteration, "answer_correct": True,
                "seconds": round(time.monotonic() - started, 3), "first_text_seconds": round(first, 3) if first is not None else None,
                "manual_controls_proven_by_this_response": False}
            metric.update({key: usage[key] for key in ("input_tokens", "output_tokens") if type(usage.get(key)) is int})
            metric["input_detail_counters"] = {key: value for key, value in details.items() if key in {
                "cached_tokens", "cache_write_tokens", "cache_creation_tokens", "uncached_tokens",
                "cache_write_tokens_30m", "cache_write_tokens_12h"} and type(value) is int}
            print(json.dumps(metric), flush=True)


if __name__ == "__main__":
    try: main()
    except (OSError, RuntimeError, ValueError): raise SystemExit("Synthetic cache audit failed; raw state withheld") from None
