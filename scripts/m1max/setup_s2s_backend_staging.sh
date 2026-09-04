#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export BACKEND_DIR="${S2S_STAGING_BACKEND_DIR:-/Users/leon/projects/speech_to_speech_backend_migration}"
export S2S_BACKEND_VERSION="0.2.12"
export S2S_BACKEND_FORK_URL="${S2S_STAGING_FORK_URL:-https://github.com/noelotpyrc/speech-to-speech.git}"
export S2S_BACKEND_FORK_SHA="${S2S_STAGING_FORK_SHA:-2e4449c345c305e4ee6b9761f86c1849bbf3cb08}"
export S2S_HOST="${S2S_STAGING_HOST:-127.0.0.1}"
export S2S_PORT="${S2S_STAGING_PORT:-8766}"
export S2S_EXPECTED_MLX_VERSION="0.31.1"
export S2S_EXPECTED_MLX_AUDIO_VERSION="0.4.2"
export S2S_EXPECTED_MLX_LM_VERSION="0.31.1"
export S2S_EXPECTED_MLX_METAL_VERSION="0.31.1"

exec "$SCRIPT_DIR/setup_s2s_backend.sh" "$@"
