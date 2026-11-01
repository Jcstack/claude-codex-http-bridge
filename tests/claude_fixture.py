"""Locate the integrity-locked native client without running npm install scripts."""

import os
from pathlib import Path
import platform


def isolated_environment(root: Path) -> dict[str, str]:
    """Give a synthetic Claude client its own home and Windows profile paths.

    The caller owns ``root``. This limits normal home-directory lookups and
    inherited environment variables; it is not an operating-system sandbox.
    """
    fixture_home = root / "fixture-home"
    fixture_home.mkdir(mode=0o700, exist_ok=True)
    allowed = ("PATH", "TMPDIR", "LANG", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["HOME"] = str(fixture_home)
    if os.name == "nt":
        roaming = fixture_home / "AppData" / "Roaming"
        local = fixture_home / "AppData" / "Local"
        roaming.mkdir(parents=True, exist_ok=True)
        local.mkdir(parents=True, exist_ok=True)
        environment.update(USERPROFILE=str(fixture_home), APPDATA=str(roaming), LOCALAPPDATA=str(local))
    return environment


def binary() -> Path:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())
    if system is None or arch is None:
        raise RuntimeError("Unsupported client-contract fixture platform")
    package = f"@anthropic-ai/claude-code-{system}-{arch}"
    path = Path(__file__).resolve().parent / "claude-client/node_modules" / package / ("claude.exe" if system == "win32" else "claude")
    if not path.is_file():
        raise RuntimeError("Pinned native package is missing; run npm ci for the fixture first")
    return path


if __name__ == "__main__":
    path = binary()
    destination = os.environ.get("GITHUB_ENV")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as handle:
            handle.write(f"CLAUDE_CODEX_TEST_BIN={path}\n")
    else:
        print(path)
