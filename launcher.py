#!/usr/bin/env python3
"""Run Claude Code against one private, session-owned local model endpoint."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import time


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
    if any(argument in ("--bg", "--background") for argument in claude_args):
        raise ValueError("Background sessions need a persistent model endpoint; this session-owned proxy exits with Claude Code")
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
    private_json(profile_path, {"command": status_line["command"], "context_windows": windows})
    command_parts = [sys.executable, str(ROOT / "statusline_adapter.py"), str(profile_path)]
    command = subprocess.list2cmdline(command_parts) if platform.system() == "Windows" else shlex.join(command_parts)
    settings = json.loads(base.read_text())
    settings["statusLine"] = {**status_line, "command": command}
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


def run_session(mode: str, selected: str, claude_args: list[str], claude: str) -> int:
    session = helper_output("session").split("\t")
    if len(session) != 5:
        raise RuntimeError("Session helper returned an invalid result")
    port_text, directory_text, config_text, key_text, log_text = session
    port = int(port_text)
    directory, config, key_path, log = map(Path, (directory_text, config_text, key_text, log_text))
    proxy_process = None
    claude_process = None
    extra_files: list[Path] = []
    try:
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as log_handle:
            proxy_process = subprocess.Popen([str(PROXY), "-config", str(config)], stdout=log_handle, stderr=subprocess.STDOUT)
            wait_for_private_listener(proxy_process, port)
            environment = os.environ.copy()
            environment["CODEX_BRIDGE_PORT"] = str(port)
            environment["CODEX_BRIDGE_KEY_FILE"] = str(key_path)
            if mode == "models":
                return subprocess.call([sys.executable, str(HELPER), "list"], env=environment)
            model_info = helper_output("prepare", selected, environment=environment).split("\t")
            if len(model_info) != 5:
                raise RuntimeError("Model helper returned an invalid result")
            model, context, compact, settings, catalog_json = model_info
            settings_path, extra_files = session_settings(Path(settings), json.loads(catalog_json), directory)
            key = key_path.read_text().strip()
            claude_environment_values = claude_environment(port, key, model)
            print(
                f"Codex bridge active on 127.0.0.1:{port}: {model}. "
                f"Route context {context} tokens; auto-compaction {compact} tokens for this model.",
                file=sys.stderr,
            )
            print("Use /model to switch among live Codex models; press s to keep the choice in this session.", file=sys.stderr)
            print("Claude Code may show Claude alias context or list cost; those UI values are not Codex limits or billing.", file=sys.stderr)
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
        stop_process(proxy_process)
        for path in (config, key_path, log, *extra_files):
            path.unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass


def main() -> int:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required")
    mode, selected, claude_args = parse_args(sys.argv[1:])
    if mode == "help":
        print("Usage: claude-codex [--model MODEL_ID|sol|astra|terra|luna] [Claude Code options]")
        print("Run claude-codex codex-login once. Run claude-codex models for live model IDs.")
        print("Inside Claude Code, use /model, /effort, and /autocompact normally.")
        return 0
    claude = os.environ.get("CLAUDE_CODEX_CLAUDE_BIN") or shutil.which("claude")
    if not claude or not shutil.which(claude):
        raise RuntimeError("Claude Code executable not found; set CLAUDE_CODEX_CLAUDE_BIN to its path")
    helper_output("verify")
    if mode == "login":
        return subprocess.call([str(PROXY), "-config", str(ROOT / "config.yaml"), "-codex-login"])
    return run_session(mode, selected, claude_args, claude)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Bridge failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
