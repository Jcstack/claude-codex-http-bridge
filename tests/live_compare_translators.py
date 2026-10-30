"""Small, content-free metric report for pin/candidate on one subscription route.

Consumes existing subscription quota. Never starts/stops the active service.
Both arms have the same additional localhost hop. This is a tool-loop probe,
not a coding-quality benchmark. Raw synthetic responses remain in memory only.
"""

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.request

from live_support import existing_subscription, temporary_translator
import bridge_models


def stream(port, key, payload, *, ca_file=None):
    scheme = "https" if ca_file is not None else "http"
    request = urllib.request.Request(f"{scheme}://127.0.0.1:{port}/v1/messages",
                                     data=json.dumps({**payload, "stream": True}).encode(),
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json",
                                              "anthropic-version": "2023-06-01"})
    start = time.monotonic()
    events = []
    handlers = [urllib.request.ProxyHandler({})]
    if ca_file is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca_file))))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=120) as response:
            for line in response:
                if line.startswith(b"data:"):
                    raw = line[5:].strip()
                    if raw and raw != b"[DONE]":
                        events.append((time.monotonic() - start, json.loads(raw)))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Synthetic stream returned HTTP {error.code}") from None
    if not events or events[-1][1].get("type") != "message_stop" or any(e.get("type") == "error" for _, e in events):
        raise RuntimeError("Incomplete synthetic stream")
    blocks, usage, model = {}, {}, None
    deltas = []
    for at, event in events:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message", {})
            usage.update(message.get("usage", {})); model = message.get("model")
        elif kind == "message_delta":
            usage.update(event.get("usage", {}))
        elif kind == "content_block_start":
            blocks[event["index"]] = dict(event["content_block"])
        elif kind == "content_block_delta":
            delta = event.get("delta", {}); block = blocks[event["index"]]
            if delta.get("type") == "input_json_delta":
                block["_arguments"] = block.get("_arguments", "") + delta["partial_json"]
            elif delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + delta["text"]
            elif delta.get("type") == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta["thinking"]
            elif delta.get("type") == "signature_delta":
                block["signature"] = block.get("signature", "") + delta["signature"]
            if delta.get("type") in ("text_delta", "input_json_delta", "thinking_delta"):
                deltas.append(at)
    for block in blocks.values():
        if "_arguments" in block:
            block["input"] = json.loads(block.pop("_arguments"))
    numeric_usage = {k: v for k, v in usage.items() if k in (
        "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens") and type(v) is int}
    metrics = {"seconds": round(events[-1][0], 3),
               "first_delta_seconds": round(deltas[0], 3) if deltas else None,
               "max_delta_gap_seconds": round(max((b-a for a,b in zip(deltas,deltas[1:])), default=0), 3),
               "events": len(events), "usage": numeric_usage, "model_matches": model == payload["model"]}
    return list(blocks.values()), metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pin", type=Path); parser.add_argument("candidate", type=Path)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    upstream = existing_subscription(args.root)
    model = bridge_models.aliases(upstream["models"]).get("sol")
    if model is None:
        raise RuntimeError("No eligible Sol model")
    tools = [{"name": "fixture_lookup", "description": "Return the synthetic value for a key",
              "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}]
    base = {"model": model, "max_tokens": 300, "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"}, "tools": tools}
    with ExitStack() as stack:
        routes = {name: stack.enter_context(temporary_translator(binary.resolve(), upstream, model))
                  for name, binary in (("pin", args.pin), ("candidate", args.candidate))}
        print(json.dumps({"model": model, "effort": "high", "binary_sha256": {
            name: hashlib.sha256(binary.read_bytes()).hexdigest()
            for name, binary in (("pin", args.pin), ("candidate", args.candidate))}}), flush=True)
        for repeat, order in [(1, ("pin", "candidate")), (2, ("candidate", "pin"))]:
            for name in order:
                port, key = routes[name]
                messages = [{"role": "user", "content": "Call fixture_lookup with key violet. Then use its result."}]
                content, first = stream(port, key, {**base, "messages": messages,
                                                  "tool_choice": {"type": "tool", "name": "fixture_lookup"}})
                calls = [b for b in content if b.get("type") == "tool_use"]
                if len(calls) != 1 or calls[0].get("name") != "fixture_lookup" or calls[0].get("input") != {"key": "violet"}:
                    raise RuntimeError("Synthetic tool call failed")
                messages.extend([{"role": "assistant", "content": calls},
                                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls[0]["id"], "content": "VIOLET_73"}]},
                                 {"role": "user", "content": "Reply with the exact tool result only."}])
                answer, second = stream(port, key, {**base, "messages": messages, "tool_choice": {"type": "none"}})
                if "".join(b.get("text", "") for b in answer).strip() != "VIOLET_73":
                    raise RuntimeError("Synthetic result continuation failed")
                print(json.dumps({"arm": name, "repeat": repeat, "tool_call": "pass", "continuation": "pass",
                                  "call_metrics": first, "continuation_metrics": second}), flush=True)


if __name__ == "__main__":
    main()
