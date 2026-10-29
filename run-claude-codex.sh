#!/usr/bin/env bash
set -euo pipefail
umask 077

bridge_root=${CLAUDE_CODEX_BRIDGE_HOME:-$HOME/.local/share/claude-codex-http-bridge}
python_bin=${CLAUDE_CODEX_PYTHON_BIN:-$(command -v python3 || true)}
if [[ -z "$python_bin" ]]; then
  echo 'Python 3.10+ is required. Set CLAUDE_CODEX_PYTHON_BIN if it is not on PATH.' >&2
  exit 1
fi
exec "$python_bin" "$bridge_root/launcher.py" "$@"
