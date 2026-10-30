"""Locate the integrity-locked native client without running npm install scripts."""

import os
from pathlib import Path
import platform


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
