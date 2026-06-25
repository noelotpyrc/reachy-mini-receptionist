#!/usr/bin/env bash
set -Eeuo pipefail

REACHY_REPO="${REACHY_REPO:-/Users/leon/projects/reachy_mini_receptionist_clean}"
OFFICIAL_APP_REPO="${OFFICIAL_APP_REPO:-/Users/leon/projects/reachy_mini_conversation_app}"
ROBOT_HOST="${ROBOT_HOST:-192.168.1.165}"
ROBOT_API="${ROBOT_API:-http://${ROBOT_HOST}:8000}"
S2S_HOST="${S2S_HOST:-127.0.0.1}"
S2S_PORT="${S2S_PORT:-8765}"
LIVE_DURATION="${LIVE_DURATION:-900}"
LOG_DIR="${LOG_DIR:-$REACHY_REPO/artifacts/logs}"
STOP_BACKEND_ON_EXIT="${STOP_BACKEND_ON_EXIT:-0}"
CONVERSATION_CUES="${CONVERSATION_CUES:-1}"
CAPTURE_VISION="${CAPTURE_VISION:-1}"
PREFLIGHT_WAV="${PREFLIGHT_WAV:-$REACHY_REPO/artifacts/official-runtime-live/audio/playable/audio-response-resp_db3304df3e804556b0aaa7ed7990048f-official-live-20260623-122844-01-pcm16.wav}"
POLICY_PREFLIGHT_DURATION="${POLICY_PREFLIGHT_DURATION:-90}"
POLICY_PREFLIGHT_TIMEOUT="${POLICY_PREFLIGHT_TIMEOUT:-30}"
POLICY_PREFLIGHT_GAP="${POLICY_PREFLIGHT_GAP:-3}"
PREFLIGHT_BETWEEN_PROBES_GAP="${PREFLIGHT_BETWEEN_PROBES_GAP:-3}"

LIVE_PATTERN="reachy_mini_brain.official_runtime.live_app"
BACKEND_PATTERN="speech-to-speech --mode realtime"
LIVE_PID=""

usage() {
  cat <<'EOF'
Usage: scripts/m1max/live_ops.sh <command>

Commands:
  status       Print m1max process state and robot REST status.
  clean-stop   Stop live runner, release media, sleep robot, disable motors.
  sleep        Release media, send goto_sleep, disable motors.
  wake         Acquire media, enable motors, send wake_up.
  backend      Start the S2S backend if the websocket port is not listening.
  stop-backend Stop only the S2S backend.
  stop-all     Stop live runner/backend, release media, sleep robot, disable motors.
  preflight    Run live-app playback probe, then scripted goodbye/greet policy probe.
  preflight-audio
               Snapshot robot state, play the known-good WAV through live-app audio sink, then clean up.
  preflight-policy-flow
               Programmatically trigger goodbye then greet through the live policy/backend/speaker path.
  clean-run    Full manual-stop live cycle: clean-stop -> backend -> wake -> live -> cleanup.

Environment:
  ROBOT_HOST=192.168.1.165
  S2S_HOST=127.0.0.1
  S2S_PORT=8765
  LIVE_DURATION=900
  STOP_BACKEND_ON_EXIT=0
  CONVERSATION_CUES=1
  CAPTURE_VISION=1
  PREFLIGHT_WAV=/Users/leon/projects/reachy_mini_receptionist_clean/artifacts/official-runtime-live/audio/playable/audio-response-resp_db3304df3e804556b0aaa7ed7990048f-official-live-20260623-122844-01-pcm16.wav
  POLICY_PREFLIGHT_DURATION=90
  POLICY_PREFLIGHT_TIMEOUT=30
  POLICY_PREFLIGHT_GAP=3           # gap between scripted goodbye and greet after goodbye audio finishes
  PREFLIGHT_BETWEEN_PROBES_GAP=3   # gap between playback probe and policy-flow probe
EOF
}

log() {
  printf '[live-ops] %s\n' "$*" >&2
}

robot_get() {
  curl -fsS "${ROBOT_API}$1"
}

robot_post() {
  curl -fsS -X POST "${ROBOT_API}$1"
}

live_pids() {
  pgrep -f "$LIVE_PATTERN" || true
}

backend_pids() {
  pgrep -f "$BACKEND_PATTERN" || true
}

kill_pids() {
  local label="$1"
  shift
  local pids=("$@")
  if [[ "${#pids[@]}" -eq 0 ]]; then
    return 0
  fi
  log "Stopping ${label}: ${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true
  sleep 2
  local alive=()
  local pid
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      alive+=("$pid")
    fi
  done
  if [[ "${#alive[@]}" -gt 0 ]]; then
    log "Hard-stopping stuck ${label}: ${alive[*]}"
    kill -KILL "${alive[@]}" 2>/dev/null || true
  fi
}

stop_live() {
  local -a pids
  pids=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(live_pids)
  if [[ "${#pids[@]}" -gt 0 ]]; then
    kill_pids "live runner" "${pids[@]}"
  fi
}

stop_backend() {
  local -a pids
  pids=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(backend_pids)
  if [[ "${#pids[@]}" -gt 0 ]]; then
    kill_pids "S2S backend" "${pids[@]}"
  fi
}

stop_running_moves() {
  local moves
  moves="$(robot_get "/api/move/running" 2>/dev/null || true)"
  if [[ -z "$moves" || "$moves" == "[]" ]]; then
    return 0
  fi

  local uuid
  while IFS= read -r uuid; do
    [[ -z "$uuid" ]] && continue
    log "Stopping running move: $uuid"
    curl -fsS \
      -X POST \
      -H 'Content-Type: application/json' \
      -d "{\"uuid\":\"$uuid\"}" \
      "${ROBOT_API}/api/move/stop" >/dev/null || true
  done < <(
    MOVES_JSON="$moves" python3 - <<'PY'
import json
import os

try:
    moves = json.loads(os.environ.get("MOVES_JSON", "[]"))
except json.JSONDecodeError:
    moves = []
for move in moves:
    if isinstance(move, dict) and move.get("uuid"):
        print(move["uuid"])
PY
  )
}

sleep_robot() {
  log "Releasing media and sleeping robot"
  stop_running_moves
  robot_post "/api/media/release" >/dev/null || true
  robot_post "/api/move/play/goto_sleep" >/dev/null || true
  sleep 3
  stop_running_moves
  robot_post "/api/motors/set_mode/disabled" >/dev/null || true
}

wake_robot() {
  log "Starting/acquiring robot media and waking robot"
  robot_post "/api/daemon/start?wake_up=false" >/dev/null || true
  robot_post "/api/media/acquire" >/dev/null
  robot_post "/api/motors/set_mode/enabled" >/dev/null
  robot_post "/api/move/play/wake_up" >/dev/null
  sleep 3
}

start_backend_if_needed() {
  if nc -z "$S2S_HOST" "$S2S_PORT" >/dev/null 2>&1; then
    log "S2S backend already listening on ${S2S_HOST}:${S2S_PORT}"
    return 0
  fi

  mkdir -p "$LOG_DIR"
  local logfile="$LOG_DIR/s2s-backend-live-$(date +%Y%m%d-%H%M%S).log"
  log "Starting S2S backend; log: $logfile"
  (
    cd "$REACHY_REPO"
    S2S_HOST="$S2S_HOST" S2S_PORT="$S2S_PORT" scripts/m1max/run_s2s_backend.sh
  ) >"$logfile" 2>&1 &
  local pid=$!

  local i
  for i in $(seq 1 45); do
    if nc -z "$S2S_HOST" "$S2S_PORT" >/dev/null 2>&1; then
      log "S2S backend ready on ${S2S_HOST}:${S2S_PORT} (pid $pid)"
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      log "S2S backend exited before ready; tailing log"
      tail -80 "$logfile" >&2 || true
      return 1
    fi
    sleep 1
  done

  log "S2S backend did not become ready; tailing log"
  tail -80 "$logfile" >&2 || true
  return 1
}

status() {
  printf '[live-ops] m1max processes\n'
  ps -ef | grep -E "$LIVE_PATTERN|$BACKEND_PATTERN" | grep -v grep || true
  printf '[live-ops] robot daemon\n'
  robot_get "/api/daemon/status" || true
  printf '\n'
  printf '[live-ops] robot media\n'
  robot_get "/api/media/status" || true
  printf '\n'
  printf '[live-ops] robot motors\n'
  robot_get "/api/motors/status" || true
  printf '\n'
  printf '[live-ops] running moves\n'
  robot_get "/api/move/running" || true
  printf '\n'
}

robot_snapshot() {
  local label="$1"
  printf '[live-ops] robot snapshot: %s\n' "$label"
  printf '[live-ops] daemon/status\n'
  robot_get "/api/daemon/status" || true
  printf '\n'
  printf '[live-ops] media/status\n'
  robot_get "/api/media/status" || true
  printf '\n'
  printf '[live-ops] motors/status\n'
  robot_get "/api/motors/status" || true
  printf '\n'
  printf '[live-ops] move/running\n'
  robot_get "/api/move/running" || true
  printf '\n'
  printf '[live-ops] volume/current\n'
  robot_get "/api/volume/current" || true
  printf '\n'
}

clean_stop() {
  stop_live
  sleep_robot
}

stop_all() {
  stop_live
  stop_backend
  sleep_robot
}

cleanup_after_run() {
  local status_code=$?
  trap - EXIT INT TERM
  if [[ -n "$LIVE_PID" ]] && kill -0 "$LIVE_PID" 2>/dev/null; then
    kill -TERM "$LIVE_PID" 2>/dev/null || true
    wait "$LIVE_PID" 2>/dev/null || true
  fi
  stop_live
  sleep_robot
  if [[ "$STOP_BACKEND_ON_EXIT" == "1" ]]; then
    stop_backend
  fi
  status
  exit "$status_code"
}

clean_run() {
  log "Clean start: stopping stale processes and putting robot to sleep"
  clean_stop
  start_backend_if_needed
  wake_robot
  status
  log "Starting one live runner. Watch separate official-runtime milestone lines; software_pipeline_initialized is not a UX-success signal."
  local -a cue_args
  cue_args=()
  if [[ "$CONVERSATION_CUES" == "1" ]]; then
    cue_args+=(--conversation-cues)
  else
    cue_args+=(--no-conversation-cues)
  fi
  local -a capture_args
  capture_args=()
  if [[ "$CAPTURE_VISION" == "1" ]]; then
    capture_args+=(--capture-vision)
  else
    capture_args+=(--no-capture-vision)
  fi
  trap cleanup_after_run EXIT INT TERM
  (
    cd "$REACHY_REPO"
    HF_REALTIME_WS_URL="ws://${S2S_HOST}:${S2S_PORT}/v1/realtime" \
      scripts/m1max/run_official_runtime_live.sh \
      --duration "$LIVE_DURATION" \
      --robot-host "$ROBOT_HOST" \
      --perception --gestures --audio-gate --ready-cue --warmup-video \
      "${cue_args[@]}" \
      "${capture_args[@]}"
  ) &
  LIVE_PID=$!
  wait "$LIVE_PID"
}

preflight_audio() {
  if [[ ! -f "$PREFLIGHT_WAV" ]]; then
    log "Missing PREFLIGHT_WAV: $PREFLIGHT_WAV"
    return 2
  fi

  log "Preflight audio probe using: $PREFLIGHT_WAV"
  log "Stopping stale live runner and putting robot in a known idle state"
  clean_stop
  robot_snapshot "before-wake"

  log "Waking robot and acquiring media for WebRTC playback probe"
  wake_robot
  robot_snapshot "after-wake"

  local run_id
  run_id="official-audio-preflight-$(date +%Y%m%d-%H%M%S)"
  log "Playing known-good WAV through live-app audio sink: $run_id"
  (
    cd "$REACHY_REPO"
    scripts/m1max/run_official_runtime_live.sh \
      --run-id "$run_id" \
      --duration 30 \
      --robot-host "$ROBOT_HOST" \
      --warmup-audio \
      --no-warmup-video \
      --record-audio \
      --no-record-video \
      --no-capture-vision \
      --no-perception \
      --no-gestures \
      --no-audio-gate \
      --no-ready-cue \
      --no-conversation-cues \
      --scripted-playback-wav "$PREFLIGHT_WAV" \
      --scripted-playback-post-roll-s 3.0
  )

  log "Playback probe finished; cleaning up robot state"
  clean_stop
  robot_snapshot "after-clean-stop"
  cat <<'EOF'
[live-ops] HUMAN CHECK REQUIRED:
[live-ops] - If the known-good WAV sounded smooth, proceed with clean-run.
[live-ops] - If it sounded choppy, do not start live conversation. Reboot/refresh robot runtime and rerun preflight-audio.
EOF
}

preflight_policy_flow() {
  log "Policy-flow preflight: scripted goodbye -> greet through live policy/backend/speaker path"
  log "Stopping stale live runner and putting robot in a known idle state"
  clean_stop
  start_backend_if_needed

  log "Waking robot and acquiring media for policy-flow probe"
  wake_robot
  status

  local run_id
  run_id="official-policy-preflight-$(date +%Y%m%d-%H%M%S)"
  log "Starting scripted policy flow run: $run_id"
  trap cleanup_after_run EXIT INT TERM
  (
    cd "$REACHY_REPO"
    HF_REALTIME_WS_URL="ws://${S2S_HOST}:${S2S_PORT}/v1/realtime" \
      scripts/m1max/run_official_runtime_live.sh \
      --run-id "$run_id" \
      --duration "$POLICY_PREFLIGHT_DURATION" \
      --robot-host "$ROBOT_HOST" \
      --no-perception --no-gestures --no-audio-gate --ready-cue --no-warmup-video \
      --no-conversation-cues --no-capture-vision \
      --scripted-policy-flow goodbye-greet \
      --scripted-policy-gap-s "$POLICY_PREFLIGHT_GAP" \
      --scripted-policy-timeout-s "$POLICY_PREFLIGHT_TIMEOUT"
  ) &
  LIVE_PID=$!

  set +e
  wait "$LIVE_PID"
  local run_status=$?
  set -e
  LIVE_PID=""
  trap - EXIT INT TERM

  log "Policy-flow probe finished with status $run_status; cleaning up robot state"
  clean_stop
  status
  if [[ "$STOP_BACKEND_ON_EXIT" == "1" ]]; then
    stop_backend
  fi

  cat <<EOF
[live-ops] POLICY PREFLIGHT COMPLETE:
[live-ops] - Run id: $run_id
[live-ops] - Expected physical output: one goodbye, then one welcome.
[live-ops] - Check events/audio artifacts under artifacts/official-runtime-live for response latency and WAV output.
EOF
  return "$run_status"
}

preflight() {
  log "Full preflight: playback probe followed by scripted goodbye/greet policy flow"
  preflight_audio
  log "Waiting ${PREFLIGHT_BETWEEN_PROBES_GAP}s before scripted policy flow"
  sleep "$PREFLIGHT_BETWEEN_PROBES_GAP"
  preflight_policy_flow
}

case "${1:-}" in
  status)
    status
    ;;
  clean-stop)
    clean_stop
    status
    ;;
  sleep)
    sleep_robot
    status
    ;;
  wake)
    wake_robot
    status
    ;;
  backend)
    start_backend_if_needed
    ;;
  stop-backend)
    stop_backend
    status
    ;;
  stop-all)
    stop_all
    status
    ;;
  preflight)
    preflight
    ;;
  preflight-audio)
    preflight_audio
    ;;
  preflight-policy-flow)
    preflight_policy_flow
    ;;
  clean-run)
    clean_run
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
