#!/usr/bin/env python3
"""Run Claude Code against a private localhost model service."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager


ROOT = Path(os.environ.get("CLAUDE_CODEX_BRIDGE_HOME", Path(__file__).resolve().parent)).expanduser().absolute()
PROXY = ROOT / "bin" / ("cli-proxy-api.exe" if platform.system() == "Windows" else "cli-proxy-api")
HELPER = ROOT / "bridge_models.py"


def parse_args(arguments: list[str]) -> tuple[str, str, list[str]]:
    if arguments and arguments[0] == "--help":
        return "help", "", []
    if arguments and arguments[0] in ("codex-login", "models", "--list-models"):
        if len(arguments) != 1:
            raise ValueError(f"{arguments[0]} does not take arguments")
        return ("login" if arguments[0] == "codex-login" else "models"), "", []
    if arguments and arguments[0] == "bridge":
        if len(arguments) != 2 or arguments[1] not in ("start", "status", "stop"):
            raise ValueError("Usage: claude-codex bridge start | status | stop")
        return "bridge-" + arguments[1], "", []
    selected = "sol"
    claude_args: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--model":
            if index + 1 == len(arguments):
                raise ValueError("Missing value after --model")
            selected = arguments[index + 1]
            index += 2
        elif argument.startswith("--model="):
            selected = argument.partition("=")[2]
            index += 1
        elif argument == "--":
            claude_args.extend(arguments[index:])
            break
        else:
            claude_args.append(argument)
            index += 1
    if not selected:
        raise ValueError("Model ID cannot be empty")
    return "claude", selected, claude_args


def helper_output(*arguments: str, environment: dict[str, str] | None = None) -> str:
    return subprocess.check_output(
        [sys.executable, str(HELPER), *arguments],
        env=environment,
        text=True,
    ).strip()


def windows_listener_pids(output: str, port: int) -> set[int]:
    owners = set()
    target = f"127.0.0.1:{port}"
    for line in output.splitlines():
        fields = line.split()
        if (len(fields) >= 5 and fields[0].upper() == "TCP" and
            fields[1] == target and fields[2] == "0.0.0.0:0" and fields[-1].isdigit()):
            owners.add(int(fields[-1]))
    return owners


def listener_pids(port: int) -> set[int]:
    if platform.system() == "Windows":
        output = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True, errors="replace")
        return windows_listener_pids(output, port)
    if shutil.which("lsof") is None:
        raise RuntimeError("lsof is required to verify the local proxy listener")
    result = subprocess.run(
        ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    return {int(line) for line in result.stdout.splitlines() if line.isdigit()}


def wait_for_private_listener(proxy: subprocess.Popen, port: int) -> None:
    for _ in range(50):
        if proxy.poll() is not None:
            raise RuntimeError("Local model proxy exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.2)
    else:
        raise RuntimeError("Local model proxy did not become ready")
    owners = listener_pids(port)
    if owners != {proxy.pid}:
        raise RuntimeError("Local model port is not owned exclusively by this session's proxy")


def wait_for_catalog(proxy: subprocess.Popen, environment: dict[str, str], require_eligible: bool) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proxy.poll() is not None:
            raise RuntimeError("Local model proxy exited before its model catalog was ready")
        result = subprocess.run(
            [sys.executable, str(HELPER), "catalog"], env=environment,
            capture_output=True, text=True, timeout=8,
        )
        if result.returncode == 0:
            counts = json.loads(result.stdout)
            if counts.get("eligible" if require_eligible else "chat", 0) > 0:
                return
        time.sleep(0.3)
    raise RuntimeError("No Codex models appeared in the local catalog; run claude-codex codex-login")


def claude_environment(port: int, key: str, model: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "DISABLE_COMPACT", "CLAUDE_CODE_EXTRA_BODY",
    ):
        environment.pop(name, None)
    environment.update({
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
        "ENABLE_TOOL_SEARCH": "true",
    })
    return environment


def configured_status_line() -> dict | None:
    if os.environ.get("CLAUDE_CODEX_STATUSLINE_ADAPTER") == "0":
        return None
    locations = os.environ.get("CLAUDE_CONFIG_DIR")
    roots = [Path(part) for part in locations.split(os.pathsep) if part] if locations else [Path.home() / ".claude"]
    for root in roots:
        settings_file = root / "settings.json"
        if not settings_file.is_file():
            continue
        try:
            settings = json.loads(settings_file.read_text())
        except (OSError, ValueError):
            continue
        status_line = settings.get("statusLine") if isinstance(settings, dict) else None
        if (isinstance(status_line, dict) and status_line.get("type") == "command" and
            isinstance(status_line.get("command"), str) and status_line["command"].strip() and
            "statusline_adapter.py" not in status_line["command"]):
            return status_line
    return None


def private_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.write("\n")
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([path])


def session_settings(base: Path, windows: dict[str, int], directory: Path) -> tuple[Path, list[Path]]:
    status_line = configured_status_line()
    if status_line is None:
        return base, []
    profile_path = directory / "statusline-profile.json"
    settings_path = directory / "settings.json"
    private_json(profile_path, {"context_windows": windows})
    if platform.system() == "Windows":
        python_command = "py -3" if shutil.which("py") else "python"
        command = f'{python_command} "{(ROOT / "statusline_adapter.py").as_posix()}" "{profile_path.as_posix()}"'
    else:
        command = shlex.join([sys.executable, str(ROOT / "statusline_adapter.py"), str(profile_path)])
    settings = json.loads(base.read_text())
    settings["statusLine"] = {**status_line, "command": command + " | " + status_line["command"]}
    private_json(settings_path, settings)
    return settings_path, [profile_path, settings_path]


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def runtime_directory() -> Path:
    directory = ROOT / "runtime"
    if directory.is_symlink():
        raise RuntimeError("Runtime directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([directory])
    return directory


@contextmanager
def service_lock():
    directory = runtime_directory()
    path = directory / "service.lock"
    if path.is_symlink():
        raise RuntimeError("Service lock must not be a symlink")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        if os.fstat(descriptor).st_size == 0:
            handle.write(b"0")
        handle.seek(0)
        deadline = time.monotonic() + 10
        while True:
            try:
                if platform.system() == "Windows":
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for the local bridge lock") from None
                time.sleep(0.1)
        try:
            yield directory
        finally:
            if platform.system() == "Windows":
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)


def state_paths(runtime: Path, state: dict) -> tuple[Path, Path, Path, Path]:
    session_name = state.get("session")
    if not isinstance(session_name, str) or not re.fullmatch(r"session-[A-Za-z0-9_-]+", session_name):
        raise RuntimeError("Invalid local bridge state")
    directory = runtime / session_name
    return directory, directory / "config.yaml", directory / "key", directory / "proxy.log"


def read_state(runtime: Path) -> dict | None:
    path = runtime / "service.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or (
        platform.system() != "Windows" and path.stat().st_mode & 0o777 != 0o600
    ):
        raise RuntimeError("Local bridge state has unsafe permissions")
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.verify([path], protected=True)
    state = json.loads(path.read_text())
    if (not isinstance(state, dict) or type(state.get("pid")) is not int or state["pid"] < 1 or
        type(state.get("port")) is not int or not 1 <= state["port"] <= 65535):
        raise RuntimeError("Invalid local bridge state")
    state_paths(runtime, state)
    return state


def state_is_live(runtime: Path, state: dict) -> bool:
    directory, config, key, log = state_paths(runtime, state)
    if any(path.is_symlink() or not path.exists() for path in (directory, config, key, log)):
        return False
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.verify([directory, config, key, log], protected=True)
    elif (directory.stat().st_mode & 0o777 != 0o700 or
          any(path.stat().st_mode & 0o777 != 0o600 for path in (config, key, log))):
        raise RuntimeError("Local bridge service files have unsafe permissions")
    if listener_pids(state["port"]) != {state["pid"]}:
        return False
    try:
        with socket.create_connection(("127.0.0.1", state["port"]), timeout=0.5):
            return True
    except OSError:
        return False


def remove_state_files(runtime: Path, state: dict) -> None:
    directory, config, key, log = state_paths(runtime, state)
    (runtime / "service.json").unlink(missing_ok=True)
    for path in (config, key, log):
        path.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        pass


def service_environment(state: dict, runtime: Path) -> dict[str, str]:
    _, _, key, _ = state_paths(runtime, state)
    environment = os.environ.copy()
    environment["CODEX_BRIDGE_PORT"] = str(state["port"])
    environment["CODEX_BRIDGE_KEY_FILE"] = str(key)
    return environment


def ensure_service(require_catalog: bool = True) -> tuple[Path, dict, bool]:
    with service_lock() as runtime:
        state = read_state(runtime)
        if state is not None:
            if state_is_live(runtime, state):
                return runtime, state, False
            owners = listener_pids(state["port"])
            if owners:
                raise RuntimeError("Stale bridge state points at an occupied port; no key was sent")
            remove_state_files(runtime, state)
        session = helper_output("session").split("\t")
        if len(session) != 5:
            raise RuntimeError("Session helper returned an invalid result")
        port_text, directory_text, config_text, key_text, log_text = session
        port = int(port_text)
        directory, config, key, log = map(Path, (directory_text, config_text, key_text, log_text))
        if directory.parent.resolve() != runtime.resolve():
            raise RuntimeError("Session helper used an unexpected runtime directory")
        proxy = None
        try:
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as log_handle:
                if platform.system() == "Windows":
                    import windows_acl
                    windows_acl.lock([log])
                    detached = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                else:
                    detached = {"start_new_session": True}
                proxy = subprocess.Popen([str(PROXY), "-config", str(config)], stdin=subprocess.DEVNULL,
                                         stdout=log_handle, stderr=subprocess.STDOUT, **detached)
            wait_for_private_listener(proxy, port)
            state = {"pid": proxy.pid, "port": port, "session": directory.name}
            if require_catalog:
                wait_for_catalog(proxy, service_environment(state, runtime), True)
            private_json(runtime / "service.json", state)
            return runtime, state, True
        except BaseException:
            stop_process(proxy)
            for path in (config, key, log):
                path.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass
            raise


def bridge_status() -> int:
    with service_lock() as runtime:
        state = read_state(runtime)
        if state is None or not state_is_live(runtime, state):
            print("Codex bridge is stopped")
            return 1
        print(f"Codex bridge is running on 127.0.0.1:{state['port']} (PID {state['pid']})")
        return 0


def bridge_stop() -> int:
    with service_lock() as runtime:
        state = read_state(runtime)
        if state is None:
            print("Codex bridge is already stopped")
            return 0
        if state_is_live(runtime, state):
            os.kill(state["pid"], signal.SIGTERM)
            for _ in range(50):
                if listener_pids(state["port"]) != {state["pid"]}:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Could not stop the local bridge process")
        elif listener_pids(state["port"]):
            raise RuntimeError("Stale bridge state points at an occupied port; no process was stopped")
        remove_state_files(runtime, state)
        for directory in runtime.glob("ui-*"):
            if directory.is_dir() and not directory.is_symlink():
                for name in ("statusline-profile.json", "settings.json"):
                    (directory / name).unlink(missing_ok=True)
                try:
                    directory.rmdir()
                except OSError:
                    pass
        print("Codex bridge stopped")
        return 0


def run_session(mode: str, selected: str, claude_args: list[str], claude: str) -> int:
    runtime, state, created = ensure_service()
    environment = service_environment(state, runtime)
    if mode == "models":
        return subprocess.call([sys.executable, str(HELPER), "list"], env=environment)
    model_info = helper_output("prepare", selected, environment=environment).split("\t")
    if len(model_info) != 5:
        raise RuntimeError("Model helper returned an invalid result")
    model, context, compact, settings, catalog_json = model_info
    directory = Path(tempfile.mkdtemp(prefix="ui-", dir=runtime))
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([directory])
    settings_path, _ = session_settings(Path(settings), json.loads(catalog_json), directory)
    _, _, key_path, _ = state_paths(runtime, state)
    claude_environment_values = claude_environment(state["port"], key_path.read_text().strip(), model)
    print(
        f"Codex bridge active on 127.0.0.1:{state['port']}: {model}. "
        f"Route context {context} tokens; auto-compaction {compact} tokens for this model.",
        file=sys.stderr,
    )
    if created:
        print("The localhost bridge stays available for /background and --bg; stop it with claude-codex bridge stop.", file=sys.stderr)
    print("Use /model to switch among live Codex models; press s to keep the choice in this session.", file=sys.stderr)
    print("Claude Code may show Claude alias context or list cost; those UI values are not Codex limits or billing.", file=sys.stderr)
    claude_process = None
    try:
        claude_process = subprocess.Popen(
            [claude, "--settings", str(settings_path), "--model", model, *claude_args],
            env=claude_environment_values,
        )
        return claude_process.wait()
    except KeyboardInterrupt:
        stop_process(claude_process)
        return 130
    finally:
        stop_process(claude_process)


def main() -> int:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required")
    mode, selected, claude_args = parse_args(sys.argv[1:])
    if mode == "help":
        print("Usage: claude-codex [--model MODEL_ID|sol|astra|terra|luna] [Claude Code options]")
        print("Run claude-codex codex-login once. Run claude-codex models for live model IDs.")
        print("Run claude-codex bridge start | status | stop to manage the private localhost service.")
        print("Inside Claude Code, use /model, /effort, and /autocompact normally.")
        return 0
    helper_output("verify")
    if mode == "bridge-status":
        return bridge_status()
    if mode == "bridge-stop":
        return bridge_stop()
    if mode == "bridge-start":
        _, state, _ = ensure_service()
        print(f"Codex bridge is running on 127.0.0.1:{state['port']} (PID {state['pid']})")
        return 0
    if mode == "login":
        return subprocess.call([str(PROXY), "-config", str(ROOT / "config.yaml"), "-codex-login"])
    claude = os.environ.get("CLAUDE_CODEX_CLAUDE_BIN") or shutil.which("claude")
    if mode != "models" and (not claude or not shutil.which(claude)):
        raise RuntimeError("Claude Code executable not found; set CLAUDE_CODEX_CLAUDE_BIN to its path")
    return run_session(mode, selected, claude_args, claude)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Bridge failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
