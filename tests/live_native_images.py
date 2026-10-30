"""Probe native subscription image generation without publishing image data.

Consumes quota. Emits only metadata and fails the size contract when the actual
PNG dimensions differ from the request. Never resizes a mismatched result.
"""

import argparse
import base64
import json
from pathlib import Path
import re
import ssl
import struct
import time
import urllib.error
import urllib.request

import api_profile
import rolling_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--model", default="gpt-image-2.5-flare")
    parser.add_argument("--size", default="1024x1024")
    args = parser.parse_args(); root = args.root.expanduser().resolve()
    if not re.fullmatch(r"[1-9][0-9]{1,3}x[1-9][0-9]{1,3}", args.size): parser.error("Use an explicit WIDTHxHEIGHT")
    state = rolling_runtime.state(root)
    if state is None: raise RuntimeError("An activated managed runtime is required")
    rolling_runtime.endpoint_status(root, state)
    runtime = root / "runtime-managed"; key = rolling_runtime.private_key(runtime / "front-key")
    models = rolling_runtime.get_json(runtime, state["port"], "/v1/models", key)
    if args.model not in {item.get("id") for item in models.get("data", [])}:
        raise RuntimeError("Requested image model is absent from the authenticated catalog")
    payload = {"model": args.model, "prompt": "A plain blue square centered on a white background. No text.",
               "size": args.size, "quality": "low", "n": 1, "output_format": "png"}
    context = ssl.create_default_context(cafile=str(runtime / "ca.pem"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context), api_profile.NoRedirect())
    request = urllib.request.Request(f'https://127.0.0.1:{state["port"]}/v1/images/generations',
                                    data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + key,
                                    "Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=180) as response: raw = response.read(32 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        text = error.read(20000).decode(errors="replace").lower()
        print(json.dumps({"http_status": error.code, "classifiers": [x for x in (
            "quota", "rate_limit", "permission", "unsupported", "model_not_found", "tls", "eof", "invalid_request", "quality") if x in text]}))
        raise SystemExit(1) from None
    if len(raw) > 32 * 1024 * 1024: raise RuntimeError("Image response exceeded fixture limit")
    items = json.loads(raw).get("data", [])
    if len(items) != 1 or not isinstance(items[0].get("b64_json"), str): raise RuntimeError("Expected one encoded image")
    image = base64.b64decode(items[0]["b64_json"], validate=True)
    if image[:8] != b"\x89PNG\r\n\x1a\n": raise RuntimeError("Expected a PNG artifact")
    width, height = struct.unpack(">II", image[16:24])
    matches = args.size == f"{width}x{height}"
    print(json.dumps({"subscription_only": True, "image_generation": "pass", "model": args.model,
                      "requested_size": args.size, "actual_size": f"{width}x{height}", "bytes": len(image),
                      "seconds": round(time.monotonic()-started, 3), "size_contract": "pass" if matches else "fail"}))
    if not matches: raise SystemExit(1)


if __name__ == "__main__": main()
