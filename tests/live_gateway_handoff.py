"""Verify isolated rolling backends against the existing Codex subscription.

Uses synthetic prompts and consumes quota. Does not change or stop the existing
service. The two disposable translators receive only its localhost client key.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid

from live_support import existing_subscription, temporary_translator
from live_compare_translators import stream
import bridge_models
import launcher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path); parser.add_argument("ingress", type=Path)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    upstream = existing_subscription(args.root)
    model = bridge_models.aliases(upstream["models"])["sol"]
    with temporary_translator(args.binary.resolve(), upstream, model) as a, \
         temporary_translator(args.binary.resolve(), upstream, model) as b, \
         tempfile.TemporaryDirectory(prefix="bridge-handoff-") as temporary:
        root = Path(temporary); root.chmod(0o700)
        if os.name == "nt":
            import windows_acl
            windows_acl.lock([root])
        front_key = secrets.token_hex(32)
        front_file = root / "front-key"; front_file.write_text(front_key); front_file.chmod(0o600)
        manifest = root / "backend.json"
        configs = {}
        for name, (port, key) in (("a", a), ("b", b)):
            owners = launcher.listener_pids(port)
            if len(owners) != 1: raise RuntimeError("Disposable listener ownership is ambiguous")
            key_file = root / (name + "-key"); key_file.write_text(key); key_file.chmod(0o600)
            configs[name] = {"generation": name, "port": port, "pid": owners.pop(), "key_file": str(key_file)}

        def switch(name):
            staged = root / "pending.json"
            staged.write_text(json.dumps(configs[name])); staged.chmod(0o600)
            os.replace(staged, manifest)

        switch("a")
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]
        ingress = subprocess.Popen([str(args.ingress.resolve()), "--root", str(root), "--listen", f"127.0.0.1:{port}",
            "--key-file", str(front_file), "--backend-file", str(manifest)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def status():
            request = urllib.request.Request(f"http://127.0.0.1:{port}/bridge/status",
                                             headers={"Authorization": "Bearer " + front_key})
            with opener.open(request, timeout=5) as response: return json.load(response)

        try:
            launcher.wait_for_private_listener(ingress, port)
            metadata = {"user_id": json.dumps({"session_id": str(uuid.uuid4())})}
            padding = "FIRST_MARKER=violet-73\n" + "pad " * 12000 + "\nLAST_MARKER=amber-29"
            base = {"model": model, "metadata": metadata, "max_tokens": 200,
                    "thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}

            def cache_call(n):
                content, metric = stream(port, front_key, {**base, "messages": [{"role": "user", "content":
                    padding + f"\nReturn only both marker values separated by one comma. Iteration {n}."}]})
                if "".join(b.get("text", "") for b in content).strip() != "violet-73,amber-29":
                    raise RuntimeError("Marker verification failed across handoff")
                return metric

            warm = cache_call(1)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(cache_call, 2)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    snapshot = status()
                    if snapshot.get("active", {}).get("a", 0) > 0: break
                    if pending.done(): raise RuntimeError("Request finished before handoff admission barrier")
                    time.sleep(.02)
                else: raise RuntimeError("Could not observe admitted request")
                switch("b")
                after = cache_call(3)
                during = pending.result(timeout=120)
            if status().get("active", {}).get("a", 0) != 0:
                raise RuntimeError("Old generation did not drain")
            print(json.dumps({"stage": "cache_handoff", "old_stream_completed": True,
                              "old_generation_drained": True, "warm": warm, "during": during, "after": after}), flush=True)

            switch("a")
            tools = [{"name": "fixture_lookup", "input_schema": {"type": "object", "properties": {
                "key": {"type": "string"}}, "required": ["key"]}}]
            base.update(tools=tools, output_config={"effort": "high"})
            expected_key = str(pow(17, 11, 97))
            messages = [{"role": "user", "content": "Compute 17 to the power of 11 modulo 97 without external tools. Then call fixture_lookup with key equal to that remainder as a decimal string. After the call, report its exact result."}]
            content, first = stream(port, front_key, {**base, "messages": messages,
                "tool_choice": {"type": "tool", "name": "fixture_lookup"}})
            calls = [b for b in content if b.get("type") == "tool_use"]
            if len(calls) != 1 or calls[0].get("input") != {"key": expected_key}:
                raise RuntimeError("Handoff tool fixture was not called correctly")
            reasoning = [b for b in content if b.get("type") == "thinking" and b.get("signature")
                         and not b["signature"].startswith("codex_")]
            switch("b")
            messages.extend([{"role": "assistant", "content": content}, {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": calls[0]["id"], "content": "VIOLET_73"}]}])
            answer, second = stream(port, front_key, {**base, "messages": messages, "tool_choice": {"type": "none"}})
            if "VIOLET_73" not in "".join(b.get("text", "") for b in answer):
                raise RuntimeError("Handoff tool result was not retained")
            print(json.dumps({"stage": "state_handoff", "tool_result_continuation": "pass",
                              "reasoning_signatures_replayed": len(reasoning), "call": first, "continuation": second}), flush=True)
        finally:
            launcher.stop_process(ingress)


if __name__ == "__main__":
    main()
