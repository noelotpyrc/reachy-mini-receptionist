#!/usr/bin/env bash
set -euo pipefail

REACHY_REPO="${REACHY_REPO:-/Users/leon/projects/reachy_mini_receptionist_deploy}"
BACKEND_DIR="${BACKEND_DIR:-/Users/leon/projects/speech_to_speech_backend}"
ARTIFACT_ROOT="${ARTIFACT_ROOT:-$REACHY_REPO/artifacts/official-runtime-live}"

usage() {
  cat <<'EOF'
Usage: scripts/m1max/recover_audio_review_text.sh <run_id> [reception-audio-review options]

Transcribes per-response WAVs whose backend assistant transcript was not logged, using the
m1max speech_to_speech Parakeet STT runtime, and writes:

  artifacts/official-runtime-live/audio-review/<run_id>/recovered-text-<run_id>.jsonl

Examples:
  scripts/m1max/recover_audio_review_text.sh official-live-20260625-133754
  scripts/m1max/recover_audio_review_text.sh official-live-20260625-133754 --overwrite-recovered-text

Environment:
  REACHY_REPO=/Users/leon/projects/reachy_mini_receptionist_deploy
  BACKEND_DIR=/Users/leon/projects/speech_to_speech_backend
  ARTIFACT_ROOT=$REACHY_REPO/artifacts/official-runtime-live
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 0
fi

RUN_ID="$1"
shift

BACKEND_PYTHON="$BACKEND_DIR/.venv/bin/python"
if [[ ! -x "$BACKEND_PYTHON" ]]; then
  echo "Missing backend Python: $BACKEND_PYTHON" >&2
  exit 2
fi

cd "$REACHY_REPO"
export PYTHONPATH="$REACHY_REPO/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$BACKEND_PYTHON" -m reachy_mini_brain.official_runtime.audio_review \
  "$ARTIFACT_ROOT" \
  --run-id "$RUN_ID" \
  --recover-missing-text \
  "$@"
