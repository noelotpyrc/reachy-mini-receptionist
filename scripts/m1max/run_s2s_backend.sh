#!/usr/bin/env bash
set -euo pipefail

BACKEND_DIR="${BACKEND_DIR:-/Users/leon/projects/speech_to_speech_backend}"
ENV_FILE="${ENV_FILE:-/Users/leon/projects/reachy_mini_receptionist_deploy/.env}"

S2S_HOST="${S2S_HOST:-127.0.0.1}"
S2S_PORT="${S2S_PORT:-8765}"
S2S_VOICE="${S2S_VOICE:-Sohee}"
S2S_PROVIDER="${S2S_PROVIDER:-auto}"
S2S_MODEL_NAME="${S2S_MODEL_NAME:-}"
S2S_LOG_LEVEL="${S2S_LOG_LEVEL:-info}"
S2S_NUM_PIPELINES="${S2S_NUM_PIPELINES:-1}"
S2S_RESPONSES_BASE_URL="${S2S_RESPONSES_BASE_URL:-}"
S2S_RESPONSES_API_KEY="${S2S_RESPONSES_API_KEY:-}"
S2S_RESPONSES_CONVERSATION="${S2S_RESPONSES_CONVERSATION:-0}"
S2S_RESPONSES_CONVERSATION_PREFIX="${S2S_RESPONSES_CONVERSATION_PREFIX:-s2s}"
S2S_RESPONSES_DIRECT_BASE_URL="${S2S_RESPONSES_DIRECT_BASE_URL:-}"
S2S_RESPONSES_DIRECT_MODEL="${S2S_RESPONSES_DIRECT_MODEL:-}"
S2S_RESPONSES_DIRECT_API_KEY="${S2S_RESPONSES_DIRECT_API_KEY:-}"

if nc -z "$S2S_HOST" "$S2S_PORT" >/dev/null 2>&1; then
  echo "S2S backend port already listening on ${S2S_HOST}:${S2S_PORT}; not starting a duplicate backend." >&2
  exit 90
fi

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

RESPONSES_BASE_URL_ARGS=()
RESPONSES_THINKING_ARGS=()
RESPONSES_CONVERSATION_ARGS=()
RESPONSES_DIRECT_ARGS=()

case "$S2S_RESPONSES_CONVERSATION" in
  0|1) ;;
  *)
    echo "S2S_RESPONSES_CONVERSATION must be 0 or 1; got: $S2S_RESPONSES_CONVERSATION" >&2
    exit 2
    ;;
esac

if [[ "$S2S_RESPONSES_CONVERSATION" == "1" && -z "$S2S_RESPONSES_BASE_URL" ]]; then
  echo "S2S_RESPONSES_CONVERSATION=1 requires S2S_RESPONSES_BASE_URL for a conversation-compatible wrapper." >&2
  exit 2
fi

if [[ "$S2S_RESPONSES_CONVERSATION" == "1" ]]; then
  RESPONSES_CONVERSATION_ARGS=(
    --responses_api_conversation
    --responses_api_conversation_prefix "$S2S_RESPONSES_CONVERSATION_PREFIX"
  )
  if [[ -n "$S2S_RESPONSES_DIRECT_BASE_URL" ]]; then
    RESPONSES_DIRECT_ARGS+=(--responses_api_direct_base_url "$S2S_RESPONSES_DIRECT_BASE_URL")
  fi
  if [[ "$S2S_RESPONSES_DIRECT_BASE_URL" == "https://openrouter.ai/api/v1" ]]; then
    RESPONSES_THINKING_ARGS=(--no_responses_api_disable_thinking)
  fi
  if [[ -n "$S2S_RESPONSES_DIRECT_MODEL" ]]; then
    RESPONSES_DIRECT_ARGS+=(--responses_api_direct_model_name "$S2S_RESPONSES_DIRECT_MODEL")
  fi
  if [[ -z "$S2S_RESPONSES_DIRECT_API_KEY" && "$S2S_RESPONSES_DIRECT_BASE_URL" == "https://openrouter.ai/api/v1" ]]; then
    S2S_RESPONSES_DIRECT_API_KEY="${OPENROUTER_API_KEY:-}"
  fi
  if [[ -n "$S2S_RESPONSES_DIRECT_API_KEY" ]]; then
    RESPONSES_DIRECT_ARGS+=(--responses_api_direct_api_key "$S2S_RESPONSES_DIRECT_API_KEY")
  elif [[ "$S2S_RESPONSES_DIRECT_BASE_URL" == "https://openrouter.ai/api/v1" ]]; then
    echo "The direct OpenRouter lane requires S2S_RESPONSES_DIRECT_API_KEY or OPENROUTER_API_KEY." >&2
    exit 2
  fi
fi

if [[ -n "$S2S_RESPONSES_API_KEY" && -z "${OPENAI_API_KEY:-}" ]]; then
  export OPENAI_API_KEY="$S2S_RESPONSES_API_KEY"
fi

if [[ -z "${OPENAI_API_KEY:-}" && -n "${OPENROUTER_API_KEY:-}" && -z "$S2S_RESPONSES_BASE_URL" ]]; then
  export OPENAI_API_KEY="$OPENROUTER_API_KEY"
  if [[ "$S2S_PROVIDER" == "auto" ]]; then
    S2S_PROVIDER="openrouter"
  fi
fi

if [[ -n "$S2S_RESPONSES_BASE_URL" ]]; then
  RESPONSES_BASE_URL_ARGS=(--responses_api_base_url "$S2S_RESPONSES_BASE_URL")
  S2S_MODEL_NAME="${S2S_MODEL_NAME:-wrapper-routed}"
  if [[ -z "${OPENAI_API_KEY:-}" ]]; then
    export OPENAI_API_KEY="local-wrapper"
  fi
elif [[ "$S2S_PROVIDER" == "openrouter" ]]; then
  RESPONSES_BASE_URL_ARGS=(--responses_api_base_url "https://openrouter.ai/api/v1")
  RESPONSES_THINKING_ARGS=(--no_responses_api_disable_thinking)
  S2S_MODEL_NAME="${S2S_MODEL_NAME:-openai/gpt-5.4-mini}"
else
  S2S_MODEL_NAME="${S2S_MODEL_NAME:-gpt-5.4-mini}"
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "Missing OPENAI_API_KEY. Add OPENAI_API_KEY or OPENROUTER_API_KEY to $ENV_FILE." >&2
  exit 2
fi

exec "$BACKEND_DIR/.venv/bin/speech-to-speech" \
  --mode realtime \
  --ws_host "$S2S_HOST" \
  --ws_port "$S2S_PORT" \
  --num_pipelines "$S2S_NUM_PIPELINES" \
  --log_level "$S2S_LOG_LEVEL" \
  --thresh 0.6 \
  --stt parakeet-tdt \
  --llm_backend responses-api \
  --tts qwen3 \
  --model_name "$S2S_MODEL_NAME" \
  --chat_size 30 \
  --responses_api_stream \
  ${RESPONSES_BASE_URL_ARGS[@]+"${RESPONSES_BASE_URL_ARGS[@]}"} \
  ${RESPONSES_THINKING_ARGS[@]+"${RESPONSES_THINKING_ARGS[@]}"} \
  ${RESPONSES_CONVERSATION_ARGS[@]+"${RESPONSES_CONVERSATION_ARGS[@]}"} \
  ${RESPONSES_DIRECT_ARGS[@]+"${RESPONSES_DIRECT_ARGS[@]}"} \
  --enable_live_transcription \
  --qwen3_tts_model_name Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice \
  --qwen3_tts_speaker "$S2S_VOICE" \
  --qwen3_tts_language auto \
  --qwen3_tts_non_streaming_mode true \
  --qwen3_tts_mlx_quantization 6bit
