#!/usr/bin/env bash
set -Eeuo pipefail

RUNTIME_CONFIG_DIR="${RECEPTION_RUNTIME_CONFIG_DIR:-$HOME/.config/reachy-reception}"
ACTIVE_RELEASE_FILE="$RUNTIME_CONFIG_DIR/active-release"
PRODUCTION_ENV_FILE="$RUNTIME_CONFIG_DIR/production.env"
RELEASE="$(/usr/bin/sed -n '1p' "$ACTIVE_RELEASE_FILE")"

[[ -d "$RELEASE" ]] || { printf 'Invalid active release: %s\n' "$RELEASE" >&2; exit 64; }
if [[ -f "$RELEASE/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$RELEASE/.env"
  set +a
fi
set -a
# shellcheck disable=SC1090
source "$PRODUCTION_ENV_FILE"
set +a

export REACHY_REPO="$RELEASE"
export OFFICIAL_RUNTIME_PYTHON="$RELEASE/.release-venv/bin/python"
export PYTHONPATH="$RELEASE/src"
export ENV_FILE="$RELEASE/.env"
exec "$RELEASE/scripts/m1max/run_s2s_backend.sh"
