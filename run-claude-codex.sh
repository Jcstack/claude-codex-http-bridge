#!/usr/bin/env bash
set -euo pipefail
umask 077

probe_root=${CLAUDE_CODEX_BRIDGE_HOME:-$HOME/.local/share/claude-codex-http-bridge}
proxy_bin=$probe_root/bin/cli-proxy-api
python_bin=${CLAUDE_CODEX_PYTHON_BIN:-$(command -v python3 || true)}
claude_bin=${CLAUDE_CODEX_CLAUDE_BIN:-$(command -v claude || true)}

if [[ "${1:-}" == --help ]]; then
  printf '%s\n' 'Usage: claude-codex [--model MODEL_ID|sol|astra|terra|luna] [Claude Code options]' \
    'Run claude-codex codex-login once. Run claude-codex models for live model IDs.' \
    'Default model: newest available sol.' \
    'Inside Claude Code, use /model to switch among live Codex models; press s for this session only.' \
    'Claude Code commands such as /effort and /autocompact remain available.'
  exit 0
fi

if [[ -z "$python_bin" ]]; then
  echo 'Python 3.10+ is required. Set CLAUDE_CODEX_PYTHON_BIN if it is not on PATH.' >&2
  exit 1
fi

login_codex=0
if [[ "${1:-}" == codex-login ]]; then
  if (($# != 1)); then
    echo 'The codex-login command does not take arguments.' >&2
    exit 2
  fi
  login_codex=1
  shift
fi

list_models=0
if [[ "${1:-}" == models || "${1:-}" == --list-models ]]; then
  if (($# != 1)); then
    echo 'The models command does not take arguments.' >&2
    exit 2
  fi
  list_models=1
  shift
fi

selected_model=sol
claude_args=()
while (($#)); do
  case "$1" in
    --model)
      if (($# < 2)); then
        echo 'Missing value after --model.' >&2
        exit 2
      fi
      selected_model=$2
      shift 2
      ;;
    --model=*)
      selected_model=${1#--model=}
      shift
      ;;
    --)
      claude_args+=("$@")
      break
      ;;
    *)
      claude_args+=("$1")
      shift
      ;;
  esac
done

for option in "${claude_args[@]}"; do
  if [[ "$option" == --bg || "$option" == --background ]]; then
    echo 'Background sessions require a persistent bridge; this private per-session launcher cannot keep their model connection alive after it exits.' >&2
    exit 2
  fi
done

if [[ -z "$claude_bin" || ! -x "$claude_bin" ]]; then
  echo 'Claude Code executable not found. Set CLAUDE_CODEX_CLAUDE_BIN to its path.' >&2
  exit 1
fi
"$python_bin" "$probe_root/bridge_models.py" verify

if [[ "$login_codex" == 1 ]]; then
  "$proxy_bin" -config "$probe_root/config.yaml" -codex-login
  exit
fi

session_info=$("$python_bin" "$probe_root/bridge_models.py" session)
IFS=$'\t' read -r probe_port session_dir session_config session_key session_log <<< "$session_info"
"$proxy_bin" -config "$session_config" >"$session_log" 2>&1 &
owned_proxy_pid=$!
cleanup() {
  kill "$owned_proxy_pid" >/dev/null 2>&1 || true
  wait "$owned_proxy_pid" >/dev/null 2>&1 || true
  rm -f "$session_config" "$session_key" "$session_log"
  rmdir "$session_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ready=0
for _ in {1..50}; do
  if ! kill -0 "$owned_proxy_pid" >/dev/null 2>&1; then
    echo "Local bridge exited during startup. Check its private session log." >&2
    exit 1
  fi
  if nc -z 127.0.0.1 "$probe_port" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.2
done
if [[ "$ready" != 1 ]]; then
  echo "Local bridge did not become ready." >&2
  exit 1
fi
listener_pid=$(lsof -nP -tiTCP:"$probe_port" -sTCP:LISTEN 2>/dev/null || true)
if [[ "$listener_pid" != "$owned_proxy_pid" ]]; then
  echo "Port $probe_port was taken by another process during startup." >&2
  exit 1
fi

export CODEX_BRIDGE_PORT=$probe_port
export CODEX_BRIDGE_KEY_FILE=$session_key
if [[ "$list_models" == 1 ]]; then
  "$python_bin" "$probe_root/bridge_models.py" list
  exit
fi

model_info=$("$python_bin" "$probe_root/bridge_models.py" prepare "$selected_model")
IFS=$'\t' read -r bridge_model context_window compact_window model_settings <<< "$model_info"

export ANTHROPIC_BASE_URL="http://127.0.0.1:$probe_port"
export ANTHROPIC_AUTH_TOKEN="$(<"$session_key")"
export ANTHROPIC_DEFAULT_OPUS_MODEL=$bridge_model
export ANTHROPIC_DEFAULT_SONNET_MODEL=$bridge_model
export ANTHROPIC_DEFAULT_HAIKU_MODEL=$bridge_model
export ANTHROPIC_DEFAULT_FABLE_MODEL=$bridge_model
export ENABLE_TOOL_SEARCH=true
unset ANTHROPIC_API_KEY CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY
# The generated modelSettings supplies a different compact window after /model switches.
# An inherited global override would mask those per-model values.
unset CLAUDE_CODE_SUBAGENT_MODEL CLAUDE_CODE_MAX_CONTEXT_TOKENS CLAUDE_CODE_AUTO_COMPACT_WINDOW
unset DISABLE_COMPACT CLAUDE_CODE_EXTRA_BODY

echo "Codex bridge active on 127.0.0.1:$probe_port: $bridge_model. Route context $context_window tokens; auto-compaction $compact_window tokens for this model." >&2
echo 'Use /model to switch among the live Codex models; press s to avoid saving a Codex model as the default for plain claude.' >&2
echo 'Claude Code may show a 1M context and Claude-list cost for this compatibility alias; those UI values are not the Codex route limits or billing.' >&2
"$claude_bin" --settings "$model_settings" --model "$bridge_model" "${claude_args[@]}"
