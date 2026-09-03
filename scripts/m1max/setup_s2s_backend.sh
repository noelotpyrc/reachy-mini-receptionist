#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="${BACKEND_DIR:-/Users/leon/projects/speech_to_speech_backend}"
BACKEND_VERSION="${S2S_BACKEND_VERSION:-0.2.10}"
BACKEND_FORK_URL="${S2S_BACKEND_FORK_URL:-https://github.com/noelotpyrc/speech-to-speech.git}"
BACKEND_FORK_SHA="${S2S_BACKEND_FORK_SHA:-a963ca68b9aa3599b7ea5eeabb9505a68263fbff}"
S2S_HOST="${S2S_HOST:-127.0.0.1}"
S2S_PORT="${S2S_PORT:-8765}"
PYTHON_BIN="${PYTHON_BIN:-}"
UV_BIN="${UV_BIN:-}"

DRY_RUN=0
CHECK_RUNNING=1

usage() {
  cat <<'EOF'
Usage: scripts/m1max/setup_s2s_backend.sh [options]

Create or update the managed Hugging Face speech-to-speech backend runtime folder on m1max.
This script owns the backend venv only; it does not delete backend logs, model caches, or run
artifacts.

Options:
  --backend-dir DIR       Runtime folder to create/update.
  --version VERSION       Expected speech-to-speech package version after installing the fork.
  --fork-url URL          Git repository containing the speech-to-speech fork.
  --fork-sha SHA          Exact fork commit to install; branch names are rejected.
  --python PATH           Python 3.12+ executable used to create the venv.
  --uv PATH               uv executable used to create a Python 3.12 venv when needed.
  --skip-running-check    Allow setup even if the backend port is listening.
  --dry-run               Print actions without changing files or installing packages.
  -h, --help              Show this help.

Environment:
  BACKEND_DIR=/Users/leon/projects/speech_to_speech_backend
  S2S_BACKEND_VERSION=0.2.10
  S2S_BACKEND_FORK_URL=https://github.com/noelotpyrc/speech-to-speech.git
  S2S_BACKEND_FORK_SHA=a963ca68b9aa3599b7ea5eeabb9505a68263fbff
  S2S_HOST=127.0.0.1
  S2S_PORT=8765
  PYTHON_BIN=python3.12
  UV_BIN=/Users/leon/.local/bin/uv
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-dir)
      BACKEND_DIR="$2"
      shift 2
      ;;
    --version)
      BACKEND_VERSION="$2"
      shift 2
      ;;
    --fork-url)
      BACKEND_FORK_URL="$2"
      shift 2
      ;;
    --fork-sha)
      BACKEND_FORK_SHA="$2"
      shift 2
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --uv)
      UV_BIN="$2"
      shift 2
      ;;
    --skip-running-check)
      CHECK_RUNNING=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

log() {
  printf '[setup-s2s] %s\n' "$*" >&2
}

run() {
  printf '[setup-s2s] run:' >&2
  printf ' %q' "$@" >&2
  printf '\n' >&2
  if [[ "$DRY_RUN" -eq 0 ]]; then
    "$@"
  fi
}

choose_python() {
  local candidates=()
  if [[ -n "$PYTHON_BIN" ]]; then
    candidates=("$PYTHON_BIN")
  else
    candidates=(
      python3.12
      /opt/homebrew/bin/python3.12
      /usr/local/bin/python3.12
      python3
      /opt/homebrew/bin/python3
      /usr/local/bin/python3
      /usr/bin/python3
    )
  fi

  local candidate path
  for candidate in "${candidates[@]}"; do
    if [[ "$candidate" == */* ]]; then
      [[ -x "$candidate" ]] || continue
      path="$candidate"
    else
      path="$(command -v "$candidate" 2>/dev/null || true)"
      [[ -n "$path" ]] || continue
    fi
    if "$path" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
    then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

choose_uv() {
  local candidates=()
  if [[ -n "$UV_BIN" ]]; then
    candidates=("$UV_BIN")
  else
    candidates=(
      uv
      /Users/leon/.local/bin/uv
      /opt/homebrew/bin/uv
      /usr/local/bin/uv
    )
  fi

  local candidate path
  for candidate in "${candidates[@]}"; do
    if [[ "$candidate" == */* ]]; then
      [[ -x "$candidate" ]] || continue
      path="$candidate"
    else
      path="$(command -v "$candidate" 2>/dev/null || true)"
      [[ -n "$path" ]] || continue
    fi
    printf '%s\n' "$path"
    return 0
  done
  return 1
}

PYTHON_BIN="$(choose_python || true)"
UV_BIN="$(choose_uv || true)"
VENV_DIR="$BACKEND_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
BACKEND_CLI="$VENV_DIR/bin/speech-to-speech"
INFO_PATH="$BACKEND_DIR/runtime-info.json"
INSTALL_SPEC="git+${BACKEND_FORK_URL}@${BACKEND_FORK_SHA}"

if [[ ! "$BACKEND_FORK_SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "S2S_BACKEND_FORK_SHA must be a full 40-character Git commit SHA; got: $BACKEND_FORK_SHA" >&2
  exit 2
fi

log "backend_dir=$BACKEND_DIR"
log "speech-to-speech==$BACKEND_VERSION"
log "speech-to-speech fork=$BACKEND_FORK_URL@$BACKEND_FORK_SHA"
if [[ -n "$PYTHON_BIN" ]]; then
  log "python=$PYTHON_BIN"
else
  log "python=not found; will use uv for venv creation if needed"
fi
if [[ -n "$UV_BIN" ]]; then
  log "uv=$UV_BIN"
fi

if [[ "$CHECK_RUNNING" -eq 1 ]] && command -v nc >/dev/null 2>&1; then
  if nc -z "$S2S_HOST" "$S2S_PORT" >/dev/null 2>&1; then
    echo "S2S backend appears to be running on ${S2S_HOST}:${S2S_PORT}; stop it before updating the venv, or pass --skip-running-check." >&2
    exit 3
  fi
fi

run mkdir -p "$BACKEND_DIR" "$BACKEND_DIR/logs"

if [[ ! -d "$VENV_DIR" ]]; then
  if [[ -n "$UV_BIN" ]]; then
    run "$UV_BIN" venv --python 3.12 "$VENV_DIR"
  elif [[ -n "$PYTHON_BIN" ]]; then
    run "$PYTHON_BIN" -m venv "$VENV_DIR"
  else
    echo "No Python 3.12+ or uv executable found; pass --python PATH or --uv PATH." >&2
    exit 4
  fi
else
  log "using existing venv: $VENV_DIR"
fi

if [[ "$DRY_RUN" -eq 0 && ! -x "$VENV_PYTHON" ]]; then
  echo "Venv Python is missing after setup: $VENV_PYTHON" >&2
  exit 4
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  "$VENV_PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit(f"Backend venv must use Python 3.12+, got {sys.version.split()[0]}")
PY
fi

if [[ -n "$UV_BIN" ]]; then
  run "$UV_BIN" pip install --python "$VENV_PYTHON" --upgrade pip setuptools wheel
  run "$UV_BIN" pip install --python "$VENV_PYTHON" --upgrade "$INSTALL_SPEC"
else
  run "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel
  run "$VENV_PYTHON" -m pip install --upgrade "$INSTALL_SPEC"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "would verify package version, CLI, and Parakeet STT handler import"
  log "would write $INFO_PATH"
  exit 0
fi

"$VENV_PYTHON" - "$BACKEND_VERSION" <<'PY'
import importlib.metadata as metadata
import sys

expected = sys.argv[1]
actual = metadata.version("speech-to-speech")
if actual != expected:
    raise SystemExit(f"speech-to-speech version mismatch: expected {expected}, got {actual}")

from speech_to_speech.STT.parakeet_tdt_handler import ParakeetTDTSTTHandler  # noqa: F401
PY

if [[ ! -x "$BACKEND_CLI" ]]; then
  echo "Missing backend CLI after install: $BACKEND_CLI" >&2
  exit 5
fi

"$BACKEND_CLI" --help >/dev/null

"$VENV_PYTHON" - "$INFO_PATH" "$BACKEND_VERSION" "$BACKEND_CLI" "$BACKEND_FORK_URL" "$BACKEND_FORK_SHA" <<'PY'
from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "managed_by": "reachy_mini_receptionist",
    "setup_script": "scripts/m1max/setup_s2s_backend.sh",
    "generated_on": datetime.now(timezone.utc).isoformat(),
    "speech_to_speech_version": sys.argv[2],
    "speech_to_speech_cli": sys.argv[3],
    "speech_to_speech_fork_url": sys.argv[4],
    "speech_to_speech_fork_sha": sys.argv[5],
    "python": sys.executable,
    "python_version": platform.python_version(),
    "platform": platform.platform(),
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

log "backend runtime ready: $BACKEND_DIR"
log "cli: $BACKEND_CLI"
log "runtime info: $INFO_PATH"
