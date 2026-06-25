#!/usr/bin/env bash
set -euo pipefail

REACHY_REPO="${REACHY_REPO:-/Users/leon/projects/reachy_mini_receptionist_deploy}"
OFFICIAL_APP_REPO="${OFFICIAL_APP_REPO:-/Users/leon/projects/reachy_mini_conversation_app}"
ENV_FILE="${ENV_FILE:-$REACHY_REPO/.env}"
RUNTIME_PYTHON="${OFFICIAL_RUNTIME_PYTHON:-}"

if [[ -z "$RUNTIME_PYTHON" ]]; then
  if [[ -x "$REACHY_REPO/.venv/bin/python" ]]; then
    RUNTIME_PYTHON="$REACHY_REPO/.venv/bin/python"
  else
    RUNTIME_PYTHON="$OFFICIAL_APP_REPO/.venv/bin/python"
  fi
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

RUNTIME_VENV="$(cd "$(dirname "$RUNTIME_PYTHON")/.." && pwd)"
GSTREAMER_PYTHON_PATH=""
for candidate in "$RUNTIME_VENV"/lib/python*/site-packages/gstreamer_python/lib/python*/site-packages; do
  if [[ -d "$candidate" ]]; then
    GSTREAMER_PYTHON_PATH="$candidate"
    break
  fi
done

export PYTHONPATH="$REACHY_REPO/src${GSTREAMER_PYTHON_PATH:+:$GSTREAMER_PYTHON_PATH}${PYTHONPATH:+:$PYTHONPATH}"
export REACHY_MINI_CONVERSATION_APP_SRC="${REACHY_MINI_CONVERSATION_APP_SRC:-$OFFICIAL_APP_REPO/src}"
export HF_REALTIME_CONNECTION_MODE="${HF_REALTIME_CONNECTION_MODE:-local}"
export HF_REALTIME_WS_URL="${HF_REALTIME_WS_URL:-ws://100.127.86.67:8765/v1/realtime}"

if [[ "${ALLOW_LIVE_DUPLICATE:-0}" != "1" ]]; then
  existing_live_pids="$(pgrep -f "reachy_mini_brain.official_runtime.live_app" || true)"
  if [[ -n "$existing_live_pids" ]]; then
    echo "official-runtime live runner already active: ${existing_live_pids//$'\n'/ }" >&2
    echo "Stop it first with: scripts/m1max/live_ops.sh clean-stop" >&2
    exit 91
  fi
fi

exec "$RUNTIME_PYTHON" \
  -m reachy_mini_brain.official_runtime.live_app \
  --backend hf-official \
  --hf-connection-mode "$HF_REALTIME_CONNECTION_MODE" \
  --hf-realtime-ws-url "$HF_REALTIME_WS_URL" \
  "$@"
