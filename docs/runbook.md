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
- Active production release: the single frozen path in
  `/Users/leon/.config/reachy-reception/active-release`; inspect it through `reception-prod status`
  rather than selecting a checkout manually.
- Current live-validated assisted release:
  `/Users/leon/projects/reachy_mini_receptionist_release_4c28a3e_frozen` at `4c28a3e`
- First release-level rollback:
  `/Users/leon/projects/reachy_mini_receptionist_release_87d35ba_frozen` at `87d35ba`
- Older frozen fallback:
  `/Users/leon/projects/reachy_mini_receptionist_release_b7520a0_frozen` at `b7520a0`

Keep the dirty rollback checkout intact. A prepared release has its own `.release-venv`, while its
ignored `.env`, `private`, and `artifacts` paths reference the existing deployment-owned data. Set
the production release with `install_production_runtime.sh`; normal operation must use the stable
`~/.local/bin/reception-prod` launcher. It validates that the selected directory is frozen, its name
matches Git HEAD, its tracked worktree is clean, and its `.release-venv` exists. It has no fallback
to the mutable deployment checkout.

Do not use `/Users/leon/projects/reachy_mini_receptionist_release_749ee18`: its initial environment
was resolved without enforcing `uv.lock` and is retained only until deletion is separately approved.

### Prepare A Frozen Release

Use an exact Git revision, Python version, and lockfile. Do not build a release with direct
`uv pip install -e`; that command resolves current compatible versions instead of enforcing
`uv.lock`.

```bash
RELEASE=/Users/leon/projects/reachy_mini_receptionist_release_<revision>_frozen
/Users/leon/.local/bin/uv lock --check --project "$RELEASE"
/Users/leon/.local/bin/uv venv --python 3.12.13 "$RELEASE/.release-venv"

env VIRTUAL_ENV="$RELEASE/.release-venv" /Users/leon/.local/bin/uv sync \
  --project "$RELEASE" --active --frozen --no-editable --no-dev \
  --extra vision --extra gesture \
  --extra door-vision --extra diagnosis
```

Use a separate `.validation-venv` with the same command plus `--extra dev` for pytest and lint.
Record the Git SHA, `uv.lock` hash, Python/uv versions, extras, installed package inventory, and
non-secret configuration-link provenance before accepting the release.

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

# Safe aggregate status: release config, backend, Hermes, provider, runner, storage, and retention.
~/.local/bin/reception-prod status

# Optional fuller read-only status, including robot daemon state.
~/.local/bin/reception-prod status --include-robot
```

Run preflight before a normal live session when time allows:

```bash
~/.local/bin/reception-prod --confirm-physical preflight
```

Start the live session:

```bash
~/.local/bin/reception-prod --confirm-physical start-session
```

The private `~/.config/reachy-reception/production.env` is copied from
`config/production.env.example` on first installation and preserved on later release activation.
The production default records audio and derived vision JSONL but does not record raw MKV video.
Use `--record-video` only for a deliberately diagnosed run.

Managed OPS sessions default to `door-v4-20260827` when
`RECEPTION_VISITOR_TRIGGER_PROFILE` is unset. Set the variable in the deploy repo's `.env` only to
select an explicit profile or rollback target:

```bash
# Managed-runtime default; this assignment is optional.
RECEPTION_VISITOR_TRIGGER_PROFILE=door-v4-20260827

# Immediate behavior rollback target.
RECEPTION_VISITOR_TRIGGER_PROFILE=legacy

# Captured-evaluation candidate for controlled live acceptance.
RECEPTION_VISITOR_TRIGGER_PROFILE=visitor-v1-20260802

# Door-ordered greet/goodbye candidate.
RECEPTION_VISITOR_TRIGGER_PROFILE=door-v1-20260805
RECEPTION_VISION_PIPELINES_CONFIG=/Users/leon/projects/reachy_mini_receptionist_release_3449f8e_frozen/config/vision/door-policy-v1.json
RECEPTION_RERUN_MODE=off

```

The v4 default uses presence-overlap greet, direct distance-crossing goodbye, and prevents vision
policies from interrupting an active wave-chat conversation. Keeping Rerun streaming and raw video
off isolates the production conversation path while derived vision and detector observations remain
available.

Only one value should be active. A changed value applies to the next OPS invocation and live
process, so stop the current session normally and start a new one. Unknown profile names fail at
startup. The run manifest's `config.visitor_trigger_profile` object records the selected name and
complete resolved configuration. Camera-free policy-speech preflight explicitly uses `legacy`; it
tests fixed-text TTS playback and does not evaluate a visitor vision profile.

The door policy pipeline loads Grounding DINO when the runner starts. On the prepared m1max release,
the first isolated model load took about 20 seconds; this is startup time before robot interaction,
not per-frame inference latency.

The frame-broker architecture is the accepted production path. The serial architecture remains a
one-setting rollback that takes effect on the next run:

```bash
# Current production behavior.
RECEPTION_VISION_RUNTIME=broker-v1
RECEPTION_BROKER_CAPTURE_FPS=15
RECEPTION_BROKER_RECORDER_QUEUE_SIZE=30
RECEPTION_BROKER_GESTURE_QUEUE_SIZE=30
RECEPTION_BROKER_POLICY_IDLE_S=0.1
RECEPTION_GESTURE_RUNNING_MODE=image
RECEPTION_WAVE_DETECTION_MODE=open_palm

# Rollback. Apply only after stopping the current run.
RECEPTION_VISION_RUNTIME=serial-v1
```

`broker-v1` records canonical source frame IDs in the video and derived-capture sidecars and writes
final capture/consumer counters to `runtime_summaries.vision_broker` in the run manifest. Stop the
current run before changing modes; there is no mid-session fallback. See
`vision-frame-broker-architecture.md` for queue semantics, acceptance, and rollback boundaries.

### GStreamer startup acceptance

A fresh uv-managed release can fail during `robot_sdk_connect_start` even when `uv lock --check`,
`uv sync --frozen`, and `uv pip check` all pass. The characteristic log output is:

```text
External plugin loader failed
Caught a segmentation fault while loading plugin file: .../libgstpython.dylib
```

This is a runtime environment/bootstrap defect, not ordinary dependency drift. The GStreamer
package's `.pth` code prepends `GST_REGISTRY_1_0` every time Python starts, while OPS launches a
Python supervisor that launches the Python live runtime. The resulting value is a different,
colon-containing registry filename at each process depth. On a fresh release, scanning also finds a
`libgstpython.dylib` whose `@rpath/libpython3.12.dylib` dependency is not available in the uv
standalone-Python layout.

The 2026-08-27 launcher fix removes wheel-generated GStreamer paths before both the supervisor and
media-child process boundaries. A new candidate release must therefore start from an empty registry
without manual prewarming. Successful acceptance requires one ordinary OPS start to reach `ready`
with advancing audio and video, followed by a second ordinary start from the same release.

Do not prewarm a new candidate before this acceptance because doing so would hide a regression. If
an older preserved release without the launcher fix must be used for rollback and shows this exact
signature, first confirm that OPS reported `child_failed` and completed robot cleanup. Its historical
release-specific workaround was:

```bash
P="$RELEASE/.release-venv/.cache/gstreamer-1.0/registry-macosx-11.0-arm64.bin"

GST_REGISTRY_1_0="$P:$P" \
  "$RELEASE/.release-venv/bin/python" -c \
  'import gi; gi.require_version("Gst", "1.0"); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())'
```

The initial environment in those older releases contains two copies because the warmup interpreter
prepends the third. A scanner warning about `libgstpython.dylib` is expected there; successful
completion and a printed GStreamer version show that the registry was written. This workaround
applies only to the older immutable release path and is not an unattended-production procedure. The
root-cause record and current acceptance criteria are in
[Production Readiness](production-readiness.md#gstreamer-uv-startup-incident-2026-08-25).

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

Add `--record-video` only when raw MKV video is needed. It increases artifact size and places raw
clinic imagery inside the 30-day review window.

Stop and clean up:

```bash
~/.local/bin/reception-prod --confirm-physical stop-session
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

Production Hermes and S2S are launchd-managed non-physical services. Their definitions use
`KeepAlive`; the physical runner is never installed as a service and is never automatically
restarted. `reception-prod status` requires both service definitions plus live Hermes/S2S checks.
It also reads the installed S2S plist and reports degraded unless `ProcessType` is exactly
`Interactive`. `reception-prod backend restart` deliberately unloads and reloads the managed S2S
service; it waits for launchd unload, backend-process exit, and port closure before starting again.
If that bounded transition fails, restart stops without attempting a competing start.

S2S uses launchd `ProcessType=Interactive` because Qwen/MLX generation is latency-sensitive work
requested directly by a visitor. Do not change it to `Background`: a controlled 30-response test on
2026-09-01 reduced Qwen throughput from about `2.5x` realtime to `0.7-0.8x`, causing the audio stream
to fall behind playback. Hermes and maintenance jobs remain `Background`; only S2S receives the
interactive resource policy. The service remains launchd-managed and consumes no additional model
compute while idle.

Release activation uses the same lifecycle boundary when a loaded LaunchAgent definition changes:
wait for `bootout` to disappear from launchd before installing/bootstraping the replacement. A newly
bootstrapped `RunAtLoad` service is not immediately kickstarted again.

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
- launchd process type: `Interactive` (required for realtime Qwen/MLX throughput)

## Recording Retention

Production defaults are `RECORD_AUDIO=1`, `CAPTURE_VISION=1`, and `RECORD_VIDEO=0`. Raw video is
opt-in because it is the dominant storage consumer. Both raw audio and any opt-in raw video enter a
30-day review window.

```bash
~/.local/bin/reception-prod recording-retention
```

The command only reports files whose modification time is older than 30 days. A daily launchd job
writes `~/.local/state/reachy-reception/recording-retention-latest.json` and displays a macOS
notification when review is due. It never moves or deletes files. Any cleanup still requires an
explicitly reviewed file list and deletion confirmation. The development Mac uses the equivalent
`scripts/install_recording_retention_reminder.sh` job and a separate local report.

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

## Emergency Stop Procedure

The production emergency action is an operational fail-stop, not a certified hardware safety
stop. It terminates the supervised reception runner, escalates to a forced process stop after the
bounded grace period, releases robot media, requests the sleep pose, disables motors, and leaves
Hermes and S2S warm.

From either a remote SSH session or a terminal on m1max, run:

```bash
~/.local/bin/reception-prod --confirm-physical emergency-stop
```

The supervised path gives the child up to 30 seconds to finalize, while OPS gives the supervisor up
to 60 seconds to complete finalization and robot cleanup before forcing it down. The command can be
repeated safely when its result is uncertain.

Verify the retained state after the command returns:

```bash
~/.local/bin/reception-prod status --include-robot
```

Expected state:

- runner is stopped and no active runner PID remains;
- latest terminal status records `requested_stop`, or reports an explicit interruption/fault;
- robot media is released and motors are disabled; and
- Hermes and S2S remain loaded and healthy.

If the remote shell is unavailable, run the same command from a local m1max terminal. If OPS cannot
reach the robot daemon, do not start another reception session: preserve the returned status and
logs, use Reachy Mini Control locally to stop the active robot application, and follow the official
Reachy Mini power procedure only when software control is unavailable. A trained onsite operator
must handle any immediate physical hazard directly.

## Removed Legacy Reception Daemon

The old `reachy_mini_brain.reception` daemon and `alert_engine` flow were removed after the
official-runtime path was accepted. The final source snapshot is available at Git tag
`legacy-daemon-last`; it is not a supported fallback runtime.

Historical details live in:

- `docs/archive/legacy/plan-reception.md`
- `docs/archive/legacy/plan.md`
- `docs/archive/legacy/progress.md`
