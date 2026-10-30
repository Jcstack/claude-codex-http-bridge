#!/usr/bin/env python3
"""Install a pinned CLIProxyAPI release and private local bridge configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shlex
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile


VERSION = "8.0.46"
ASSETS = {
    ("Darwin", "arm64"): ("darwin_aarch64", "f9cdb85ccd70602ce084ad0aa8576a057a5e7dd8ff63a64b229cd145c5e6652b"),
    ("Darwin", "x86_64"): ("darwin_amd64", "bf8ba3bd1990e115bfba6f61a93d83233974a5457540ebdd03df69d142f14efb"),
    ("Linux", "aarch64"): ("linux_aarch64", "07b792e0d2eedb46fcb6c9d1eda913b4c53413076a03199346a85a573fd00de7"),
    ("Linux", "x86_64"): ("linux_amd64", "471f5235c501c19d3dec85c06e7612f0fe5ce3f823ef75331c2097aad8ed3c62"),
    ("Windows", "arm64"): ("windows_aarch64", "83ede8ad3f97d9c8c09aa8097c432c9e377f7f5566f7ffbc10a9d9f74e07504c"),
    ("Windows", "x86_64"): ("windows_amd64", "b203fe25bb90ad94dc3a4936bf49d08f0c7eca1a2fc6c923d0b9eca59557c67b"),
}
SOURCE = Path(__file__).resolve().parent
INSTALL_MARKER = ".claude-codex-http-bridge-install"


def private_dir(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlinked directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def install_root(path: Path) -> None:
    marker = path / INSTALL_MARKER
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlinked installation root: {path}")
    if path.exists():
        if not path.is_dir():
            raise RuntimeError(f"Installation root is not a directory: {path}")
        if any(path.iterdir()):
            if marker.is_symlink() or not marker.is_file() or marker.read_text() != "claude-codex-http-bridge\n":
                raise RuntimeError(f"Refusing to change an unrelated nonempty directory: {path}")
    private_dir(path)
    if not marker.exists():
        write_file(marker, b"claude-codex-http-bridge\n", 0o600)
    elif platform.system() != "Windows" and marker.stat().st_mode & 0o777 != 0o600:
        raise RuntimeError(f"Installation marker must have 0600 permissions: {marker}")


def write_file(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_archive(root: Path, offline: Path | None) -> Path:
    system = platform.system()
    machine = platform.machine().lower()
    if machine == "amd64":
        machine = "x86_64"
    if system == "Windows" and machine == "aarch64":
        machine = "arm64"
    asset = ASSETS.get((system, machine))
    if asset is None:
        raise RuntimeError("Supported platforms: macOS, Linux, and Windows on ARM64 or x64")
    suffix, expected = asset
    extension = "zip" if system == "Windows" else "tar.gz"
    filename = f"CLIProxyAPI_{VERSION}_{suffix}.{extension}"
    if offline is not None:
        archive = offline
    else:
        archive = root / filename
        if not archive.is_file() or file_hash(archive) != expected:
            url = f"https://github.com/router-for-me/CLIProxyAPI/releases/download/v{VERSION}/{filename}"
            request = urllib.request.Request(url, headers={"User-Agent": "claude-codex-http-bridge-installer"})
            with urllib.request.urlopen(request, timeout=60) as response:
                with tempfile.NamedTemporaryFile(dir=root, prefix="download-", delete=False) as handle:
                    temporary = Path(handle.name)
                    try:
                        shutil.copyfileobj(response, handle)
                    except BaseException:
                        temporary.unlink(missing_ok=True)
                        raise
            os.replace(temporary, archive)
            archive.chmod(0o600)
    if file_hash(archive) != expected:
        raise RuntimeError("CLIProxyAPI archive SHA-256 does not match its pinned GitHub release digest")
    return archive


def install_proxy(root: Path, offline: Path | None) -> None:
    windows = platform.system() == "Windows"
    binary_name = "cli-proxy-api.exe" if windows else "cli-proxy-api"
    proxy = root / "bin" / binary_name
    checksum = root / "proxy.sha256"
    if proxy.is_symlink() or checksum.is_symlink():
        raise RuntimeError("Refusing symlinked proxy binary or checksum")
    if proxy.is_file() and checksum.is_file() and file_hash(proxy) == checksum.read_text().strip():
        return
    archive = verified_archive(root, offline)
    if windows:
        with zipfile.ZipFile(archive) as package:
            member = package.getinfo(binary_name)
            if member.is_dir() or not 0 < member.file_size <= 100 * 1024 * 1024:
                raise RuntimeError("Unexpected CLIProxyAPI binary in release archive")
            binary = package.read(member)
            write_file(root / "UPSTREAM-LICENSE", package.read("LICENSE"), 0o600)
    else:
        with tarfile.open(archive, "r:gz") as package:
            member = package.getmember(binary_name)
            if not member.isfile() or not 0 < member.size <= 100 * 1024 * 1024:
                raise RuntimeError("Unexpected CLIProxyAPI binary in release archive")
            stream = package.extractfile(member)
            if stream is None:
                raise RuntimeError("CLIProxyAPI binary is missing from release archive")
            with stream:
                binary = stream.read()
            license_stream = package.extractfile("LICENSE")
            if license_stream is not None:
                with license_stream:
                    write_file(root / "UPSTREAM-LICENSE", license_stream.read(), 0o600)
    write_file(proxy, binary, 0o700)
    write_file(checksum, (hashlib.sha256(binary).hexdigest() + "\n").encode(), 0o600)


def install_config(root: Path) -> None:
    config = root / "config.yaml"
    if config.exists():
        if config.is_symlink() or not config.is_file() or (
            platform.system() != "Windows" and config.stat().st_mode & 0o777 != 0o600
        ):
            raise RuntimeError("Existing config.yaml must be a private regular file (0600)")
        return
    template = (SOURCE / "config.example.yaml").read_text()
    for marker in ("__LOCAL_KEY__", "__MANAGEMENT_KEY__", "__AUTH_DIR__"):
        if template.count(marker) != 1:
            raise RuntimeError(f"Expected one {marker} marker in config template")
    text = template.replace("__LOCAL_KEY__", "sk-local-" + secrets.token_hex(32))
    text = text.replace("__MANAGEMENT_KEY__", secrets.token_hex(32))
    text = text.replace("__AUTH_DIR__", json.dumps(str(root / "auth")))
    write_file(config, text.encode(), 0o600)


def install_command(root: Path, bin_dir: Path, replace: bool) -> None:
    if bin_dir.is_symlink():
        raise RuntimeError(f"Refusing symlinked command directory: {bin_dir}")
    bin_dir.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows":
        command = bin_dir / "claude-codex.cmd"
        for path in (root, Path(sys.executable)):
            if any(character in str(path) for character in ('\r', '\n', '"', '^')):
                raise RuntimeError("Installation paths contain unsupported CMD characters")
        home = str(root).replace("%", "%%")
        python = str(Path(sys.executable)).replace("%", "%%")
        launcher = str(root / "launcher.py").replace("%", "%%")
        wrapper = (
            "@echo off\r\n"
            "setlocal DisableDelayedExpansion\r\n"
            f'set "CLAUDE_CODEX_BRIDGE_HOME={home}"\r\n'
            f'"{python}" "{launcher}" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n"
        ).encode()
    else:
        command = bin_dir / "claude-codex"
        wrapper = (
            "#!/usr/bin/env bash\n"
            "# managed by claude-codex-http-bridge\n"
            f"export CLAUDE_CODEX_BRIDGE_HOME={shlex.quote(str(root))}\n"
            f"exec {shlex.quote(str(root / 'run-claude-codex.sh'))} \"$@\"\n"
        ).encode()
    if command.exists() or command.is_symlink():
        if command.is_file() and not command.is_symlink() and command.read_bytes() == wrapper:
            command.chmod(0o755)
            return
        if not replace:
            raise RuntimeError(f"Existing {command} was not changed; rerun with --replace-command to replace it")
    write_file(command, wrapper, 0o755)


def add_windows_user_path(bin_dir: Path) -> None:
    import ctypes
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            value, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, value_type = "", winreg.REG_EXPAND_SZ
        if value_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            raise RuntimeError("Windows user PATH has an unsupported registry value type")
        entries = [entry for entry in value.split(";") if entry]
        normalized = {os.path.normcase(os.path.expandvars(entry)) for entry in entries}
        if os.path.normcase(str(bin_dir)) not in normalized:
            winreg.SetValueEx(key, "Path", 0, value_type, ";".join([*entries, str(bin_dir)]))
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, ctypes.byref(result))


def main() -> None:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required")
    parser = argparse.ArgumentParser(description=__doc__)
    windows = platform.system() == "Windows"
    default_root = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
                    if windows else Path.home() / ".local" / "share") / "claude-codex-http-bridge"
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--bin-dir", type=Path, help="Windows default: ROOT/bin; POSIX default: ~/.local/bin")
    parser.add_argument("--archive", type=Path, help="Use a local release archive with the same pinned SHA-256")
    parser.add_argument("--replace-command", action="store_true", help="Replace an existing claude-codex command")
    args = parser.parse_args()
    root = args.root.expanduser().absolute()
    bin_dir = (args.bin_dir.expanduser().absolute() if args.bin_dir else
               root / "bin" if windows else Path.home() / ".local" / "bin")
    if windows and bin_dir != root / "bin":
        raise RuntimeError("On Windows, the command directory must be ROOT/bin so it stays under the private ACL")
    install_root(root)
    if windows:
        import windows_acl
        windows_acl.lock([root])
    private_dir(root / "auth")
    private_dir(root / "bin")
    if windows:
        windows_acl.lock([root / "auth", root / "bin"])
    for filename in ("bridge_models.py", "launcher.py", "diagnostics.py", "session_models.py", "statusline_adapter.py", "codex_usage.py",
                     "api_profile.py", "accounts.py", "rolling_runtime.py", "runtime_bundle.py", "capabilities.py",
                     "native_client.py", "native_services.py", "native_tools.py", "requirements-capabilities.lock",
                     "desktop_adapter.py", "desktop_scope.py", "desktop_windows.py",
                     "webfetch_pdf_guard.py",
                     "windows_acl.py", "run-claude-codex.sh"):
        write_file(root / filename, (SOURCE / filename).read_bytes(), 0o700)
    import runtime_bundle
    write_file(root / "runtime-source-lock.json", runtime_bundle.canonical(runtime_bundle.source_lock()), 0o600)
    install_proxy(root, args.archive)
    install_config(root)
    install_command(root, bin_dir, args.replace_command)
    if windows:
        paths = [root, *root.rglob("*")]
        if any(path.is_symlink() for path in paths):
            raise RuntimeError("Refusing symlink inside the Windows installation root")
        windows_acl.lock(paths)
        if args.bin_dir is None:
            add_windows_user_path(bin_dir)
    print(f"Installed: {bin_dir / ('claude-codex.cmd' if windows else 'claude-codex')}")
    if windows:
        print("Open a new PowerShell or CMD terminal to pick up the user PATH change")
    print("Next: claude-codex codex-login and models, or claude-codex api key set and api models")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, tarfile.TarError, zipfile.BadZipFile, KeyError) as error:
        print(f"Install failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
