"""Restart the private subscription bridge at login without replaying requests."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import plistlib
import shutil
import subprocess
import sys
import time

# Isolated Python deliberately does not add the script's directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_profile
import launcher
import rolling_runtime
import runtime_bundle


INTERVAL_SECONDS = 120


@contextmanager
def operation_lock(root: Path, *, wait_seconds: float = 60, watcher: bool = False):
    runtime = root / "runtime"
    runtime_bundle.private_dir(runtime)
    path = runtime / ("autostart-watcher.lock" if watcher else "autostart.lock")
    if path.is_symlink(): raise RuntimeError("Autostart lock must not be a symlink")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        if os.name == "nt":
            import windows_acl
            windows_acl.lock([path])
            import msvcrt
        else:
            import fcntl
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another bridge start or stop is in progress") from None
                time.sleep(.1)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)


def pause(root: Path) -> None:
    api_profile.write_private(root / "runtime" / "autostart-paused", "paused\n")


def unpause(root: Path) -> None:
    path = root / "runtime" / "autostart-paused"
    if path.is_symlink(): raise RuntimeError("Autostart pause marker must not be a symlink")
    path.unlink(missing_ok=True)


def healthy(root: Path) -> bool:
    """Check only owned loopback listeners and metadata; do not call a model."""
    with launcher.service_lock() as runtime:
        state = launcher.read_state(runtime)
        if state is None or not launcher.state_is_live(runtime, state): return False
        if not (root / "runtime-selected.json").exists(): return True
        managed = rolling_runtime.state(root)
        if managed is None: return False
        rolling_runtime.endpoint_status(root, managed)
        directory = root / "runtime-managed"
        backend = rolling_runtime.read(directory / "backend.json")
        origin = rolling_runtime.read(directory / "origin.json")
        current = managed["generations"][managed["current"]]
        return (backend.get("generation") == managed["current"] and
                backend.get("pid") == current["pid"] and backend.get("port") == current["port"] and
                launcher.listener_pids(current["port"]) == {current["pid"]} and
                origin.get("pid") == state["pid"] and origin.get("port") == state["port"])


def record(root: Path, result: str) -> None:
    if result not in ("healthy", "recovered", "paused", "failed"):
        raise ValueError("Invalid autostart result")
    api_profile.write_private(root / "runtime" / "autostart-status.json",
                              json.dumps({"result": result, "time": int(time.time())}) + "\n")


def run_once(root: Path) -> int:
    """A quiet bounded login/health check; no raw errors or payloads are logged."""
    try:
        with operation_lock(root, wait_seconds=0):
            if (root / "runtime" / "autostart-paused").exists():
                record(root, "paused")
                return 0
            try:
                ready = healthy(root)
            except (OSError, RuntimeError, ValueError, KeyError):
                ready = False
            if ready:
                record(root, "healthy")
                return 0
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                launcher.helper_output("verify")
                launcher.bridge_start()
            if not healthy(root): raise RuntimeError("Bridge did not become healthy")
            record(root, "recovered")
            return 0
    except Exception:
        # Do not persist exception text: transports can include private paths
        # or server responses. Manual bridge start gives an actionable error.
        try: record(root, "failed")
        except Exception: pass
        return 1


def label(root: Path) -> str:
    identity = hashlib.sha256(str(root.absolute()).encode()).hexdigest()[:16]
    return "org.claude-codex-http-bridge." + identity


def python_command() -> str:
    candidate = shutil.which("python3")
    if candidate:
        try:
            if Path(candidate).is_absolute() and os.path.samefile(candidate, sys.executable): return candidate
        except OSError:
            pass
    return sys.executable


def agent_definition(root: Path, python: str | None = None) -> bytes:
    python = python or python_command()
    if not Path(python).is_absolute(): raise RuntimeError("Autostart requires an absolute Python path")
    path = os.pathsep.join(dict.fromkeys([str(Path(python).parent), "/opt/homebrew/bin", "/usr/local/bin",
                                         "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    return plistlib.dumps({"Label": label(root),
        "ProgramArguments": [python, "-I", "-B", str(root / "autostart.py"), "run", "--root", str(root)],
        "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": path},
        "RunAtLoad": True, "StartInterval": INTERVAL_SECONDS,
        "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}, sort_keys=True)


def agent_path(root: Path) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / (label(root) + ".plist")


def agent_is_ours(path: Path, root: Path) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536: return False
    try:
        data = plistlib.loads(path.read_bytes())
        args = data["ProgramArguments"]
        return (isinstance(args, list) and len(args) == 7 and isinstance(args[0], str) and
                Path(args[0]).is_absolute() and data == plistlib.loads(agent_definition(root, args[0])))
    except (ValueError, TypeError, KeyError, OSError):
        return False


def launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *arguments], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def agent_loaded(root: Path) -> bool:
    return launchctl("print", f"gui/{os.getuid()}/{label(root)}").returncode == 0


WINDOWS_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"


def windows_pythonw() -> str:
    executable = Path(sys.executable)
    candidate = executable if executable.name.lower() == "pythonw.exe" else executable.with_name("pythonw.exe")
    if not candidate.is_absolute() or not candidate.is_file():
        raise RuntimeError("A Python installation with pythonw.exe is required for Windows login recovery")
    return str(candidate)


def windows_command(root: Path, pythonw: str | None = None) -> str:
    return subprocess.list2cmdline([pythonw or windows_pythonw(), "-I", "-B", str(root / "autostart.py"),
                                    "watch", "--root", str(root)])


def windows_is_ours(root: Path, value: object) -> bool:
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n\x00"):
        return False
    if value.startswith('"'):
        end = value.find('"', 1)
        if end < 0: return False
        executable = value[1:end]
    else:
        executable = value.partition(" ")[0]
    candidate = PureWindowsPath(executable)
    return candidate.is_absolute() and candidate.name.lower() == "pythonw.exe" and value == windows_command(root, executable)


def windows_value(root: Path) -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN, 0, winreg.KEY_QUERY_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, label(root))
    except FileNotFoundError:
        return None
    if kind != winreg.REG_SZ or not isinstance(value, str):
        raise RuntimeError("An unrelated Windows startup value uses this name")
    return value


def windows_write(root: Path, value: str | None) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, WINDOWS_RUN, 0, winreg.KEY_SET_VALUE) as key:
        if value is None:
            try: winreg.DeleteValue(key, label(root))
            except FileNotFoundError: pass
        else:
            winreg.SetValueEx(key, label(root), 0, winreg.REG_SZ, value)


def enable_windows(root: Path) -> None:
    command = windows_command(root)
    old = windows_value(root)
    if old is not None and not windows_is_ours(root, old):
        raise RuntimeError("An unrelated Windows startup value uses this name")
    windows_write(root, command)
    try:
        with operation_lock(root): unpause(root)
        executable = windows_pythonw()
        subprocess.Popen([executable, "-I", "-B", str(root / "autostart.py"), "watch", "--root", str(root)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=root, env=rolling_runtime.process_environment(), close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    except BaseException:
        if windows_value(root) == command: windows_write(root, old)
        raise


def disable_windows(root: Path) -> None:
    value = windows_value(root)
    if value is not None and not windows_is_ours(root, value):
        raise RuntimeError("An unrelated Windows startup value uses this name")
    if value is not None: windows_write(root, None)


def windows_watch(root: Path) -> int:
    try:
        with operation_lock(root, wait_seconds=0, watcher=True):
            while windows_is_ours(root, windows_value(root)):
                run_once(root)
                time.sleep(INTERVAL_SECONDS)
    except RuntimeError:
        # Another watcher already owns this installation, or the startup
        # setting became invalid. In neither case should we replace a process.
        return 0
    return 0


def enable(root: Path) -> None:
    if platform.system() == "Windows":
        enable_windows(root)
        return
    if platform.system() != "Darwin":
        raise RuntimeError("Automatic login startup is available on macOS and Windows; use bridge start on this platform")
    path = agent_path(root)
    if path.parent.is_symlink(): raise RuntimeError("LaunchAgents directory must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    if path.exists() or path.is_symlink():
        if not agent_is_ours(path, root): raise RuntimeError("An unrelated login agent uses this path")
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle: handle.write(agent_definition(root))
        created = True
    try:
        if not agent_loaded(root):
            if launchctl("bootstrap", f"gui/{os.getuid()}", str(path)).returncode != 0 or not agent_loaded(root):
                raise RuntimeError("Could not load the user login agent; check that a graphical macOS login is active")
    except BaseException:
        if created: path.unlink(missing_ok=True)
        raise
    with operation_lock(root): unpause(root)


def disable(root: Path) -> None:
    if platform.system() == "Windows":
        disable_windows(root)
        return
    if platform.system() != "Darwin": raise RuntimeError("Automatic login startup is not installed on this platform")
    path = agent_path(root)
    if path.exists() or path.is_symlink():
        if not agent_is_ours(path, root): raise RuntimeError("An unrelated login agent uses this path")
        if agent_loaded(root) and launchctl("bootout", f"gui/{os.getuid()}/{label(root)}").returncode != 0:
            raise RuntimeError("Could not unload the user login agent")
        path.unlink()


def main(arguments: list[str], *, root: Path) -> int:
    if len(arguments) != 1 or arguments[0] not in ("enable", "disable", "status"):
        raise ValueError("Usage: claude-codex autostart enable | status | disable")
    action = arguments[0]
    if action == "enable":
        enable(root)
        print("Login recovery enabled for this user; a stopped bridge stays paused until started again")
    elif action == "disable":
        with operation_lock(root): disable(root)
        print("Login recovery disabled; running bridge sessions were left untouched")
    else:
        system = platform.system()
        path = agent_path(root) if system == "Darwin" else None
        installed = (windows_is_ours(root, windows_value(root)) if system == "Windows"
                     else bool(path and agent_is_ours(path, root)))
        last = root / "runtime" / "autostart-status.json"
        result = {"installed": installed, "loaded": (agent_loaded(root) if installed and system == "Darwin"
                                                      else None if installed and system == "Windows" else False),
                  "paused": (root / "runtime" / "autostart-paused").exists()}
        if last.is_file() and not last.is_symlink():
            runtime_bundle.safe_file(last, limit=4096)
            state = json.loads(last.read_text())
            if state.get("result") in ("healthy", "recovered", "paused", "failed") and type(state.get("time")) is int:
                result["last"] = state
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in ("run", "watch") or sys.argv[2] != "--root":
        raise SystemExit(2)
    root = Path(sys.argv[3]).expanduser().absolute()
    if root.resolve() != Path(__file__).resolve().parent:
        raise SystemExit(2)
    launcher.ROOT = root
    if sys.argv[1] == "watch":
        if platform.system() != "Windows": raise SystemExit(2)
        raise SystemExit(windows_watch(root))
    raise SystemExit(run_once(root))
