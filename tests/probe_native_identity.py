"""Compare a native identity experiment against the Python controller.

Only two owned fixture processes are examined. No model, bridge key, user
process, executable path, process ID, or identity stamp is reported.
"""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from claude_fixture import isolated_environment
import process_identity


def main() -> None:
    system = {"darwin": "darwin", "linux": "linux", "win32": "windows"}.get(sys.platform)
    if not system: raise RuntimeError("This isolated experiment requires macOS, Linux or Windows")
    go = shutil.which("go")
    if not go: raise RuntimeError("Go is required; macOS also needs a C compiler")
    common = Path(__file__).with_name("native_identity_probe.go")
    source = Path(__file__).with_name("native_identity_probe_" + system + ".go")
    with tempfile.TemporaryDirectory(prefix="bridge-native-identity-") as temporary:
        root = Path(temporary)
        env = isolated_environment(root)
        env.update(GO111MODULE="off", GOTOOLCHAIN="local", GOPROXY="off", CGO_ENABLED="1" if system == "darwin" else "0",
                   GOCACHE=str(root / "go-cache"), GOPATH=str(root / "go-work"))
        suffix = ".exe" if system == "windows" else ""
        binary, unicode_binary = root / ("fixture" + suffix), root / ("fixture Ü İ Σ 中" + suffix)
        subprocess.run([go, "build", "-o", str(binary), str(common), str(source)], env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=120, check=True)
        shutil.copy2(binary, unicode_binary)
        unicode_binary.chmod(0o700)
        fixtures = [binary, unicode_binary]
        if system == "linux":
            raw_path = os.fsencode(root) + b"/fixture-\xff"
            shutil.copy2(os.fsencode(binary), raw_path)
            os.chmod(raw_path, 0o700)
            fixtures.append(raw_path)
        children = []
        readers = []
        try:
            for path in fixtures:
                program = path if isinstance(path, bytes) else str(path)
                child = subprocess.Popen([program, "--fixture-child"], env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                children.append(child)
                ready = queue.Queue()
                reader = threading.Thread(target=lambda c=child, q=ready: q.put(c.stdout.readline()), daemon=True)
                readers.append(reader)
                reader.start()
                try: message = ready.get(timeout=10).strip()
                except queue.Empty: raise RuntimeError("Owned identity fixture did not start") from None
                if message != "READY":
                    raise RuntimeError("Owned identity fixture did not start")
            first, second = children[:2]
            first_stamp = process_identity.stamp(first.pid)
            birth, executable = process_identity.capture(second.pid)
            second_stamp = process_identity.stamp(second.pid, (birth, executable))
            valid = subprocess.run([str(binary), str(second.pid), second_stamp], env=env, capture_output=True,
                                   text=True, timeout=15, check=True)
            payload = json.loads(valid.stdout)
            if payload.get("native_matches_python_identity") is not True:
                raise RuntimeError("Native and controller identities did not match")
            if system == "linux":
                raw_child = children[2]
                raw_stamp = process_identity.stamp(raw_child.pid)
                subprocess.run([str(binary), str(raw_child.pid), raw_stamp], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)
            altered_birth = ("1" if birth[0] == "0" else "0") + birth[1:]
            wrong_birth = process_identity.stamp(second.pid, (altered_birth, executable))
            wrong_path = process_identity.stamp(second.pid, (birth, executable + ".fixture-other"))
            for stamp in (first_stamp, wrong_birth, wrong_path):
                result = subprocess.run([str(binary), str(second.pid), stamp], env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                if result.returncode == 0: raise RuntimeError("Native fixture accepted a different identity")
            first.terminate()
            first.wait(timeout=5)
            gone = subprocess.run([str(binary), str(first.pid), first_stamp], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if gone.returncode == 0: raise RuntimeError("Native fixture accepted an exited process")
            payload.update(platform=system, unicode_executable_path_matches=True, different_process_rejected=True,
                           same_pid_wrong_birth_rejected=True, same_pid_wrong_executable_rejected=True,
                           exited_process_rejected=True,
                           non_utf8_executable_path_matches=True if system == "linux" else "not-run")
            print(json.dumps(payload))
        finally:
            for child in children:
                if child.poll() is None:
                    try: child.terminate()
                    except ProcessLookupError: pass
            for child in children:
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            for reader in readers: reader.join(timeout=2)
            for child in children:
                if child.stdout: child.stdout.close()


if __name__ == "__main__":
    main()
