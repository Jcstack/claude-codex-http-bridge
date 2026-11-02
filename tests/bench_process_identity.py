"""Measure process-owner checks against one disposable loopback listener.

The fixture inherits no application credentials, records no paths or process
IDs, and never talks to a model or to the user's running bridge.
"""

import argparse
import json
import math
from pathlib import Path
import platform
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claude_fixture import isolated_environment
import launcher
import process_identity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=12)
    count = parser.parse_args().samples
    if not 1 <= count <= 50: raise ValueError("Sample count must be between 1 and 50")
    with tempfile.TemporaryDirectory(prefix="bridge-identity-benchmark-") as temporary:
        env = isolated_environment(Path(temporary))
        child_code = ("import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); "
                      "s.listen(); print(s.getsockname()[1],flush=True); time.sleep(90)")
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", child_code], env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
        line = queue.Queue()
        reader = threading.Thread(target=lambda: line.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            try: port = int(line.get(timeout=10))
            except (queue.Empty, ValueError): raise RuntimeError("Fixture listener did not start") from None
            if platform.system() == "Windows":
                binary = shutil.which("netstat", path=env.get("PATH"))
                if binary is None: raise RuntimeError("netstat is required for the benchmark")
                command = [binary, "-ano", "-p", "tcp"]
                def owner():
                    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5, check=True)
                    return launcher.windows_listener_pids(result.stdout, port) == {child.pid}
            else:
                binary = shutil.which("lsof", path=env.get("PATH"))
                if binary is None: raise RuntimeError("lsof is required for the benchmark")
                command = [binary, "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"]
                def owner():
                    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5, check=True)
                    return {int(item) for item in result.stdout.split()} == {child.pid}
            if not owner(): raise RuntimeError("Fixture listener owner did not match")
            stamp = process_identity.stamp(child.pid)
            helper = ("import sys;sys.path.insert(0,sys.argv[1]);import process_identity as p;"
                      "raise SystemExit(0 if p.matches(int(sys.argv[2]),sys.argv[3]) else 1)")
            args = [sys.executable, "-I", "-B", "-c", helper,
                    str(Path(__file__).resolve().parents[1]), str(child.pid), stamp]
            def helper_matches(command_args):
                return subprocess.run(command_args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=5).returncode == 0
            if not helper_matches(args): raise RuntimeError("Fixture identity helper failed")
            values = {"listener_owner": [], "in_process_identity": [], "subprocess_identity": []}
            for _ in range(count):
                for label, check in (("listener_owner", owner),
                                     ("in_process_identity", lambda: process_identity.matches(child.pid, stamp)),
                                     ("subprocess_identity", lambda: helper_matches(args))):
                    start = time.perf_counter()
                    matched = check()
                    values[label].append((time.perf_counter() - start) * 1000)
                    if not matched: raise RuntimeError("Fixture identity check changed unexpectedly")
            if helper_matches(args[:-1] + ["0" * 64]):
                raise RuntimeError("Fixture accepted a different identity")
            def summary(samples):
                return {"median": round(statistics.median(samples), 3),
                        "p95": round(sorted(samples)[math.ceil(len(samples) * .95) - 1], 3)}
            print(json.dumps({"synthetic_local_process_only": True, "platform": platform.system(),
                "samples": count, "milliseconds": {k: summary(v) for k, v in values.items()},
                "wrong_identity_rejected": True}))
        finally:
            if child.poll() is None:
                try: child.terminate()
                except ProcessLookupError: pass
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            reader.join(timeout=2)
            child.stdout.close()


if __name__ == "__main__":
    main()
