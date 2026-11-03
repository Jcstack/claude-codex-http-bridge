#!/usr/bin/env python3
"""Run Claude Code against a private localhost model service."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, nullcontext

from model_helper import model_helper_command, model_helper_environment


ROOT = Path(os.environ.get("CLAUDE_CODEX_BRIDGE_HOME", Path(__file__).resolve().parent)).expanduser().absolute()
PROXY = ROOT / "bin" / ("cli-proxy-api.exe" if platform.system() == "Windows" else "cli-proxy-api")
HELPER = ROOT / "bridge_models.py"


def parse_args(arguments: list[str]) -> tuple[str, str, list[str]]:
    if arguments and arguments[0] == "cache":
        return "cache", "", arguments[1:]
    if arguments and arguments[0] == "account":
        return "account", "", arguments[1:]
    if arguments and arguments[0] == "quota":
        return "quota", "", arguments[1:]
    if arguments and arguments[0] == "tools":
        return "tools", "", arguments[1:]
    if arguments and arguments[0] == "desktop":
        return "desktop", "", arguments[1:]
    if arguments and arguments[0] == "diagnostics":
        return "diagnostics", "", arguments[1:]
    if arguments and arguments[0] == "mcp-audit":
        return "mcp-audit", "", arguments[1:]
    if arguments and arguments[0] == "mcp-definitions":
        return "mcp-definitions", "", arguments[1:]
    if arguments and arguments[0] == "autostart":
        return "autostart", "", arguments[1:]
    if arguments and arguments[0] == "--help":
        return "help", "", []
    if arguments and arguments[0] in ("codex-login", "models", "--list-models", "usage"):
        if len(arguments) != 1:
            raise ValueError(f"{arguments[0]} does not take arguments")
        return ("login" if arguments[0] == "codex-login" else
                "usage" if arguments[0] == "usage" else "models"), "", []
    if arguments and arguments[0] == "bridge":
        if len(arguments) == 3 and arguments[1] == "upgrade":
            return "bridge-upgrade", arguments[2], []
        if len(arguments) != 2 or arguments[1] not in ("start", "status", "stop", "reload", "rollback"):
            raise ValueError("Usage: claude-codex bridge start | status | stop | reload | rollback | upgrade BUNDLE")
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
        elif argument == "--settings" and index + 1 < len(arguments):
            # Its value belongs to Claude, even if it looks like a model flag.
            claude_args.extend(arguments[index:index + 2])
            index += 2
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
        model_helper_command(HELPER, *arguments),
        env=model_helper_environment(environment),
        text=True, encoding="utf-8",
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
        import windows_acl
        output = subprocess.check_output([windows_acl.system_tool("netstat"), "-ano", "-p", "tcp"],
                                         text=True, errors="replace")
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


def wait_for_catalog(proxy: subprocess.Popen, environment: dict[str, str], require_eligible: bool,
                     route: str = "subscription") -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proxy.poll() is not None:
            raise RuntimeError("Local model proxy exited before its model catalog was ready")
        result = subprocess.run(
            model_helper_command(HELPER, "catalog"), env=model_helper_environment(environment),
            capture_output=True, text=True, encoding="utf-8", timeout=8,
        )
        if result.returncode == 0:
            counts = json.loads(result.stdout)
            if counts.get("eligible" if require_eligible else "chat", 0) > 0:
                return
        time.sleep(0.3)
    remedy = "claude-codex api key status" if route == "api" else "claude-codex codex-login"
    raise RuntimeError(f"No {route} models appeared in the local catalog; run {remedy}")


def claude_environment(port: int, key: str, model: str,
                       route: str = "subscription", *,
                       available_models: set[str] | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    allowed = {model, *(available_models or ())}
    alias_names = ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                   "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL")
    alias_models = {}
    for name in alias_names:
        requested = environment.get(name, "").strip()
        if requested and requested not in allowed:
            raise ValueError(f"{name} is not in the eligible local model catalog; run claude-codex models")
        alias_models[name] = requested or model
    subagent = environment.get("CLAUDE_CODE_SUBAGENT_MODEL", "").strip()
    if subagent and subagent not in allowed | {"inherit", "opus", "sonnet", "haiku", "fable"}:
        raise ValueError("CLAUDE_CODE_SUBAGENT_MODEL is not an eligible model ID or Claude model alias")
    for name in (
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "DISABLE_COMPACT", "CLAUDE_CODE_EXTRA_BODY",
    ):
        environment.pop(name, None)
    environment.update({
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": key,
        "ENABLE_TOOL_SEARCH": "true",
    })
    environment.update(alias_models)
    if subagent:
        environment["CLAUDE_CODE_SUBAGENT_MODEL"] = subagent
    # Documented gateway controls. OpenAI does not strip Anthropic's attribution
    # block; omit it at the client to keep the reusable prefix stable. Fixed-
    # vocabulary hint headers distinguish compaction from ordinary turns without
    # guessing from prompt text. Keep explicit operator overrides.
    environment.setdefault("CLAUDE_CODE_ATTRIBUTION_HEADER", "0")
    environment.setdefault("CLAUDE_CODE_GATEWAY_HINT_HEADERS", "1")
    # The user chose a private local gateway. Disable optional Anthropic
    # telemetry/survey traffic by default using the documented host control;
    # ordinary tools, plugins and the WebFetch safety check remain available.
    environment.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    # The subscription endpoint accepted the tool flag in a live call. On the
    # direct API route, this proxy forwards eager_input_streaming as an extra
    # Responses function-tool field, so do not enable it without an upstream
    # translation fix and live API verification.
    streaming_flag = "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING"
    if route == "subscription":
        environment.setdefault(streaming_flag, "1")
    elif route == "api":
        if environment.get(streaming_flag) == "1":
            raise RuntimeError("Fine-grained tool streaming is not verified on the API route; "
                               "unset CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING")
    else:
        raise RuntimeError("Unknown bridge route")
    return environment


def native_compaction_options(environment: dict[str, str], arguments: list[str],
                              route: str, manifest: dict | None) -> list[str]:
    """Opt in only with the reviewed implementation, preserving unrelated headers."""
    setting = os.environ.get("CLAUDE_CODEX_NATIVE_COMPACTION", "0")
    if setting not in ("0", "1"):
        raise ValueError("CLAUDE_CODEX_NATIVE_COMPACTION must be 0 or 1")
    enabled = setting == "1"
    forwarded = []
    for index, argument in enumerate(arguments):
        if argument == "--":
            forwarded.extend(arguments[index:])
            break
        if argument in ("--native-compaction", "--no-native-compaction"):
            enabled = argument == "--native-compaction"
        else:
            forwarded.append(argument)
    if enabled:
        files = manifest.get("source", {}).get("files", {}) if manifest else {}
        if route != "subscription" or "upstream/patches/native-compaction-v2.patch" not in files:
            raise RuntimeError("Native compaction requires the reviewed subscription runtime with its V2 patch; upgrade the runtime first")
        if environment.get("CLAUDE_CODE_GATEWAY_HINT_HEADERS") != "1":
            raise RuntimeError("Native compaction requires CLAUDE_CODE_GATEWAY_HINT_HEADERS=1")
    name = "X-Codex-Bridge-Native-Compaction"
    headers = [line for line in environment.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
               if line.partition(":")[0].strip().lower() != name.lower()]
    headers.append(name + (": 1" if enabled else ": 0"))
    environment["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(headers)
    return forwarded


def configured_status_line() -> dict | None:
    if os.environ.get("CLAUDE_CODEX_STATUSLINE_ADAPTER") == "0":
        return None
    locations = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        roots = [Path(part) for part in locations.split(os.pathsep) if part] if locations else [Path.home() / ".claude"]
    except RuntimeError:
        return None
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


def status_line_setting_sources(arguments: list[str]) -> set[str]:
    """Determine the explicitly requested local setting scopes."""
    if any(os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on") for name in
           ("CLAUDE_CODE_RESTRICTED", "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE")):
        return set()
    selected_sources = None
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            break
        if value in ("--restricted", "--safe-mode", "--bare"):
            return set()
        if value == "--setting-sources":
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                return set()
            selected_sources = arguments[index + 1]
            index += 2
            continue
        if value.startswith("--setting-sources="):
            selected_sources = value.partition("=")[2]
        index += 1
    if selected_sources is None:
        return {"user", "project", "local"}
    return {part.strip() for part in selected_sources.split(",") if part.strip()}


def user_status_line_allowed(arguments: list[str]) -> bool:
    """Do not promote a disabled user setting into command-line scope."""
    return "user" in status_line_setting_sources(arguments)


def bounded_regular_bytes(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("File exceeds limit")
    return data


def settings_may_define_status_line(path: Path) -> bool:
    """Read only bounded JSON; uncertainty leaves precedence to Claude."""
    try:
        data = bounded_regular_bytes(path, SETTINGS_LIMIT)
        value = json.loads(data.decode("utf-8-sig"))
        return not isinstance(value, dict) or "statusLine" in value
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError, ValueError, RecursionError):
        return True


def local_settings_roots(workspace: Path) -> tuple[set[Path], bool]:
    """Find ordinary repository and linked-worktree local settings roots."""
    roots = {workspace}
    # Claude keeps project-local settings at the starting directory on Windows.
    if os.name == "nt":
        return roots, False
    for directory in (workspace, *workspace.parents):
        marker = directory / ".git"
        try:
            if marker.is_dir():
                roots.add(directory)
                break
            if not marker.is_file():
                continue
            roots.add(directory)
            content = bounded_regular_bytes(marker, 4096).decode("utf-8").strip()
            if not content.startswith("gitdir: "):
                return roots, True
            location = Path(content.removeprefix("gitdir: "))
            gitdir = (location if location.is_absolute() else directory / location).resolve()
            common_file = gitdir / "commondir"
            if common_file.exists():
                common_text = bounded_regular_bytes(common_file, 4096).decode("utf-8").strip()
                common = Path(common_text)
                common = (common if common.is_absolute() else gitdir / common).resolve()
                if not common_text or common.name != ".git":
                    return roots, True
                roots.add(common.parent)
            break
        except (OSError, UnicodeError, RuntimeError, ValueError):
            return roots, True
    return roots, False


def project_status_line_may_override(arguments: list[str], workspace: Path) -> bool:
    sources = status_line_setting_sources(arguments)
    if "project" in sources:
        shared = workspace / ".claude" / "settings.json"
        locations = os.environ.get("CLAUDE_CONFIG_DIR")
        try:
            user_roots = ([Path(part) for part in locations.split(os.pathsep) if part] if locations
                          else [Path.home() / ".claude"])
        except RuntimeError:
            return True
        same_as_user = False
        for root in user_roots:
            try:
                if os.path.samefile(shared, root / "settings.json"):
                    same_as_user = True
                    break
            except OSError:
                pass
        if not same_as_user and settings_may_define_status_line(shared):
            return True
    if "local" in sources:
        roots, uncertain = local_settings_roots(workspace)
        if uncertain:
            return True
        return any(settings_may_define_status_line(root / ".claude" / "settings.local.json") for root in roots)
    return False


def private_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.write("\n")
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([path])


def private_settings_json(path: Path, value: dict) -> None:
    if len((json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("Combined Claude settings exceed the client's 2 MiB limit")
    private_json(path, value)


def session_settings(base: Path, windows: dict[str, int], directory: Path,
                     route: str = "subscription", *,
                     include_user_status_line: bool = True) -> tuple[Path, list[Path]]:
    settings = json.loads(base.read_text())
    # An explicit launch setting has precedence over the user-level status line.
    if "statusLine" in settings:
        status_line = settings["statusLine"]
        if (os.environ.get("CLAUDE_CODEX_STATUSLINE_ADAPTER") == "0" or
            not isinstance(status_line, dict) or status_line.get("type") != "command" or
            not isinstance(status_line.get("command"), str) or not status_line["command"].strip() or
            "statusline_adapter.py" in status_line["command"]):
            status_line = None
    else:
        status_line = configured_status_line() if include_user_status_line else None
    if status_line is None:
        return base, []
    profile_path = directory / "statusline-profile.json"
    settings_path = directory / "settings.json"
    profile = {"context_windows": windows}
    profile.update({"remember_model": True, "route": route})
    if os.environ.get("CLAUDE_CODEX_STATUSLINE_CACHE_METRICS") == "1":
        profile["expose_cache_metrics"] = True
    elif os.environ.get("CLAUDE_CODEX_STATUSLINE_CACHE_METRICS") == "0":
        profile["expose_cache_metrics"] = False
    if route == "subscription":
        profile["usage_cache"] = str(directory.parent / "codex-usage.json")
    private_json(profile_path, profile)
    if platform.system() == "Windows":
        python_command = "py -3" if shutil.which("py") else "python"
        command = f'{python_command} "{(ROOT / "statusline_adapter.py").as_posix()}" "{profile_path.as_posix()}"'
    else:
        command = shlex.join([sys.executable, str(ROOT / "statusline_adapter.py"), str(profile_path)])
    settings["statusLine"] = {**status_line, "command": command + " | " + status_line["command"]}
    private_settings_json(settings_path, settings)
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


def runtime_directory(route: str = "subscription") -> Path:
    if route not in ("subscription", "api"):
        raise RuntimeError("Unknown bridge route")
    directory = ROOT / ("runtime-api" if route == "api" else "runtime")
    if directory.is_symlink():
        raise RuntimeError("Runtime directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([directory])
    return directory


@contextmanager
def service_lock(route: str = "subscription"):
    directory = runtime_directory(route)
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
    if "process_identity" in state and (not isinstance(state["process_identity"], str) or
            not re.fullmatch(r"[a-f0-9]{64}", state["process_identity"])):
        raise RuntimeError("Invalid local bridge process identity")
    state_paths(runtime, state)
    return state


def service_proxy(runtime: Path) -> Path:
    """Resolve the owner binary from the state root, even in offline tools."""
    return runtime.parent / "bin" / ("cli-proxy-api.exe" if platform.system() == "Windows" else "cli-proxy-api")


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
    import process_identity
    try:
        identity = process_identity.capture(state["pid"])
        current = process_identity.stamp(state["pid"], identity)
        expected = state.get("process_identity")
        if expected is not None:
            if current != expected:
                return False
        elif not process_identity.executable_matches(identity, service_proxy(runtime)):
            # Legacy state has no birth marker. Check the actual executable
            # before ever sending its localhost key or adopting the process.
            return False
    except RuntimeError:
        return False
    try:
        with socket.create_connection(("127.0.0.1", state["port"]), timeout=0.5):
            return (listener_pids(state["port"]) == {state["pid"]} and
                    process_identity.matches(state["pid"], current))
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


def service_environment(state: dict, runtime: Path, route: str = "subscription") -> dict[str, str]:
    _, _, key, _ = state_paths(runtime, state)
    environment = os.environ.copy()
    environment["CODEX_BRIDGE_PORT"] = str(state["port"])
    environment["CODEX_BRIDGE_KEY_FILE"] = str(key)
    if route == "api":
        environment["CODEX_BRIDGE_ROUTE"] = "api"
        environment["CODEX_BRIDGE_API_METADATA_FILE"] = str(runtime / "catalog.json")
    else:
        environment.pop("CODEX_BRIDGE_ROUTE", None)
        environment.pop("CODEX_BRIDGE_API_METADATA_FILE", None)
    return environment


def origin_process_environment() -> dict[str, str]:
    """Keep ambient credentials and unrelated proxy storage controls out."""
    import rolling_runtime
    environment = rolling_runtime.process_environment()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        if name in os.environ:
            environment[name] = os.environ[name]
    for name in ("NO_PROXY", "no_proxy"):
        configured = os.environ.get(name, "").strip(", ")
        environment[name] = ",".join(value for value in (configured, "127.0.0.1", "localhost") if value)
    return environment


def ensure_service(require_catalog: bool = True, route: str = "subscription") -> tuple[Path, dict, bool]:
    with service_lock(route) as runtime:
        state = read_state(runtime)
        if state is not None:
            if state_is_live(runtime, state):
                if "process_identity" not in state:
                    import api_profile
                    import process_identity
                    identity = process_identity.capture(state["pid"])
                    if (not process_identity.executable_matches(identity, service_proxy(runtime)) or
                            listener_pids(state["port"]) != {state["pid"]}):
                        raise RuntimeError("Legacy bridge owner changed; no key was sent")
                    state = {**state, "process_identity": process_identity.stamp(state["pid"], identity)}
                    api_profile.write_private(runtime / "service.json", json.dumps(state) + "\n")
                return runtime, state, False
            owners = listener_pids(state["port"])
            if owners:
                raise RuntimeError("Stale bridge state points at an occupied port; no key was sent")
            remove_state_files(runtime, state)
        if route == "api":
            import api_profile
            session = list(api_profile.make_session(ROOT, runtime))
        else:
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
                                         stdout=log_handle, stderr=subprocess.STDOUT, cwd=directory,
                                         env=origin_process_environment(), **detached)
            wait_for_private_listener(proxy, port)
            import process_identity
            identity = process_identity.capture(proxy.pid)
            if not process_identity.executable_matches(identity, PROXY) or proxy.poll() is not None:
                raise RuntimeError("New bridge process identity changed; no key was sent")
            state = {"pid": proxy.pid, "port": port, "session": directory.name,
                     "process_identity": process_identity.stamp(proxy.pid, identity)}
            if require_catalog:
                wait_for_catalog(proxy, service_environment(state, runtime, route), True, route)
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


def bridge_status(route: str = "subscription") -> int:
    label = "OpenAI API" if route == "api" else "Codex"
    with service_lock(route) as runtime:
        state = read_state(runtime)
        if state is None or not state_is_live(runtime, state):
            print(f"{label} bridge is stopped")
            return 1
        print(f"{label} bridge is running on 127.0.0.1:{state['port']} (PID {state['pid']})")
        if route == "subscription":
            import rolling_runtime
            managed = rolling_runtime.state(ROOT)
            if managed:
                snapshot = rolling_runtime.endpoint_status(ROOT, managed)
                print(f"Managed TLS endpoint: https://127.0.0.1:{managed['port']}; active requests {sum(snapshot.get('active', {}).values())}")
        return 0


def bridge_stop(route: str = "subscription") -> int:
    label = "OpenAI API" if route == "api" else "Codex"
    if route == "subscription":
        import rolling_runtime
        rolling_runtime.stop(ROOT)
    with service_lock(route) as runtime:
        state = read_state(runtime)
        if state is None:
            print(f"{label} bridge is already stopped")
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
                for name in ("statusline-profile.json", "settings.json", "managed-settings.json",
                             "composed-settings.json", "trusted-ca.pem"):
                    (directory / name).unlink(missing_ok=True)
                try:
                    directory.rmdir()
                except OSError:
                    pass
        print(f"{label} bridge stopped")
        return 0


def bridge_start(route: str = "subscription") -> int:
    runtime, state, _ = ensure_service(route=route)
    if route == "subscription":
        import quota_recovery
        quota_recovery.ensure_watch(ROOT)
    if route == "subscription" and (ROOT / "runtime-selected.json").exists():
        import accounts
        import rolling_runtime
        with accounts.switch_lock(ROOT, wait_seconds=30):
            models, verify = managed_catalog(state)
            managed = rolling_runtime.ensure(ROOT, runtime, state, models, before_publish=verify)
        print(f"Managed TLS endpoint is running on https://127.0.0.1:{managed['port']}")
        return 0
    label = "OpenAI API" if route == "api" else "Codex"
    print(f"{label} bridge is running on 127.0.0.1:{state['port']} (PID {state['pid']})")
    return 0


def managed_catalog(state: dict):
    """Call while holding the account lock; verify again before publication."""
    import accounts
    management = accounts.Management(ROOT)
    if management.state != state:
        raise RuntimeError("Account model owner changed; no catalog was published")
    models = accounts.selected_catalog(ROOT, management)

    def verify_before_publish():
        current = accounts.Management(ROOT)
        if current.state != state or accounts.selected_catalog(ROOT, current) != models:
            raise RuntimeError("Account model catalog changed during replacement")

    return models, verify_before_publish


def verify_prepared_model_settings(catalog_json: str, settings: Path, models: dict) -> None:
    """Reject a model or compact-window change during session preparation."""
    import runtime_bundle
    try:
        windows = json.loads(catalog_json)
        runtime_bundle.safe_file(settings, limit=2 * 1024 * 1024)
        prepared = json.loads(settings.read_text())
        configured = prepared.get("modelSettings") if isinstance(prepared, dict) else None
        if not isinstance(configured, dict): raise ValueError("Missing model settings")
        compact = {name: row.get("autoCompactWindow") if isinstance(row, dict) else None
                   for name, row in configured.items()}
        expected_windows = {name: spec["context_window"] for name, spec in models.items()}
        expected_compact = {name: spec["auto_compact_window"] for name, spec in models.items()}
        if windows != expected_windows or compact != expected_compact:
            raise ValueError("Changed model settings")
    except (KeyError, OSError, ValueError, TypeError):
        raise RuntimeError("Model catalog or compact window changed during session preparation; retry the launch") from None


SETTINGS_LIMIT = 2 * 1024 * 1024
SETTINGS_NO_VALUE_OPTIONS = frozenset({
    "--allow-dangerously-skip-permissions", "--dangerously-skip-permissions", "--restricted",
    "--safe-mode", "--bare", "--strict-mcp-config", "--verbose", "--debug-to-stderr",
    "--continue", "-c", "--print", "-p", "--bg", "--background", "--no-session-persistence",
    "--fork-session", "--chrome", "--no-chrome", "--ide",
    "--disable-slash-commands", "--include-partial-messages", "--replay-user-messages",
    "--native-tools", "--no-native-tools", "--native-compaction", "--no-native-compaction",
})
SETTINGS_PROTECTED_ENV = frozenset({
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "DISABLE_COMPACT", "CLAUDE_CODE_EXTRA_BODY",
    "CLAUDE_CODE_GATEWAY_HINT_HEADERS", "CLAUDE_CODE_ATTRIBUTION_HEADER",
    "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING", "ENABLE_TOOL_SEARCH",
})
SETTINGS_PROTECTED_KEYS = frozenset({
    "apiKeyHelper", "proxyAuthHelper", "forceLoginMethod", "forceLoginGatewayUrl",
    "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh", "policyHelper",
})


def load_launch_settings(value: str) -> dict:
    """Read bounded JSON once; never include user values or paths in errors."""
    try:
        if value.lstrip().startswith("{"):
            data = value.encode("utf-8")
        else:
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(value, flags)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > SETTINGS_LIMIT:
                    raise ValueError("Invalid settings file")
                data = handle.read(SETTINGS_LIMIT + 1)
        if len(data) > SETTINGS_LIMIT:
            raise ValueError("Settings are too large")

        def unique_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("Duplicate setting")
                result[key] = item
            return result

        def no_constant(_value):
            raise ValueError("Invalid JSON constant")

        def finite_float(raw):
            result = float(raw)
            if not math.isfinite(result):
                raise ValueError("Non-finite JSON number")
            return result

        settings = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_object,
                              parse_constant=no_constant, parse_float=finite_float)
        if not isinstance(settings, dict):
            raise ValueError("Settings must be an object")
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ValueError("Custom --settings must be a readable regular JSON object no larger than 2 MiB, without duplicate keys") from None
    if any(name in settings for name in SETTINGS_PROTECTED_KEYS):
        raise ValueError("Custom --settings cannot replace bridge authentication or provider configuration")
    environment = settings.get("env", {})
    if not isinstance(environment, dict):
        raise ValueError("Custom --settings env must be an object")
    for name in environment:
        upper = name.upper()
        if upper in SETTINGS_PROTECTED_ENV or upper.startswith("ANTHROPIC_DEFAULT_"):
            raise ValueError("Custom --settings cannot replace bridge routing, authentication, TLS, proxy, or protected model controls")
    if "model" in settings and (not isinstance(settings["model"], str) or not settings["model"].strip()):
        raise ValueError("Custom --settings model must be a nonempty model ID")
    return settings


def extract_launch_settings(arguments: list[str]) -> tuple[dict | None, list[str]]:
    """Keep native last-occurrence behavior, but forward exactly one composed file."""
    remaining = []
    selected = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            remaining.extend(arguments[index:])
            break
        if argument == "--settings" or argument.startswith("--settings="):
            # Avoid turning another option's literal value into a settings flag.
            previous = arguments[index - 1] if index else ""
            if previous.startswith("-") and "=" not in previous and previous not in SETTINGS_NO_VALUE_OPTIONS:
                raise ValueError("Ambiguous --settings position; place it before options that take a value")
            if argument == "--settings":
                if index + 1 >= len(arguments) or arguments[index + 1] == "--":
                    raise ValueError("Missing value after --settings")
                selected = arguments[index + 1]
                index += 2
            else:
                selected = argument.partition("=")[2]
                index += 1
            continue
        remaining.append(argument)
        index += 1
    return (load_launch_settings(selected) if selected is not None else None), remaining


def compose_launch_settings(base: dict, custom: dict) -> dict:
    """Preserve hooks and per-model fields without weakening discovered windows."""
    result = {**base, **custom}
    for key in ("env", "hooks", "modelSettings"):
        if key in custom and not isinstance(custom[key], dict):
            raise ValueError(f"Custom --settings {key} must be an object")
        if key in base and not isinstance(base[key], dict):
            raise ValueError("Generated bridge settings have an invalid structure")
    result["env"] = {**base.get("env", {}), **custom.get("env", {})}
    hooks = dict(base.get("hooks", {}))
    for event, entries in custom.get("hooks", {}).items():
        previous = hooks.get(event, [])
        if not isinstance(entries, list) or not isinstance(previous, list):
            raise ValueError("Custom and bridge hook events must be lists")
        hooks[event] = [*previous, *entries]
    if hooks or "hooks" in base or "hooks" in custom:
        result["hooks"] = hooks
    model_settings = {name: dict(row) for name, row in base.get("modelSettings", {}).items()
                      if isinstance(row, dict)}
    if len(model_settings) != len(base.get("modelSettings", {})):
        raise ValueError("Generated bridge model settings have an invalid structure")
    custom_models = custom.get("modelSettings", {})
    if any(not isinstance(row, dict) for row in custom_models.values()):
        raise ValueError("Each custom modelSettings entry must be an object")

    def checked_window(name, value):
        ceiling = base.get("modelSettings", {}).get(name, {}).get("autoCompactWindow")
        if value == "auto" and type(ceiling) is int and ceiling >= 100_000:
            # The Claude alias's own default can be larger than this Codex
            # model. Resolve auto from the current eligible catalog instead.
            return ceiling
        # Claude's current settings schema accepts 100k–1M. The upper bound
        # here is the dynamically prepared model-specific bridge window.
        if type(value) is not int or type(ceiling) is not int or not 100_000 <= value <= ceiling:
            raise ValueError("A custom autoCompactWindow is not within the discovered bridge limit; set a safe value for a known model")
        return value

    if "autoCompactWindow" in custom:
        if type(custom["autoCompactWindow"]) is not int:
            raise ValueError("A top-level autoCompactWindow must be numeric")
        for name, row in model_settings.items():
            row["autoCompactWindow"] = checked_window(name, custom["autoCompactWindow"])
    for name, row in custom_models.items():
        row = dict(row)
        if "autoCompactWindow" in row:
            row["autoCompactWindow"] = checked_window(name, row["autoCompactWindow"])
        model_settings[name] = {**model_settings.get(name, {}), **row}
    if model_settings or "modelSettings" in base or "modelSettings" in custom:
        result["modelSettings"] = model_settings
    return result


def run_session(mode: str, selected: str, claude_args: list[str], claude: str,
                route: str = "subscription", *, explicit_model: bool = True) -> int:
    custom_settings = None
    if mode == "claude":
        custom_settings, claude_args = extract_launch_settings(claude_args)
        if custom_settings is not None and "model" in custom_settings and not explicit_model:
            selected = custom_settings["model"]
            explicit_model = True
    include_user_status_line = user_status_line_allowed(claude_args)
    if (mode == "claude" and include_user_status_line and
        (custom_settings is None or "statusLine" not in custom_settings) and
        os.environ.get("CLAUDE_CODEX_STATUSLINE_ADAPTER") != "0" and
        project_status_line_may_override(claude_args, Path.cwd())):
        if configured_status_line() is not None:
            print("Project/local status-line precedence cannot be safely wrapped; Claude selects the "
                  "status line without bridge context, cost, quota, or cache corrections for this launch.", file=sys.stderr)
        include_user_status_line = False
    runtime, state, created = ensure_service(route=route)
    environment = service_environment(state, runtime, route)
    if mode == "models":
        return subprocess.call(model_helper_command(HELPER, "list"), env=model_helper_environment(environment))
    resume_without_model = False
    if not explicit_model:
        import session_models
        resuming, remembered = session_models.restore(ROOT, claude_args, Path.cwd(), route)
        if remembered:
            selected = remembered
        elif resuming:
            resume_without_model = True
    managed = None
    managed_enabled = route == "subscription" and (ROOT / "runtime-selected.json").exists()
    if managed_enabled:
        import accounts
        import rolling_runtime
    lock = accounts.switch_lock(ROOT, wait_seconds=30) if managed_enabled else nullcontext()
    with lock:
        model_info = helper_output("prepare", selected, environment=environment).split("\t")
        if len(model_info) != 5:
            raise RuntimeError("Model helper returned an invalid result")
        model, context, compact, settings, catalog_json = model_info
        if custom_settings is not None:
            # Reject structural or window conflicts before any managed handoff
            # or copying a key into a per-launch settings file.
            compose_launch_settings(json.loads(Path(settings).read_text()), custom_settings)
        if managed_enabled:
            models, verify = managed_catalog(state)
            verify_prepared_model_settings(catalog_json, Path(settings), models)
            managed = rolling_runtime.ensure(ROOT, runtime, state, models, before_publish=verify)
    if route == "subscription":
        import autostart
        with autostart.operation_lock(ROOT): autostart.unpause(ROOT)
        import quota_recovery
        quota_recovery.ensure_watch(ROOT)
    directory = Path(tempfile.mkdtemp(prefix="ui-", dir=runtime))
    if platform.system() == "Windows":
        import windows_acl
        windows_acl.lock([directory])
    settings_path = Path(settings)
    if custom_settings is None:
        settings_path, statusline_files = session_settings(settings_path, json.loads(catalog_json), directory, route,
                                                          include_user_status_line=include_user_status_line)
        if statusline_files and route == "subscription":
            import codex_usage
            codex_usage.start_refresh(runtime / "codex-usage.json")
    _, _, key_path, _ = state_paths(runtime, state)
    claude_environment_values = claude_environment(state["port"], key_path.read_text().strip(),
                                                  model, route, available_models=set(json.loads(catalog_json)))
    if managed:
        claude_environment_values = rolling_runtime.client_environment(ROOT, managed, claude_environment_values, directory)
    manifest = None
    if managed:
        import runtime_bundle
        manifest = runtime_bundle.selected(ROOT)[1]
    claude_args = native_compaction_options(claude_environment_values, claude_args, route, manifest)
    if managed or custom_settings is not None:
        import rolling_runtime
        settings_path = rolling_runtime.client_settings(settings_path, claude_environment_values, directory)
    if custom_settings is not None:
        combined = compose_launch_settings(json.loads(settings_path.read_text()), custom_settings)
        compact = combined.get("modelSettings", {}).get(model, {}).get("autoCompactWindow", compact)
        settings_path = directory / "composed-settings.json"
        private_settings_json(settings_path, combined)
        settings_path, statusline_files = session_settings(settings_path, json.loads(catalog_json), directory, route,
                                                          include_user_status_line=include_user_status_line)
        if statusline_files and route == "subscription":
            import codex_usage
            codex_usage.start_refresh(runtime / "codex-usage.json")
        # If Claude rejects the composed settings, an inherited token must not
        # follow a lower-priority user-configured endpoint. The authenticated
        # token is supplied only by the private settings file for this launch.
        claude_environment_values.pop("ANTHROPIC_AUTH_TOKEN", None)
    label = "OpenAI API" if route == "api" else "Codex"
    print(
        f"{label} bridge active on {claude_environment_values['ANTHROPIC_BASE_URL']}: "
        f"{'native resume selection; fallback ' if resume_without_model else ''}{model}. "
        f"Route context {context} tokens; auto-compaction {compact} tokens for this model.",
        file=sys.stderr,
    )
    if created:
        print("The localhost bridge stays available for /background and --bg; stop it with claude-codex bridge stop.", file=sys.stderr)
    print("Use /model to switch among live Codex models; press s to keep the choice in this session.", file=sys.stderr)
    print("Claude Code may show Claude alias context or list cost; those UI values are not Codex limits or billing.", file=sys.stderr)
    capability_flags = []
    if (ROOT / "capabilities.json").exists() or any(value in claude_args for value in ("--native-tools", "--no-native-tools")):
        import capabilities
        capability_flags, claude_args = capabilities.launch_flags(ROOT, directory, Path.cwd(), claude_args, route)
    model_flags = [] if resume_without_model else ["--model", model]
    command = [claude, *capability_flags, "--settings", str(settings_path), *model_flags, *claude_args]
    if platform.system() != "Windows":
        # Replace the Python launcher so tmux sees Claude Code as the foreground process.
        # The localhost service is independent and must remain alive for background agents.
        os.execvpe(claude, command, claude_environment_values)
    claude_process = None
    try:
        claude_process = subprocess.Popen(
            command,
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
    arguments = sys.argv[1:]
    route = "api" if arguments and arguments[0] == "api" else "subscription"
    if route == "api":
        arguments = arguments[1:]
    if route == "api" and arguments and arguments[0] == "key":
        if len(arguments) != 2:
            raise ValueError("Usage: claude-codex api key set | status")
        helper_output("verify")
        import api_profile
        return api_profile.key_command(ROOT, arguments[1])
    mode, selected, claude_args = parse_args(arguments)
    if mode == "help":
        print("Usage: claude-codex [api] [--model MODEL_ID|sol|astra|terra|luna] [Claude Code options]")
        print("Subscription: claude-codex codex-login; claude-codex models; claude-codex usage.")
        print("Independent accounts: claude-codex account login ALIAS; account check ALIAS; account use ALIAS; account list.")
        print("Direct OpenAI API: claude-codex api key set; claude-codex api models; claude-codex api --model sol.")
        print("Run claude-codex [api] bridge start | status | stop to manage each private localhost service.")
        print("macOS/Windows login recovery: claude-codex autostart enable | status | disable.")
        print("Reviewed runtime: claude-codex bridge upgrade BUNDLE | rollback | reload; replaced backends drain.")
        print("Inside Claude Code, use /model, /effort, and /autocompact normally.")
        print("Optional native capabilities: claude-codex tools install | status | enable | disable (Python 3.11+).")
        print("Use --no-native-tools to omit them for one launch; --native-tools explicitly enables them with strict MCP config.")
        print("Opt-in native V2 /compact: --native-compaction (reviewed subscription runtime required); --no-native-compaction disables it.")
        print("Opt-in scoped computer use: claude-codex desktop install; desktop windows; desktop select WINDOW_ID; desktop disable.")
        print("Read-only metadata: claude-codex diagnostics [--hours 2 | --lifetime] [--session UUID | --tracked] [--include-children].")
        print("Partial offline MCP name check: claude-codex mcp-audit [--config FILE] [--inventory FILE] [--show-names].")
        print("Partial offline MCP definition check: claude-codex mcp-definitions --session-file FILE --inventory FILE.")
        print("Reported cache tokens: claude-codex cache telemetry enable | disable | status.")
        print("Quota monitor: claude-codex quota status | recover.")
        return 0
    if mode == "diagnostics":
        import diagnostics
        return diagnostics.main(claude_args, root=ROOT, route=route)
    if mode == "mcp-audit":
        import mcp_name_audit
        return mcp_name_audit.main(claude_args)
    if mode == "mcp-definitions":
        import mcp_definition_audit
        return mcp_definition_audit.main(claude_args)
    if mode == "cache":
        if route != "subscription": raise ValueError("The shared cache telemetry preference uses the subscription route")
        import statusline_adapter
        return statusline_adapter.cache_metrics_command(ROOT, claude_args)
    helper_output("verify")
    if mode == "autostart":
        if route != "subscription": raise ValueError("Login recovery uses the Codex subscription profile")
        import autostart
        return autostart.main(claude_args, root=ROOT)
    if mode == "quota":
        if route != "subscription": raise ValueError("Quota recovery uses the Codex subscription profile")
        import quota_recovery
        return quota_recovery.main(claude_args, root=ROOT)
    if mode == "account":
        if route != "subscription": raise ValueError("Account selection uses the Codex subscription profile")
        import accounts
        return accounts.main(claude_args, root=ROOT)
    if mode == "tools":
        if route != "subscription": raise ValueError("Native tools use the subscription profile")
        import capabilities
        return capabilities.main(claude_args, root=ROOT)
    if mode == "desktop":
        if route != "subscription": raise ValueError("Scoped desktop currently uses the subscription capability pack")
        import desktop_adapter
        return desktop_adapter.main(claude_args, root=ROOT)
    if route == "api" and mode in ("login", "usage"):
        raise ValueError("API route uses a project key and API Platform billing; run claude-codex api key set")
    if mode == "usage":
        import codex_usage
        return codex_usage.main()
    if mode == "bridge-status":
        return bridge_status(route)
    if mode == "bridge-stop":
        if route == "api": return bridge_stop(route)
        import autostart
        with autostart.operation_lock(ROOT):
            result = bridge_stop(route)
            if result == 0: autostart.pause(ROOT)
            return result
    if mode in ("bridge-upgrade", "bridge-rollback", "bridge-reload"):
        if route != "subscription": raise ValueError("Managed runtime currently wraps the Codex subscription route")
        import rolling_runtime
        import runtime_bundle
        import accounts
        with accounts.switch_lock(ROOT, wait_seconds=30):
            previous_selection = attempted_identity = history_before = history_after = None
            if mode in ("bridge-upgrade", "bridge-rollback"):
                with service_lock():
                    previous_selection = runtime_bundle.selection_snapshot(ROOT)
                    current = runtime_bundle.selected(ROOT)
                    current_identity = current[2] if current is not None else None
                    if mode == "bridge-upgrade":
                        bundle = Path(selected).expanduser().absolute()
                        _, attempted_identity = runtime_bundle.verify(bundle)
                    else:
                        if current_identity is None: raise RuntimeError("No selected runtime is available for rollback")
                        _, _, attempted_identity = runtime_bundle.previous_installed(ROOT, current_identity)
                    history_before = runtime_bundle.history_snapshot(ROOT)
                    if attempted_identity != current_identity:
                        history_after = runtime_bundle.history_transition(attempted_identity, current_identity)
                    try:
                        if history_after is not None: runtime_bundle.write_history(ROOT, history_after)
                        if mode == "bridge-upgrade": runtime_bundle.activate(ROOT, bundle)
                        else: runtime_bundle.select_installed(ROOT, attempted_identity)
                    except BaseException:
                        runtime_bundle.rollback_selection(ROOT, previous_selection, attempted_identity)
                        if history_after is not None: runtime_bundle.rollback_history(ROOT, history_before, history_after)
                        raise
            try:
                runtime, state, _ = ensure_service(route=route)
                models, verify = managed_catalog(state)
                managed = rolling_runtime.ensure(ROOT, runtime, state, models, force=mode == "bridge-reload",
                                                 before_publish=verify)
            except BaseException:
                if mode in ("bridge-upgrade", "bridge-rollback"):
                    with service_lock():
                        runtime_bundle.rollback_selection(ROOT, previous_selection, attempted_identity)
                        if history_after is not None: runtime_bundle.rollback_history(ROOT, history_before, history_after)
                raise
        import quota_recovery
        quota_recovery.ensure_watch(ROOT)
        print(f"Managed TLS endpoint ready: https://127.0.0.1:{managed['port']}; legacy sessions retain their original service")
        return 0
    if mode == "bridge-start":
        if route == "api": return bridge_start(route)
        import autostart
        with autostart.operation_lock(ROOT):
            result = bridge_start(route)
            if result == 0: autostart.unpause(ROOT)
            return result
    if mode == "login":
        import accounts
        accounts.initial_login(ROOT)
        return 0
    claude = os.environ.get("CLAUDE_CODEX_CLAUDE_BIN") or shutil.which("claude")
    if mode != "models" and (not claude or not shutil.which(claude)):
        raise RuntimeError("Claude Code executable not found; set CLAUDE_CODEX_CLAUDE_BIN to its path")
    before_separator = arguments[:arguments.index("--")] if "--" in arguments else arguments
    explicit_model = any(value == "--model" or value.startswith("--model=") for value in before_separator)
    return run_session(mode, selected, claude_args, claude, route, explicit_model=explicit_model)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Bridge failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
