#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export BACKEND_DIR="${S2S_STAGING_BACKEND_DIR:-/Users/leon/projects/speech_to_speech_backend_migration}"
export S2S_BACKEND_VERSION="0.2.12"
export S2S_BACKEND_FORK_URL="${S2S_STAGING_FORK_URL:-https://github.com/noelotpyrc/speech-to-speech.git}"
export S2S_BACKEND_FORK_SHA="${S2S_STAGING_FORK_SHA:-aaa7c75e1f16a6ccdcd902ea94af92e325ebd455}"
export S2S_HOST="${S2S_STAGING_HOST:-127.0.0.1}"
export S2S_PORT="${S2S_STAGING_PORT:-8766}"

exec "$SCRIPT_DIR/setup_s2s_backend.sh" "$@"
