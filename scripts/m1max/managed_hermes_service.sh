#!/usr/bin/env bash
set -Eeuo pipefail

RUNTIME_CONFIG_DIR="${RECEPTION_RUNTIME_CONFIG_DIR:-$HOME/.config/reachy-reception}"
PRODUCTION_ENV_FILE="$RUNTIME_CONFIG_DIR/production.env"
set -a
# shellcheck disable=SC1090
source "$PRODUCTION_ENV_FILE"
set +a

HERMES_BIN="${HERMES_BIN:-$HOME/.hermes/hermes-agent/venv/bin/hermes}"
HERMES_PROFILE="${HERMES_PROFILE:-reachyclinic}"
exec "$HERMES_BIN" -p "$HERMES_PROFILE" gateway run --replace --accept-hooks
