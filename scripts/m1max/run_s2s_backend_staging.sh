#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="${S2S_STAGING_BACKEND_DIR:-/Users/leon/projects/speech_to_speech_backend_migration}"
ENV_FILE="${ENV_FILE:-/Users/leon/projects/reachy_mini_receptionist_deploy/.env}"
S2S_HOST="${S2S_STAGING_HOST:-127.0.0.1}"
S2S_PORT="${S2S_STAGING_PORT:-8766}"
S2S_VOICE="${S2S_STAGING_VOICE:-Sohee}"
S2S_MODEL_NAME="${S2S_STAGING_MODEL_NAME:-openai/gpt-5.6-luna}"
S2S_RESPONSES_BASE_URL="${S2S_STAGING_RESPONSES_BASE_URL:-https://openrouter.ai/api/v1}"
S2S_LOG_LEVEL="${S2S_STAGING_LOG_LEVEL:-info}"
S2S_NUM_PIPELINES="${S2S_STAGING_NUM_PIPELINES:-1}"

if nc -z "$S2S_HOST" "$S2S_PORT" >/dev/null 2>&1; then
  echo "Staging S2S backend port already listening on ${S2S_HOST}:${S2S_PORT}; not starting a duplicate backend." >&2
  exit 90
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ "$S2S_RESPONSES_BASE_URL" == "https://openrouter.ai/api/v1" ]]; then
  if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
    echo "Missing OPENROUTER_API_KEY in $ENV_FILE for the isolated staging backend." >&2
    exit 2
  fi
  export OPENAI_API_KEY="$OPENROUTER_API_KEY"
elif [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "Missing OPENAI_API_KEY in $ENV_FILE for the isolated staging backend." >&2
  exit 2
fi

exec "$BACKEND_DIR/.venv/bin/speech-to-speech" \
  serve \
  --host "$S2S_HOST" \
  --port "$S2S_PORT" \
  --num_pipelines "$S2S_NUM_PIPELINES" \
  --log_level "$S2S_LOG_LEVEL" \
  --thresh 0.6 \
  --stt parakeet-tdt \
  --llm_backend responses-api \
  --tts qwen3 \
  --model_name "$S2S_MODEL_NAME" \
  --chat_size 30 \
  --responses_api_stream \
  --responses_api_base_url "$S2S_RESPONSES_BASE_URL" \
  --no_responses_api_disable_thinking \
  --no_smart_turn \
  --enable_live_transcription \
  --qwen3_tts_model_name Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice \
  --qwen3_tts_speaker "$S2S_VOICE" \
  --qwen3_tts_language auto \
  --qwen3_tts_non_streaming_mode true \
  --qwen3_tts_mlx_quantization 6bit
