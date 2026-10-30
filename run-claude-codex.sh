#!/usr/bin/env bash
set -euo pipefail
umask 077

bridge_root=${CLAUDE_CODEX_BRIDGE_HOME:-$HOME/.local/share/claude-codex-http-bridge}
python_bin=${CLAUDE_CODEX_PYTHON_BIN:-$(command -v python3 || true)}
if [[ -z "$python_bin" ]]; then
  echo 'Python 3.10+ is required. Set CLAUDE_CODEX_PYTHON_BIN if it is not on PATH.' >&2
  exit 1
fi

if [[ -z "${TMUX_PANE:-}" ]] || ! command -v tmux >/dev/null 2>&1; then
  exec "$python_bin" "$bridge_root/launcher.py" "$@"
fi

window_id=$(tmux display-message -p -t "$TMUX_PANE" '#{window_id}' 2>/dev/null || true)
if [[ -z "$window_id" ]]; then
  exec "$python_bin" "$bridge_root/launcher.py" "$@"
fi
previous_name=$(tmux display-message -p -t "$window_id" '#{window_name}' 2>/dev/null || true)
previous_local_auto=$(tmux show-option -w -v -t "$window_id" automatic-rename 2>/dev/null || true)
if ! tmux rename-window -t "$window_id" claude-codex 2>/dev/null; then
  exec "$python_bin" "$bridge_root/launcher.py" "$@"
fi

restore_tmux_name() {
  local current_name
  current_name=$(tmux display-message -p -t "$window_id" '#{window_name}' 2>/dev/null || true)
  [[ "$current_name" == claude-codex ]] || return 0
  if [[ -z "$previous_local_auto" ]]; then
    tmux set-window-option -u -t "$window_id" automatic-rename >/dev/null 2>&1 || true
  else
    tmux set-window-option -t "$window_id" automatic-rename "$previous_local_auto" >/dev/null 2>&1 || true
  fi
  if [[ "$previous_local_auto" == off && -n "$previous_name" ]]; then
    tmux rename-window -t "$window_id" "$previous_name" >/dev/null 2>&1 || true
  fi
}
trap restore_tmux_name EXIT
"$python_bin" "$bridge_root/launcher.py" "$@"
