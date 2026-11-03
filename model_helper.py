"""Minimal launch policy for the stdlib-only model-catalog helper."""

from __future__ import annotations

import os
from pathlib import Path
import sys


MODEL_HELPER_ENVIRONMENT = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "APPDATA", "LOCALAPPDATA", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_BRIDGE_PORT", "CODEX_BRIDGE_KEY_FILE", "CODEX_BRIDGE_ROUTE",
    "CODEX_BRIDGE_API_METADATA_FILE", "CLAUDE_CODEX_PDF_GUARD", "CLAUDE_CODEX_DEBUG_ACL",
)


def model_helper_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Give the catalog helper only its OS and explicit routing settings."""
    source = os.environ if environment is None else environment
    return {name: source[name] for name in MODEL_HELPER_ENVIRONMENT if name in source}


def model_helper_command(script: Path, *arguments: str) -> list[str]:
    # The helper needs its trusted script directory for sibling modules on
    # Windows. -E ignores PYTHON* settings and -S avoids site customization.
    return [sys.executable, "-E", "-S", "-X", "utf8", str(script), *arguments]
