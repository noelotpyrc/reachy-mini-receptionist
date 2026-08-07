# Runbook - bring up the reception robot

Offline profile, model, policy, and S2S checks are indexed in the
[Runtime Test Catalog](runtime-test-catalog.md). This runbook remains the source
of truth for physical preflight and live operation.

Production promotion gates and unresolved reliability/privacy decisions are tracked separately in
the [Production Readiness checklist](production-readiness.md). A successful command in this runbook
does not by itself mean the system is approved for unattended production.

## Current Live Path

Use the accepted official-runtime path for normal live tests:

```text
m1max deploy repo
  -> OPS CLI
  -> local Hugging Face speech-to-speech backend on :8765
  -> official_runtime.live_app --backend s2s-local
  -> Reachy Mini robot runtime
```

Do not start `official_runtime.live_app` directly for physical tests. Use OPS so stale runners,
backend state, media ownership, wake/sleep, and teardown are handled by one owner.

## Canonical Repos And Runtime Folders

Product/controller repo:

- Local dev: `/Users/noel/projects/reachy_mini_receptionist_clean`
- m1max rollback checkout: `/Users/leon/projects/reachy_mini_receptionist_deploy`
- Current clean m1max release: `/Users/leon/projects/reachy_mini_receptionist_release_6b4c5a6`

Keep the dirty rollback checkout intact. A prepared release has its own `.release-venv`, while its
ignored `.env`, `private`, and `artifacts` paths reference the existing deployment-owned data. Set
`REACHY_REPO` and `OFFICIAL_RUNTIME_PYTHON` explicitly when operating a release so the OPS process
and spawned runner use the same product revision.

S2S backend runtime folder:

- m1max runtime: `/Users/leon/projects/speech_to_speech_backend`
- Contains the `speech-to-speech==0.2.10` venv, backend logs, and runtime state.
- This is a managed external runtime folder, not product source code and not a place for product
  edits.

The product/controller repo owns backend lifecycle, launch flags, and documentation through
`scripts/m1max/setup_s2s_backend.sh`, `scripts/m1max/run_s2s_backend.sh`, and the OPS CLI. The
backend folder owns only the installed service runtime.

## Backend LLM Context And Model Knobs

Context ownership follows the backend mode. With `S2S_RESPONSES_CONVERSATION=1`, OPS launches the
live app with `--profile-owned-context`: Hermes owns persona, clinic facts, capabilities, and tool
policy, while the application sends an empty session prompt and the S2S voice adapter supplies only
generic spoken-output rules. Run artifacts record `instructions_source=hermes-profile`, the empty
prompt hash, and `instructions_chars=0`.

The legacy direct-only backend fallback still defaults to
`profiles/clinic_receptionist/instructions.txt`, because no Hermes profile supplies its context.

For direct OpenRouter model swaps, keep the backend launcher on OpenRouter and set the model:

```bash
S2S_PROVIDER=openrouter S2S_MODEL_NAME=openai/gpt-5.6-luna scripts/m1max/run_s2s_backend.sh
```

The current production-candidate backend uses the Hermes wrapper on `127.0.0.1:8642`, with direct
OpenRouter/GPT-5.6-Luna retained as its configured direct model path. Backend feature development is
paused during production preparation.

For a staging Hermes experiment, point the S2S Responses slot at the test wrapper's
OpenAI-compatible `/v1` endpoint (normally port `8643`, never the production profile):

```bash
S2S_RESPONSES_BASE_URL=http://127.0.0.1:8643/v1 \
S2S_MODEL_NAME=wrapper-routed \
S2S_RESPONSES_API_KEY=local-wrapper \
S2S_RESPONSES_CONVERSATION=1 \
scripts/m1max/run_s2s_backend.sh
```

OPS reads the same `S2S_RESPONSES_CONVERSATION` setting and automatically selects profile-owned
context for normal sessions and policy preflights. Put the selected deployment settings in the
deploy repo's `.env`; a one-command shell override used only to start the backend is not visible to a
later OPS process.

## Start A Live Test

Run from m1max:

```bash
ssh leon@100.127.86.67
RELEASE=/Users/leon/projects/reachy_mini_receptionist_release_6b4c5a6
cd "$RELEASE"
export REACHY_REPO="$RELEASE"
export OFFICIAL_RUNTIME_PYTHON="$RELEASE/.release-venv/bin/python"
export PYTHONPATH="$RELEASE/src"

# Safe status: backend + runner + latest-run pointer.
"$OFFICIAL_RUNTIME_PYTHON" -m reachy_mini_brain.official_runtime.ops_cli status

# Optional fuller read-only status, including robot daemon state.
"$OFFICIAL_RUNTIME_PYTHON" -m reachy_mini_brain.official_runtime.ops_cli status --include-robot
```

Run preflight before a normal live session when time allows:

```bash
"$OFFICIAL_RUNTIME_PYTHON" -m reachy_mini_brain.official_runtime.ops_cli \
  --confirm-physical preflight
```

Start the live session:

```bash
"$OFFICIAL_RUNTIME_PYTHON" -m reachy_mini_brain.official_runtime.ops_cli \
  --confirm-physical start-session --record-audio --record-video --capture-vision
```

The vision greet/goodbye implementation is selected in the deploy repo's `.env`:

```bash
# Safe default and immediate rollback target.
RECEPTION_VISITOR_TRIGGER_PROFILE=legacy

# Captured-evaluation candidate for controlled live acceptance.
RECEPTION_VISITOR_TRIGGER_PROFILE=visitor-v1-20260802

# Door-ordered greet/goodbye candidate.
RECEPTION_VISITOR_TRIGGER_PROFILE=door-v1-20260805
RECEPTION_VISION_PIPELINES_CONFIG=/Users/leon/projects/reachy_mini_receptionist_release_6b4c5a6/config/vision/door-policy-v1.json
RECEPTION_RERUN_MODE=off
```

For the current controlled acceptance, export the three door-policy values in the release shell
instead of changing the shared rollback `.env`. Keeping Rerun streaming off isolates the conversation
and policy test while raw video and detector observations are still recorded.

Only one value should be active. A changed value applies to the next OPS invocation and live
process, so stop the current session normally and start a new one. Unknown profile names fail at
startup. The run manifest's `config.visitor_trigger_profile` object records the selected name and
complete resolved configuration.

The door policy pipeline loads Grounding DINO when the runner starts. On the prepared m1max release,
the first isolated model load took about 20 seconds; this is startup time before robot interaction,
not per-frame inference latency.

There is not yet a first-class unlimited duration. For the 2026-08-06 long run, OPS used a very
large `LIVE_DURATION` as a run-until-stopped workaround. This is acceptable only for assisted
operation and remains a production-readiness item.

To reproduce a height-based live profile offline against a retained video:

```bash
reception-vision-replay path/to/video.mkv \
  --visitor-trigger-profile visitor-v1-20260802
```

Profile rollback is operational: set `RECEPTION_VISITOR_TRIGGER_PROFILE=legacy`, unset
`RECEPTION_VISION_PIPELINES_CONFIG`, then start a new session. Release rollback uses the preserved
`reachy_mini_receptionist_deploy` checkout and its environment. Neither path requires reverting a
commit.

Add `--record-video` only when raw MKV video is needed. It increases artifact size.

Stop and clean up:

```bash
"$OFFICIAL_RUNTIME_PYTHON" -m reachy_mini_brain.official_runtime.ops_cli \
  --confirm-physical stop-session
```

Expected post-run state:

- Live runner stopped.
- Robot slept.
- Motors disabled.
- Backend left warm by default.

## Compatibility Wrapper

`scripts/m1max/live_ops.sh` still exists as a compatibility wrapper, but it is no longer the source
of truth. Prefer the Python OPS CLI above for new work.

The equivalent wrapper commands are:

```bash
scripts/m1max/live_ops.sh status
scripts/m1max/live_ops.sh preflight
LIVE_DURATION=900 scripts/m1max/live_ops.sh clean-run
scripts/m1max/live_ops.sh clean-stop
```

## Backend Lifecycle

Backend lifecycle is intentionally separate from robot lifecycle. Keep the S2S backend warm across
robot tests unless:

- model, voice, provider, or backend config changes;
- backend state appears wedged;
- a cold-start timing measurement is needed.

Create or update the managed backend runtime venv from this repo:

```bash
scripts/m1max/setup_s2s_backend.sh
```

The setup script creates/updates `/Users/leon/projects/speech_to_speech_backend/.venv` with Python
3.12+, installs the pinned `speech-to-speech==0.2.10`, verifies the backend CLI and Parakeet STT
import, and writes `runtime-info.json`. It can use uv for venv creation when `python3.12` is not on
the non-login shell `PATH`. It refuses to update the venv while the backend port is listening unless
`--skip-running-check` is passed. It does not delete backend logs, model caches, or run artifacts.

Useful commands:

```bash
PYTHONPATH=src .venv/bin/python -m reachy_mini_brain.official_runtime.ops_cli backend status
PYTHONPATH=src .venv/bin/python -m reachy_mini_brain.official_runtime.ops_cli backend start
PYTHONPATH=src .venv/bin/python -m reachy_mini_brain.official_runtime.ops_cli backend restart
PYTHONPATH=src .venv/bin/python -m reachy_mini_brain.official_runtime.ops_cli backend stop
```

Current backend contract:

- Backend package: Hugging Face `speech-to-speech==0.2.10`
- Runtime folder: `/Users/leon/projects/speech_to_speech_backend`
- WebSocket: `ws://127.0.0.1:8765/v1/realtime`
- Live handler: native `s2s-local`, not the official app's handler
- STT: `parakeet-tdt`
- LLM slot: `responses-api` through the local Hermes wrapper on `127.0.0.1:8642`
- Direct model path: OpenRouter `openai/gpt-5.6-luna`
- TTS: `qwen3`, voice `Sohee`

The accepted live app talks to this backend directly. It should not require
`/Users/leon/projects/reachy_mini_conversation_app`.

## Mark Live Feedback

While the test runs, drop time-stamped feedback markers from a second pane so subjective reactions
become queryable timestamps instead of memory. Press Enter to stamp "now"; type a few words first
for an inline note. Annotate the rest after Ctrl-D.

```bash
cd "$RELEASE"
"$OFFICIAL_RUNTIME_PYTHON" scripts/m1max/mark.py
```

This writes `artifacts/markers-<run_id>.jsonl`, aligned by wall-clock `ts` to events/audio/video
for later review.

## Readiness Milestones

Milestone logging is intentionally split. No single line means "the robot is ready for everything."
Watch for separate lines like:

```text
official-runtime milestone <run-id>: robot_control_ready
official-runtime milestone <run-id>: robot_sdk_connected
official-runtime milestone <run-id>: robot_audio_warmup_ok
official-runtime milestone <run-id>: robot_video_warmup_ok
official-runtime milestone <run-id>: gesture_detector_init_start gestures=['Open_Palm'] threshold=0.5
official-runtime milestone <run-id>: gesture_detector_ready gestures=['Open_Palm'] threshold=0.5 load_ms=...
official-runtime milestone <run-id>: backend_handler_started
official-runtime milestone <run-id>: software_pipeline_initialized
official-runtime milestone <run-id>: input_loop_starting
official-runtime milestone <run-id>: first_mic_frame_captured forwarded=False
official-runtime milestone <run-id>: audio_gate_opened audio_gate_open=True reason='wave'
official-runtime milestone <run-id>: first_mic_frame_forwarded_to_backend
official-runtime milestone <run-id>: first_backend_audio_pushed_to_robot
```

With audio gate enabled, `first_mic_frame_captured` does not mean the backend is receiving speech.
Backend forwarding begins only after the wave policy opens the audio gate.

With gestures enabled, `robot_video_warmup_ok` only proves camera frames are flowing. Wave readiness
is the separate `gesture_detector_ready` milestone. Gesture diagnostics are recorded in
`events-<run-id>-NN.jsonl` as `vision.gesture_candidate`, `vision.gesture_suppressed`, and
`vision.gesture_emitted`; reception ingress is recorded as `policy.wave_received`.

## Artifacts

Latest-run pointer:

```bash
PYTHONPATH=src .venv/bin/python -m reachy_mini_brain.official_runtime.ops_cli latest-run
```

Main artifact roots on m1max:

- Run manifests: `artifacts/official-runtime-live/runs/run-<run_id>.json`
- Runtime log: `artifacts/logs/<run_id>.log`
- Audio WAVs/sidecars: `artifacts/official-runtime-live/audio/`
- Event/realtime/policy/capture JSONL: under `artifacts/official-runtime-live/`
- Optional raw video MKV: `artifacts/official-runtime-live/video/`
- Markers: `artifacts/markers-<run_id>.jsonl`

## Live Rerun Viewer

The validated remote workflow uses the native Rerun app on the review Mac through an SSH tunnel.
It does not depend on the Rerun web viewer.

1. Before starting the robot run, host the Rerun server on m1max:

```bash
cd "$RELEASE"
"$RELEASE/.release-venv/bin/rerun" --serve-web --web-viewer-port 9092 --port 9880 --hide-welcome-screen
```

2. Configure the live runner to publish locally on m1max and retain an `.rrd`:

```text
RECEPTION_RERUN_GRPC_URL=rerun+http://127.0.0.1:9880/proxy
--rerun-mode grpc+file
--vision-pipelines-config config/vision/door-live-compare-v1.json
```

3. On the review Mac, keep this tunnel running:

```bash
ssh -N -L 9880:127.0.0.1:9880 leon@100.127.86.67
```

4. In another review-Mac shell, open the native viewer:

```bash
cd /Users/noel/projects/reachy_mini_receptionist_clean
.venv/bin/rerun --connect rerun+http://127.0.0.1:9880/proxy
```

Verify the camera framing and detector overlays in the native viewer before beginning the physical
test sequence. For a door test, the actual door must be visible; detections while the door is outside
the frame are false-detection diagnostics, not door-localization results. The SSH tunnel and native
viewer may remain open after `stop-session` so the finalized recording stays available for review.

## Manual Stop Rules

If Codex started the run, tell Codex `stop`; Codex should run `stop-session` and report artifact
pointers.

If stopping from a shell:

```bash
PYTHONPATH=src .venv/bin/python -m reachy_mini_brain.official_runtime.ops_cli \
  --confirm-physical stop-session
```

Use backend stop only when intentionally shutting down the warm backend:

```bash
PYTHONPATH=src .venv/bin/python -m reachy_mini_brain.official_runtime.ops_cli backend stop
```

## Legacy Reception Daemon

The old `reachy_mini_brain.reception` daemon + `alert_engine` flow is legacy fallback/reference
only. It should not be used for normal live tests unless explicitly comparing against legacy
behavior.

Historical details live in:

- `docs/archive/legacy/plan-reception.md`
- `docs/archive/legacy/plan.md`
- `docs/archive/legacy/progress.md`
