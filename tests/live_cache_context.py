"""Probe real context acceptance and cached-prefix reuse with synthetic text.

Consumes subscription quota; large fixtures require an explicit --padding count.
Reports token counters only. Does not change any user session or service.
"""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from live_support import existing_subscription, temporary_translator
from live_compare_translators import stream
import bridge_models
import rolling_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?"); parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--managed", action="store_true", help="Use the already activated managed TLS endpoint")
    parser.add_argument("--handoff", action="store_true", help="Reload the managed backend after the first request")
    parser.add_argument("--model", default="sol")
    parser.add_argument("--padding", type=int, default=12000, help="Synthetic repeated words, not a guaranteed token count")
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--minimum-input", type=int, default=0)
    args = parser.parse_args()
    if not 1000 <= args.padding <= 800000:
        parser.error("padding must be between 1000 and 800000")
    if not args.managed and args.binary is None: parser.error("Supply a proxy binary or --managed")
    if args.handoff and not args.managed: parser.error("--handoff requires --managed")
    upstream = existing_subscription(args.root)
    model = bridge_models.aliases(upstream["models"]).get(args.model, args.model)
    if model not in upstream["models"]:
        raise RuntimeError("Requested model is absent from the live catalog")
    content = ("FIRST_MARKER=violet-73\nThe following padding is irrelevant: " + "pad " * args.padding +
               "\nLAST_MARKER=amber-29\nReturn only the two marker values separated by one comma, first then last.")
    with ExitStack() as stack:
        ca = None
        if args.managed:
            managed = rolling_runtime.state(args.root)
            if managed is None: raise RuntimeError("Managed runtime is not activated")
            rolling_runtime.endpoint_status(args.root, managed)
            port, key = managed["port"], rolling_runtime.private_key(args.root / "runtime-managed" / "front-key")
            ca = args.root / "runtime-managed" / "ca.pem"
        else:
            port, key = stack.enter_context(temporary_translator(args.binary.resolve(), upstream, model))
        # Match Claude Code's stable per-session identity. A bare Messages
        # request without this identity is not a faithful cache-routing probe.
        metadata = {"user_id": json.dumps({"session_id": str(uuid.uuid4())})}
        for index in range(args.repeats):
            answer, metric = stream(port, key, {"model": model, "max_tokens": 100,
                "metadata": metadata,
                "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"},
                "messages": [{"role": "user", "content": content + f"\nProbe iteration {index + 1}."}]}, ca_file=ca)
            text = "".join(block.get("text", "") for block in answer).strip()
            if text != "violet-73,amber-29":
                raise RuntimeError("Synthetic boundary markers were not retained exactly")
            usage = metric["usage"]
            total = sum(usage.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if total < args.minimum_input:
                raise RuntimeError("Reported input did not reach the requested acceptance boundary")
            print(json.dumps({"model": model, "repeat": index + 1, "markers": "pass", "managed": args.managed,
                              "total_input_tokens": total, "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                              "catalog_context": upstream["models"][model].get("context_window"), **metric}), flush=True)
            if args.handoff and index == 0:
                environment = {**os.environ, "CLAUDE_CODEX_BRIDGE_HOME": str(args.root)}
                result = subprocess.run([sys.executable, str(args.root / "launcher.py"), "bridge", "reload"],
                                        env=environment, capture_output=True, timeout=60)
                if result.returncode: raise RuntimeError("Managed cache-probe handoff failed")
                after = rolling_runtime.state(args.root)
                if (after["pid"], after["port"]) != (managed["pid"], managed["port"]):
                    raise RuntimeError("Stable endpoint changed during cache probe")


if __name__ == "__main__":
    main()
