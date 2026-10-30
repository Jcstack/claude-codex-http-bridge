"""Install and select private optional MCP capabilities for Claude Code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

import runtime_bundle

SERVER_SOURCES = ("native_client.py", "native_services.py", "native_tools.py", "runtime_bundle.py",
                  "rolling_runtime.py", "launcher.py", "api_profile.py", "bridge_models.py", "windows_acl.py",
                  "desktop_adapter.py", "desktop_scope.py", "desktop_windows.py")


def environment_python(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def config(root: Path) -> dict | None:
    path = root / "capabilities.json"
    if not path.exists(): return None
    runtime_bundle.safe_file(path, limit=16384)
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("format") != 1 or type(value.get("enabled")) is not bool or
        not isinstance(value.get("lock"), str) or len(value["lock"]) != 64 or
        any(c not in "0123456789abcdef" for c in value["lock"]) or
        not isinstance(value.get("server"), str) or len(value["server"]) != 64 or
        any(c not in "0123456789abcdef" for c in value["server"])):
        raise RuntimeError("Invalid private capability configuration")
    return value


def selected_python(root: Path) -> Path:
    value = config(root)
    if value is None: raise RuntimeError("Run claude-codex tools install before using native capabilities")
    directory = root / "capabilities" / ("env-" + value["lock"])
    marker = directory / ".bridge-capability-lock"
    runtime_bundle.safe_file(marker, limit=128)
    if directory.is_symlink() or (root / "capabilities").is_symlink() or marker.read_text().strip() != value["lock"]:
        raise RuntimeError("Capability environment does not match its private lock")
    executable = environment_python(directory)
    if not executable.is_file(): raise RuntimeError("The selected capability interpreter is missing")
    return executable


def selected_server(root: Path) -> Path:
    value = config(root)
    if value is None: raise RuntimeError("Native capabilities are not installed")
    directory = root / "capabilities" / "servers" / value["server"]
    if any(path.is_symlink() for path in (directory, directory.parent, directory.parent.parent)):
        raise RuntimeError("Capability source directories must not be symlinked")
    manifest = directory / "bundle.json"
    runtime_bundle.safe_file(manifest, limit=16384)
    data = json.loads(manifest.read_text())
    if (not isinstance(data, dict) or not isinstance(data.get("files"), dict) or
        hashlib.sha256(runtime_bundle.canonical(data)).hexdigest() != value["server"] or
        data.get("lock") != value["lock"] or set(data.get("files", {})) != set(SERVER_SOURCES)):
        raise RuntimeError("The selected capability source identity is invalid")
    for name, digest in data["files"].items():
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError("Invalid capability source digest")
        runtime_bundle.safe_file(directory / name)
        if runtime_bundle.file_hash(directory / name) != digest:
            raise RuntimeError("The selected capability source was modified")
    return directory / "native_tools.py"


def freeze_server(root: Path, identity: str) -> str:
    """Snapshot the reviewed entry point and all local imports for new launches."""
    data = {"lock": identity, "files": {name: runtime_bundle.file_hash(root / name) for name in SERVER_SOURCES}}
    source_identity = hashlib.sha256(runtime_bundle.canonical(data)).hexdigest()
    sources = root / "capabilities" / "servers"; runtime_bundle.private_dir(sources)
    snapshot = sources / source_identity; runtime_bundle.private_dir(snapshot)
    for name in SERVER_SOURCES:
        destination = snapshot / name
        if not destination.exists(): runtime_bundle.write_file(destination, (root / name).read_bytes(), 0o700)
        elif runtime_bundle.file_hash(destination) != data["files"][name]:
            raise RuntimeError("An immutable capability snapshot was modified")
    if not (snapshot / "bundle.json").exists(): runtime_bundle.write_file(snapshot / "bundle.json", runtime_bundle.canonical(data), 0o600)
    return source_identity


def install(root: Path) -> None:
    if sys.version_info < (3, 11): raise RuntimeError("Optional native capabilities require Python 3.11 or newer")
    lock = root / "requirements-capabilities.lock"
    runtime_bundle.safe_file(lock, limit=512 << 10)
    identity = hashlib.sha256(lock.read_bytes()).hexdigest()
    directory = root / "capabilities"; runtime_bundle.private_dir(directory)
    environment = directory / ("env-" + identity)
    marker = environment / ".bridge-capability-lock"
    if not marker.exists():
        if environment.exists(): raise RuntimeError("An incomplete capability environment exists; inspect it before reinstalling")
        runtime_bundle.private_dir(environment)
        print("Creating an isolated, hash-locked capability environment", flush=True)
        venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(environment)
        allowed = ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP",
                   "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")
        child_env = {name: os.environ[name] for name in allowed if name in os.environ}
        log = environment / "install.log"
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            result = subprocess.run([str(environment_python(environment)), "-m", "pip", "--isolated", "install",
                "--only-binary=:all:", "--require-hashes", "--index-url", "https://pypi.org/simple", "-r", str(lock)],
                cwd=environment, env=child_env, stdout=output, stderr=subprocess.STDOUT)
        if result.returncode: raise RuntimeError("Hash-locked dependency installation failed; see the private capability install.log")
        runtime_bundle.write_file(marker, (identity + "\n").encode(), 0o600)
    source_identity = freeze_server(root, identity)
    value = {"format": 1, "enabled": True, "lock": identity, "server": source_identity}
    runtime_bundle.write_file(root / "capabilities.json", runtime_bundle.canonical(value), 0o600)
    if os.name == "nt":
        import windows_acl
        windows_acl.lock([directory, environment, marker, root / "capabilities.json"])
    selected_python(root)
    selected_server(root)
    print("Native image, document and realtime MCP tools enabled for new claude-codex launches")


def launch_flags(root: Path, directory: Path, workspace: Path, arguments: list[str], route: str) -> tuple[list[str], list[str]]:
    explicit = "--native-tools" in arguments
    disabled = "--no-native-tools" in arguments
    clean = [value for value in arguments if value not in ("--native-tools", "--no-native-tools")]
    if explicit and disabled: raise ValueError("Choose --native-tools or --no-native-tools")
    value = config(root)
    enabled = explicit or bool(value and value["enabled"] and "--strict-mcp-config" not in arguments)
    if disabled or not enabled: return [], clean
    if route != "subscription":
        if explicit: raise ValueError("These native capabilities use the subscription route; the API profile remains separate")
        return [], clean
    executable = selected_python(root)
    data = {"mcpServers": {"codex_native": {"command": str(executable), "args": [str(selected_server(root)),
             "--root", str(root), "--workspace", str(workspace.resolve())]}}}
    path = directory / "native-mcp.json"
    runtime_bundle.write_file(path, runtime_bundle.canonical(data), 0o600)
    if os.name == "nt":
        import windows_acl
        windows_acl.lock([path])
    # Place this variadic option before --settings/--model, so a positional
    # user prompt cannot accidentally become another MCP config filename.
    return ["--mcp-config", str(path)], clean


def main(arguments: list[str], *, root: Path) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "enable", "disable"))
    args = parser.parse_args(arguments)
    if args.action == "install": install(root); return 0
    value = config(root)
    if args.action == "status":
        print(json.dumps({"installed": value is not None, "enabled": bool(value and value["enabled"]),
                          "interpreter_ready": bool(value and selected_python(root).is_file()),
                          "source_ready": bool(value and selected_server(root).is_file()),
                          "no_project_api_key": True, "microphone_capture": False}))
        return 0
    if value is None: raise RuntimeError("Run claude-codex tools install first")
    selected_python(root)
    value["enabled"] = args.action == "enable"
    runtime_bundle.write_file(root / "capabilities.json", runtime_bundle.canonical(value), 0o600)
    print("Native capabilities " + ("enabled" if value["enabled"] else "disabled") + " for future launches")
    return 0
