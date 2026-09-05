#!/usr/bin/env bash
set -Eeuo pipefail

RUNTIME_CONFIG_DIR="${RECEPTION_RUNTIME_CONFIG_DIR:-$HOME/.config/reachy-reception}"
ACTIVE_RELEASE_FILE="${RECEPTION_ACTIVE_RELEASE_FILE:-$RUNTIME_CONFIG_DIR/active-release}"
PRODUCTION_ENV_FILE="${RECEPTION_PRODUCTION_ENV_FILE:-$RUNTIME_CONFIG_DIR/production.env}"

fail() {
  printf '[reception-prod] %s\n' "$*" >&2
  exit 64
}

[[ -f "$ACTIVE_RELEASE_FILE" ]] || fail "Missing active release file: $ACTIVE_RELEASE_FILE"
RELEASE="$(/usr/bin/sed -n '1p' "$ACTIVE_RELEASE_FILE")"
[[ -n "$RELEASE" && "$RELEASE" == /* ]] || fail "Active release must be one absolute path"
[[ "$(/usr/bin/wc -l < "$ACTIVE_RELEASE_FILE" | /usr/bin/tr -d ' ')" == "1" ]] || \
  fail "Active release file must contain exactly one line"
[[ -d "$RELEASE" ]] || fail "Active release does not exist: $RELEASE"

release_name="$(/usr/bin/basename "$RELEASE")"
[[ "$release_name" =~ ^reachy_mini_receptionist_release_([0-9a-f]{7,40})_frozen$ ]] || \
  fail "Active release is not a versioned frozen release: $release_name"
expected_revision="${BASH_REMATCH[1]}"
actual_revision="$(/usr/bin/git -C "$RELEASE" rev-parse HEAD 2>/dev/null)" || \
  fail "Active release is not a Git checkout: $RELEASE"
[[ "$actual_revision" == "$expected_revision"* ]] || \
  fail "Release directory revision $expected_revision does not match Git HEAD $actual_revision"
[[ -z "$(/usr/bin/git -C "$RELEASE" status --porcelain --untracked-files=no)" ]] || \
  fail "Active frozen release has tracked modifications: $RELEASE"

RUNTIME_PYTHON="$RELEASE/.release-venv/bin/python"
[[ -x "$RUNTIME_PYTHON" ]] || fail "Missing frozen release Python: $RUNTIME_PYTHON"
[[ -f "$PRODUCTION_ENV_FILE" ]] || fail "Missing production config: $PRODUCTION_ENV_FILE"

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
export OFFICIAL_RUNTIME_PYTHON="$RUNTIME_PYTHON"
export PYTHONPATH="$RELEASE/src"
export S2S_ENV_LOADED=1

exec "$RUNTIME_PYTHON" -m reachy_mini_brain.official_runtime.ops_cli "$@"
