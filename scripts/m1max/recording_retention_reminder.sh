#!/usr/bin/env bash
set -Eeuo pipefail

STATE_DIR="${RECEPTION_RUNTIME_STATE_DIR:-$HOME/.local/state/reachy-reception}"
PROD_OPS="${RECEPTION_PROD_OPS:-$HOME/.local/bin/reception-prod}"
REPORT="${RECEPTION_RETENTION_REPORT:-$STATE_DIR/recording-retention-latest.json}"
mkdir -p "$STATE_DIR"

if [[ -x "$PROD_OPS" ]]; then
  "$PROD_OPS" --json-output recording-retention > "$REPORT"
else
  REPO="${RECEPTION_REPO:-}"
  RUNTIME_PYTHON="${RECEPTION_RUNTIME_PYTHON:-}"
  [[ -n "$REPO" && -x "$RUNTIME_PYTHON" ]] || {
    printf 'Neither reception-prod nor a local repo/Python pair is configured.\n' >&2
    exit 64
  }
  REACHY_REPO="$REPO" PYTHONPATH="$REPO/src" \
    "$RUNTIME_PYTHON" -m reachy_mini_brain.official_runtime.ops_cli \
    --json-output recording-retention > "$REPORT"
fi

due_count="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"]["due_file_count"])' "$REPORT")"
if [[ "$due_count" -gt 0 ]]; then
  message="$due_count reception recording files are older than the retention window. Review the report before approved cleanup."
  /usr/bin/osascript -e "display notification \"$message\" with title \"Reachy recording cleanup reminder\"" >/dev/null 2>&1 || true
  printf '%s\n' "$message"
fi
