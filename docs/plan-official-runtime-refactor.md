# Official Runtime Refactor Plan

## Decision

Adopt the official `pollen-robotics/reachy_mini_conversation_app` core runtime design as the
foundation for our next architecture.

The target is not a sidecar process and not a blind rewrite. The target is:

```text
official-style core runtime:
  audio stream loop
  camera worker
  movement manager
  tool/capability registry
  realtime backend handlers

policy controllers:
  deterministic reception UX policies
  realtime LLM conversation policy
  optional camera/head-tracking policies

data harness:
  run manifests
  raw audio/video/capture artifacts
  event timestamps
  transcript/response timing
```

In this model, an "agent" is only one kind of policy controller. A deterministic policy can
invoke the same capabilities without asking an LLM to decide.

## Acceptance Direction, 2026-06-21

Decision after the 2026-06-21 live runs: accept the m1max local S2S backend plus the ported
official-runtime live app as the new basic-function path for reception UX.

What "accepted" means now:

- Use the new runtime/backend path for continuing greet, goodbye, wave-chat, recording, and policy UX
  work.
- Treat the legacy reception daemon as fallback/regression reference, not the product direction.
- Do not delete or break the legacy path until housecleaning explicitly identifies what is still needed
  for fallback, comparison, or historical replay.
- Keep `/Users/noel/projects/reachy_mini` as the product source of truth; m1max remains the deployment
  mirror, and the official conversation app checkout remains upstream/reference only.

Acceptance evidence:

- Local S2S backend was reused warm across live tests and produced smooth robot voice output.
- User feedback for `official-live-20260621-133812`: smoothest wave-chat session so far; greet/goodbye
  trigger behavior smooth.
- User feedback for `official-live-20260621-140328`: again very smooth voice for all policies; antenna
  movement worked fine overall, with a possible miss on the first goodbye.
- Robot cleanup after each run ended with media released, motors disabled, and move queue drained.

Known rough edges that do not block acceptance:

- Console milestones are transition markers, not a full turn monitor. Add per-turn summaries later.
- Live runner stop path still falls back to hard kill after TERM; cleanup completes, but stop should
  become graceful.
- Antenna behavior needs polish, especially the possible first-goodbye miss and speech-time movement.

Immediate next work:

1. **Repo housecleaning around the accepted path.**
   - Make the official-runtime live app + m1max local backend the documented default.
   - Keep legacy daemon code quarantined as fallback/reference until a deliberate cleanup pass decides
     what to remove.
   - Remove or archive stale prototype folders, duplicated m1max root files, outdated LiveKit template
     remnants, and docs that point users to the legacy path as the primary path.
   - Preserve recorded artifacts and docs needed for replay, regression, and root-cause history.

2. **Clinic context for backend LLM.**
   - Ensure the local S2S backend receives the real clinic receptionist profile, not only the client app
     wrapper.
   - Define exactly where clinic facts, receptionist tone, safety boundaries, and conversation behavior
     live.
   - Add a verification path that logs which instruction/profile was sent to the backend for each run.
   - Explore two implementation directions:
     - an agentic LLM API path, for example a local Hermes-agent-style API using remote LLM providers,
       where context, memory, tool policy, and conversation state can be owned by the agent harness
       rather than the thin realtime backend adapter;
     - a bare Responses API path, where the local S2S backend keeps direct `responses-api` calls and we
       sweep remote LLM models/instructions without adding an agent harness.

3. **Antenna UX during wave-chat speak.**
   - Keep startup cue separate from policy cues.
   - Keep greet/goodbye/wave-open antenna cues.
   - Add subtle antenna movement during assistant speech in wave-chat, with movement gating so it does
     not reintroduce choppy audio or distract from speech.
   - Investigate the possible first-goodbye missed pulse from the 2026-06-21 antenna retest.

### Conversation Latency Cue Policy

Decision, 2026-06-22: switch the text brain direction toward Hermes `conversation` state first, because
it lets Hermes own short-term conversation memory without us writing a custom transcript harness in the
realtime backend. The benchmark showed this likely costs about 2.5s additional text-response latency
versus direct OpenRouter Responses calls.

Do not hide that latency inside backend code. Add a modular, event-driven UX policy:

```text
backend / stream runtime
  emits semantic lifecycle events
    assistant.thinking.started
    assistant.audio.started
    assistant.audio.done

ConversationCuePolicy
  listens to those events
  starts/stops visible thinking cues

movement capabilities
  execute cancellable antenna-only cues
```

Constraints:

- The policy runs inside the live app process. It is lightweight event handling and must not become an
  STT/LLM/TTS worker or separate long-running compute process.
- It must not own conversation logic, memory, backend calls, or speech generation.
- It must not overlap cue movement with assistant voice output. Stop the cue on first assistant output
  audio frame and ensure antennas return to rest on response done, failure, cancellation, runtime stop,
  or policy stop.
- Keep movement implementation behind named capabilities, so later live testing can tune, replace, or
  disable movement without touching Hermes/backend flow.
- Start with antenna-only cues. Head movement during speech remains out of scope for the first pass.

4. **Ops management tools.**
   - Promote `scripts/m1max/live_ops.sh` into the normal live-test entrypoint.
   - Add explicit commands/status for backend lifecycle, live runner lifecycle, robot lifecycle, and
     artifact/run discovery.
   - Make stop graceful before hard kill where possible, while preserving guaranteed robot cleanup.
   - Add a preflight/status packet that captures backend PID, robot media/motor state, daemon status,
     current run id, artifact path, and whether the backend is warm or cold.

## Repo Ownership And Cleanup

Decision, 2026-06-16: make `/Users/noel/projects/reachy_mini` the single product repo and source of
truth for new work.

Repository roles:

- `reachy_mini`: owns the legacy daemon, the isolated official-style runtime sandbox, custom backend
  experiments, clinic profile, runbooks, logs, tests, and all future product code.
- `reachy_mini_conversation_app`: upstream/reference checkout only. Do not continue product
  development there. Use it to inspect official behavior or to pull selected code/design ideas into
  `reachy_mini`.
- `m1max:/Users/leon/projects/reachy_mini_receptionist_clean`: deployment/test mirror of this repo. Do not hand-edit
  there except for machine-local env or emergency live-test debugging; sync changes from local.

Cleanup decisions:

- The generated LiveKit template folder `reachy/` is not a product repo. Its useful agent shape is now
  represented by `src/reachy_mini_brain/official_runtime/livekit_agent.py`, and credentials live in the
  main repo `.env`, so `reachy/` should stay removed/ignored.
- The accepted clinic receptionist profile is checked into `profiles/clinic_receptionist/` and is the
  default instruction source for the LiveKit agent helper when no explicit instruction file is set.
- Keep the legacy daemon path for fallback and regression comparison, but do not add new architecture
  features there unless needed to unblock live tests.
- Keep `src/reachy_mini_brain/official_runtime/` isolated until it passes live acceptance. After that,
  promote it to the main runtime path and retire old sandwich-code defaults.

Open consolidation items before deleting the separate official checkout:

- Port or supersede any useful prototype reception-controller ideas from
  `/Users/noel/projects/reachy_mini_conversation_app/src/reachy_mini_conversation_app/reception/`.
  Status, 2026-06-16: core pieces are now ported into the isolated runtime:
  - `official_runtime.artifacts.ArtifactRecorder`: run manifest, events/policies/realtime JSONL,
    session snapshots, response metadata, input/output WAV streams, and per-response output WAVs.
  - `OfficialStyleStreamRuntime` runtime observer hooks: input audio tap, output audio tap, audio gate,
    output message tap, metadata propagation from official-style audio tuples, and composite observers.
  - `official_runtime.reception.ReceptionPolicy`: deterministic approach/greet, wave conversation open,
    goodbye/idle/max-duration close, cooldowns, and audio-gate behavior.
  - `official_runtime.perception.PerceptionPipeline`: person approach/departure and wave pipeline,
    with lazy heavy dependencies and injection points for offline tests.
  - `official_runtime.replay_vision`: video perception replay CLI scaffold.
  - `official_runtime.moves`: antenna pulse move, antenna-pulse capability helper, and playback movement
    gate for suppressing nonessential movement during assistant audio.
  - `official_runtime.camera`: official-style camera Q&A and head-tracking capability adapters.
    Camera Q&A mirrors the official tool boundary: latest BGR frame -> local vision processor if
    available -> otherwise base64 JPEG for the realtime backend. Head tracking mirrors the official tool
    boundary: call `camera_worker.set_head_tracking_enabled(start)`.
- Remaining artifact gaps after this port: backend-input audio tap after any live robot resample/gate
  layer, full tool result tracing, stable video frame id alignment, and provider-internal visibility where
  the backend exposes it.
- Camera Q&A and the head-tracking toggle are now represented in the isolated runtime. The lower-level
  live camera worker / face-offset loop still needs live-runtime wiring: camera frames must be fed from
  the robot media stream, optional head tracker output must update movement-manager secondary offsets,
  and playback movement gating must suppress that blend during assistant speech.
- Remaining dirty official-checkout ideas to either port or intentionally drop before deleting it:
  - Provider-specific OpenAI/HF/Gemini realtime event-envelope hooks and `request_text_response()`
    implementations.
  - Official-app CLI flags and startup plumbing for `--reception`, `reception-check`, and
    `reception-replay`; the isolated repo now owns equivalent core modules, but not a full live startup
    command for the refactor path.
  - Official movement-manager live loop details: idle-breathing toggle, neutral antenna config, and
    face-tracking offset composition. The isolated runtime has capability/gate primitives, not the full
    live movement loop.
- Status, 2026-06-17: added a first live-test runner in the ported path:
  - `official_runtime.robot_io`: robot mic source, speaker sink, camera frame
    provider, SDK session/warmup wrapper.
  - `official_runtime.live_app`: `official-runtime-live` CLI that composes robot IO, backend handler,
    artifact recorder, reception policy, movement gate, optional perception, and clean SDK teardown.
  - `official_runtime.hf_official`: temporary adapter around the official app HF realtime handler for
    today's m1max local S2S / HF remote live tests. This preserves the ported runtime composition while
    deferring a full provider-specific handler port.
  - First recommended live smoke: `--backend hf-official --hf-connection-mode local --no-perception
    --no-audio-gate --duration <short>` to validate robot IO + backend audio before wave/greet policies.
- Once those are done, either delete the separate checkout or reclone it clean as a read-only upstream
  reference with no local product edits.

## Immediate Refactor Goal: Preserve Official Audio Semantics

Decision, 2026-06-17: the ported `official_runtime` must match the official app's headless live
audio output semantics unless a divergence is explicitly documented and tested.

Official behavior:

```text
handler.emit() audio tuple
-> convert/resample for the robot output sample rate
-> robot.media.push_audio_sample(audio_frame)
-> yield to the event loop
```

The official app does not split live backend output into Python-side 20 ms chunks and does not sleep
to pace those chunks. The installed Reachy SDK/GStreamer path assigns buffer PTS/duration from sample
count and owns live playback timing.

Drift found during live testing:

- The port copied the official handler interface shape but introduced Python-side chunk splitting,
  queueing, and sleep-based pacing in `ReachyAudioSink`.
- In `official-live-20260617-143749`, the backend emitted `4.832s` of response audio, while our output
  handling stretched it across `18.221s`. This matched the live report that the robot audio became
  more choppy.
- The tests validated the invented 20 ms pacing behavior instead of validating official-compatible
  playback semantics. That is why this architecture drift survived local tests.

Acceptance before further broad live UX tests:

- `official_runtime` live conversation playback has no Python-side sleep, pacing queue, or forced
  20 ms splitting.
- One official handler audio tuple becomes one converted/resampled `push_audio_sample()` call, unless
  the upstream official app does otherwise.
- SDK/GStreamer buffer timing remains the timing authority for live backend output.
- Tests fail if Python-side output pacing is reintroduced into the official-runtime conversation path.
- Artifact logging records output timing without changing playback semantics.
- Any direct-WAV playback experiment or legacy pacing helper remains separate from live conversation
  playback and is named as such.

Implementation status, 2026-06-17:

- `ReachyAudioSink` now follows the official play-loop contract: convert to mono float32, resample to
  the robot output sample rate when needed, push one frame per handler audio tuple, then yield.
- The live conversation sink no longer owns a worker thread, pacing queue, forced 20 ms chunks, or
  sleep-based output timing.
- Official-runtime tests now guard this behavior by asserting one backend tuple produces one robot
  `push_audio_sample()` call and by covering output-rate resampling.

## Why

Our current reception daemon uses a serial speech sandwich:

```text
VAD -> local STT -> text brain -> TTS -> playback
```

Live data shows two primary UX failures:

- **STT latency** — faster-whisper `medium` often takes ~3-4s per utterance.
- **Voice-loop blocking** — `brain.respond()` and `speak()` block transcript draining, so text that
  is already STT-complete can sit stale in the queue.

The official conversation app uses realtime audio:

```text
mic frames -> realtime backend -> streamed audio deltas -> speaker
```

It also already has the right shape for camera, head tracking, tools, movement, and interruption.

## Core Concepts

### Core Runtime

The core runtime owns streams and low-level robot capability plumbing:

- Continuous mic input.
- Continuous speaker output.
- Camera frame buffering.
- Movement composition.
- Tool/capability registration.
- Realtime backend connection and event handling.

Official reference modules:

- `/Users/noel/projects/reachy_mini_conversation_app/src/reachy_mini_conversation_app/console.py`
  - `LocalStream.record_loop()`
  - `LocalStream.play_loop()`
  - `clear_audio_queue()`
- `/Users/noel/projects/reachy_mini_conversation_app/src/reachy_mini_conversation_app/conversation_handler.py`
  - `ConversationHandler.receive()`
  - `ConversationHandler.emit()`
- `/Users/noel/projects/reachy_mini_conversation_app/src/reachy_mini_conversation_app/base_realtime.py`
  - response queue
  - realtime event handling
  - interruption hooks
- `/Users/noel/projects/reachy_mini_conversation_app/src/reachy_mini_conversation_app/camera_worker.py`
- `/Users/noel/projects/reachy_mini_conversation_app/src/reachy_mini_conversation_app/moves.py`

### Policy Controllers

A policy controller decides when to invoke a capability.

Examples:

```text
LLM conversation policy:
  user asks "what do you see?"
  -> model calls camera(question)

Reception wave policy:
  camera stream detects wave while idle
  -> open conversation
  -> start realtime voice interaction

Head tracking policy:
  head tracker detects face position
  -> movement manager blends look-at offsets
```

This lets us keep deterministic reception behavior without forcing the LLM to decide every UX
transition.

### Capabilities

Capabilities are callable robot/app actions. They can be invoked by any policy controller:

- realtime voice response
- camera Q&A
- head tracking toggle
- greet
- goodbye
- look/move/dance/emotion
- clinic FAQ lookup
- memory/context tools
- recording/artifact hooks

## Keep Official Behaviors

### Camera Q&A

Keep the official `camera` tool behavior:

```text
tool call camera(question)
-> get latest camera frame
-> if local vision enabled: SmolVLM2 returns text
-> else: send image to selected realtime backend
-> assistant answers
```

Add our own trigger/policy layer on top when needed. For example, a deterministic policy can call
camera analysis on approach/wave events, or the realtime LLM can call it during conversation.

### Head Tracking

Keep official head tracking as a policy/capability pair:

```text
camera frames
-> YOLO or MediaPipe head tracker
-> face/head center
-> movement manager blends head offsets
```

This is conceptually the same pattern as our reception UX:

```text
toggle feature on
-> consume stream
-> maintain state
-> trigger motion/action
```

### Realtime Audio

Replace the local STT/TTS voice loop as the default conversation path:

```text
mic frames
-> official realtime handler receive()
-> backend VAD/transcription/reasoning/TTS
-> output audio delta queue
-> speaker output loop
```

Keep the current local STT path as a fallback/debug backend until realtime is validated.

## Reception Policies

Implement our clinic UX as separate deterministic policies:

### Approach/Greet Policy

```text
vision enabled
person approaches while idle
-> greet once
-> enter greeted/cooldown state
```

### Wave Conversation Policy

```text
wave detected while idle/greeted
-> open conversation
-> enable realtime voice loop
-> optionally speak short opener / nonverbal ack
```

### Conversation Close Policy

```text
conversation active
idle timeout or explicit goodbye/depart
-> stop realtime conversation
-> return to idle/cooldown
```

### Departure/Goodbye Policy

```text
person departs after active interaction
-> goodbye if not conversation-active
```

These policies should be explicit state machines, not model-only behavior.

## Data Harness

Preserve the data harness as an observer of core runtime events.

Required event/timing categories:

- audio input frames / raw recording
- camera frames / video capture
- policy events: approach, wave, greet, conversation open/close, depart
- realtime backend events: speech_started, speech_stopped, transcript_delta,
  transcript_completed, response_created, first_audio_delta, response_done
- backend observability, where the provider exposes it: backend-side VAD decisions, transcript
  finalization timing, model/voice selection, queue/backpressure signals, response cancellation, and
  backend error payloads
- tool calls and results
- robot output events: motion, audio chunks, playback cleared/interrupted

Artifacts should remain run-scoped:

```text
artifacts/runs/run-<run_id>.json
artifacts/audio/audio-<run_id>-NN.wav
artifacts/video/video-<run_id>-NN.mkv
artifacts/capture/capture-<run_id>-NN.jsonl
artifacts/realtime/realtime-<run_id>.jsonl
artifacts/policies/policies-<run_id>.jsonl
```

## Refactor Phases

### Phase 0 — Read And Map

- Map official modules to our current modules.
- Identify minimum imports/adapters needed to instantiate official handlers.
- Decide whether to vendor code, depend on the package, or copy selected modules with attribution.

### Phase 1 — Core Runtime Skeleton

- Create a new runtime abstraction in our repo.
- Add a single owner for robot media streams.
- Add event bus / observer hooks for artifacts.
- Keep existing daemon commands where useful, but route voice through the new runtime.

### Phase 2 — Realtime Voice Backend

- Integrate official-style `ConversationHandler` contract.
- Start with one provider, likely OpenAI or Hugging Face depending cost/config.
- Record realtime events and timing.
- Preserve local STT backend as fallback.

### Phase 3 — Vision Capabilities

- Port/adapt `CameraWorker`.
- Add camera Q&A capability.
- Add optional local vision path.
- Add head tracking using official YOLO/MediaPipe policy shape.

### Phase 4 — Reception Policies

- Rebuild approach/greet/wave/conversation-close as policy controllers.
- Keep deterministic state transitions.
- Ensure policies invoke capabilities, not low-level robot APIs directly.

### Phase 5 — Data Harness Parity

- Restore or improve all current artifact categories.
- Add realtime-specific event logs.
- Add replay/review utilities for the new event format.

### Phase 6 — Remove Old Sandwich Default

- After live validation, make realtime voice the default.
- Keep local STT/TTS as debug/fallback only.
- Retire stale-queue/batch code that exists solely for the old local STT path.

## After Refactor Acceptance Plan

This section starts only after the official-runtime refactor passes basic UX acceptance: approach/greet,
wave-triggered conversation, goodbye/close, camera/head-tracking behavior, and artifact recording all work
well enough for continued live testing.

### Immediate Live-Test Priorities

Use the m1max-hosted local S2S backend as the next realtime backend candidate for live tests.
This is no longer only a research option: the 2026-06-15 offline replay showed comparable
post-transcript latency to deployed HF on the approved `full-retest-sohee` clips, with better
server-side visibility.

Live-test configuration:

```env
BACKEND_PROVIDER=huggingface
HF_REALTIME_CONNECTION_MODE=local
HF_REALTIME_WS_URL=ws://100.127.86.67:8765/v1/realtime
REACHY_MINI_CUSTOM_PROFILE=clinic_receptionist
```

The m1max backend stack to test first:

```text
STT: parakeet-tdt
LLM: responses-api via OpenRouter
TTS: qwen3
Voice: Sohee
```

Acceptance checks for this backend live test:

- It can run the same greet/goodbye and wave-conversation flows through the official-runtime handler.
- It preserves or improves response coverage and post-transcript first-audio latency versus deployed HF.
- It records enough backend/server logs to explain VAD, STT finalization, LLM response timing, TTS
  first-audio timing, cancellations, and interruptions.
- It does not regress camera/head-tracking policy behavior or artifact recording.
- Any audio-quality judgement comes from human review of WAVs or live UX feedback, not transcript-only
  inference.

Add a response-playback movement gate before the next full conversation live test. The backend swap
does not by itself prevent unwanted head/antenna motion while audio is playing; that is a runtime/client
responsibility.

Implementation target:

- Add a runtime config flag, defaulting on for reception live tests, to suppress movement during
  assistant audio playback.
- On first assistant audio delta / playback start, mark `playback_active=true` and pause conversation
  wobble, head tracking blend output, idle motion, and policy-triggered motion that is not explicitly
  allowed.
- On response audio done, response cancelled, interruption, or connection close, clear the suppression
  flag so normal policies resume.
- Keep pre-speech UX motion available: listening/thinking/greeting setup motion can happen before audio
  starts, but continuous motion should not overlap spoken playback unless explicitly enabled for an A/B
  test.
- Log suppression start/end events with response id and reason so playback/movement diagnosis can be
  replayed from artifacts.

### Robot-Side Runtime Access And Refresh

High priority before deeper audio/movement diagnosis: set up direct SSH access to the Reachy Mini robot
itself, not only to m1max. Recent live tests suggest some failures may be robot-runtime state rather
than app/backend state:

- Sohee output WAVs generated by the backend sounded clean locally, while robot playback sometimes had
  volume/dropout symptoms.
- After a robot restart, the same full official-runtime path sounded clean again.
- Bad/rough runs logged robot control instability such as
  `Failed to set robot target: Lost connection with the server.`

Goals:

- Establish the robot SSH target, credentials, and safe access path from the dev machine and m1max.
- Discover robot-side service names with `systemctl list-units` rather than guessing.
- Identify the daemon/media/signaling services that own SDK control, WebRTC audio/video, speaker output,
  mic input, camera streaming, and wobbling.
- Define safe refresh commands:
  - restart only robot media/signaling service if available
  - restart only Reachy Mini daemon if needed
  - full reboot as last resort
- Collect robot-side logs for live runs:
  - daemon logs around app start/stop
  - WebRTC/media pipeline logs
  - audio device / ReSpeaker / XMOS detection logs
  - motion/control server errors
- Add a live-test preflight: record robot service versions, uptime, audio device status, and whether the
  previous app run exited cleanly.

Runtime access layers to use:

1. **Daemon REST API** (`http://<robot>:8000/api`) for low-risk inspect/control.
   - Inspect: `/api/daemon/status`, `/api/media/status`, `/api/motors/status`, `/api/state/full`,
     `/api/move/running`, `/api/volume/current`.
   - Control: `/api/daemon/start`, `/api/daemon/stop`, `/api/daemon/restart`,
     `/api/media/release`, `/api/media/acquire`, `/api/media/wobbling/enable`,
     `/api/media/wobbling/disable`, motor mode endpoints.
   - Audio-board config: `/api/audio/config/parameter/<name>` and `/api/audio/config/apply`.
   - Prefer `GET /api/volume/current` for inspection. Avoid casual `POST /api/volume/set` during
     tests because the official app comments note it can trigger a daemon test sound; use SDK typed
     volume command where possible.
2. **SDK/WebSocket path** (`ws://<robot>:8000/ws/sdk`) for app-level controls.
   - Confirmed SDK methods in the m1max venv include `wake_up`, `goto_sleep`, `enable_motors`,
     `disable_motors`, `goto_target`, `set_target`, `enable_wobbling`, `disable_wobbling`,
     `release_media`, and `acquire_media`.
   - The installed SDK protocol also includes daemon log subscription, audio-parameter commands,
     and a daemon restart command that tears down media/control transport and expects reconnect.
3. **Robot SSH / systemd** for cases REST/SDK cannot explain.
   - Service name discovered from installed SDK scripts: `reachy-mini-daemon.service`.
   - Initial inspect commands once SSH is available:

```bash
hostname
uptime
systemctl status reachy-mini-daemon --no-pager
journalctl -u reachy-mini-daemon -n 300 --no-pager
ss -ltnp | grep -E ':8000|:8443'
aplay -l
arecord -l
rpicam-hello --list
gst-inspect-1.0 libcamerasrc
```

First incident packet after a bad run:

- m1max app process log and run manifest.
- REST snapshots: daemon status, media status, motor status, state/full, move/running, volume/current.
- Robot `journalctl -u reachy-mini-daemon` covering 2 minutes before app start through shutdown.
- Audio/video device state (`aplay`, `arecord`, camera list) if playback/capture symptoms occurred.
- Whether SDK/REST daemon restart was enough to recover, or full robot reboot was required.

Acceptance for this infrastructure item: after a bad audio or motion run, we can answer whether the
fault was visible in robot-side logs and can refresh the robot runtime without a full power/reboot cycle.

### Backend Model-Stack Customization

The first accepted refactor should use profile/instruction customization for clinic behavior. In the
official-app refactor this is `profiles/clinic_receptionist/instructions.txt`, selected with
`REACHY_MINI_CUSTOM_PROFILE=clinic_receptionist`.

Changing the actual realtime STT/LLM/TTS stack is a separate post-acceptance track. The deployed Hugging
Face backend chooses its model stack server-side; app-side `MODEL_NAME` is empty for HF and cannot directly
swap the current reported stack:

```text
STT: parakeet-tdt
LLM: responses-api
TTS: qwen3
```

Current custom-backend candidate for live testing is the m1max-hosted local S2S backend, which keeps
the official app's Hugging Face realtime handler boundary but points it at our own websocket service:

- Set `HF_REALTIME_CONNECTION_MODE=local`.
- Set `HF_REALTIME_WS_URL` to the m1max local S2S websocket.
- Compare against deployed HF on STT accuracy, turn latency, TTS smoothness, interruption behavior,
  camera/tool compatibility, and artifact completeness.
- Promote this path only if it materially improves or preserves the accepted basic UX without making
  startup/ops too fragile.

Clinic-context implementation should be evaluated in two directions:

1. **Agentic LLM API harness.**
   - Candidate shape: a local Hermes-agent-style API running on m1max, backed by remote LLM providers.
   - Purpose: offload clinic context, memory, long-running conversation state, tool policy, and safety
     boundaries to an agent harness instead of hand-managing them in the realtime backend adapter.
   - Compare against the current local S2S backend on latency, response relevance, context retention,
     debuggability, failure recovery, and ease of logging what context/memory was used per turn.
   - Keep STT/TTS local unless the agent experiment specifically requires changing the speech stack.

2. **Bare Responses API model sweep.**
   - Keep the current local S2S backend's direct `responses-api` LLM call path.
   - Test different remote LLM models and prompt/profile variants for clinic receptionist behavior.
   - Compare models on first-token/first-audio latency, instruction following, concise reception tone,
     stale-context behavior, cost, and robustness to imperfect STT.
   - Use this as the lower-complexity baseline before adopting an agentic harness.

### Custom Conversation Backend Research

Future research track: evaluate whether we should build our own conversation backend on top of
`https://github.com/livekit/agents`.

Do this after the official-runtime refactor has passed basic UX acceptance. The goal is not to replace
the current HF deployed backend prematurely; it is to understand whether a LiveKit Agents backend would
give us better control over streaming STT, LLM policy/context, TTS voice consistency, interruption,
backend observability, and provider/model swapping.

Research questions:

- Can it expose an OpenAI-compatible or otherwise small adapter surface that fits the official app's
  realtime handler?
- Can we choose and log the exact STT, LLM, and TTS models per session/response?
- Can it provide better event visibility for speech boundaries, transcript finalization, stale/cancelled
  responses, first-audio latency, and voice selection?
- Can it run reliably on our target deployment path without adding too much ops complexity?
- How does end-to-end latency and audio smoothness compare with the deployed HF realtime backend?

Current offline benchmark note, 2026-06-15: the first LiveKit prototype underperformed the deployed
official HF backend on the six approved `full-retest-sohee` replay clips. HF produced spoken output on
5/6 clips; LiveKit produced spoken output on 2/6 clips and missed or partially transcribed several
inputs. Keep this as a model/provider/config result, not a final architecture rejection. Details and
artifact paths live in `docs/archive/research/plan-livekit-backend.md`.

### Voice Consistency And Accent Instrumentation

Live test `goodbye-20260612-134651` passed the basic greet/goodbye UX, but the spoken voice appeared
to change accent across otherwise fixed deterministic lines. Current artifacts are enough to review
the audio by ear:

```text
artifacts/audio/audio-output-<run_id>-01.wav
artifacts/realtime/realtime-<run_id>-01.jsonl
artifacts/policies/policies-<run_id>-01.jsonl
```

They are not enough to prove whether the backend used the requested voice consistently. The run used
the HF deployed backend and the app resolved the session voice as the HF default (`Aiden`), but the
manifest/realtime logs do not record per-response voice metadata or response-specific audio clips.

Post-acceptance data-harness upgrade:

- Record backend provider, connection mode, requested voice, resolved voice, session id, and model/backend
  stack in the run manifest.
- Record response id, requested/resolved voice, first-audio timestamp, transcript, and response completion
  in realtime JSONL.
- Split output audio into one WAV per assistant response in addition to the combined output WAV.
- Add a review utility that lists response clips with transcript/timing/voice metadata so accent drift can
  be reviewed without manually slicing the combined WAV.
- Add backend visibility to the review path where possible: show when the backend believed speech
  started/stopped, when it finalized each transcript, when it started/finished reasoning, when it started
  audio generation, which model/voice/session metadata it reported, and any backend error/cancellation
  payloads. This is needed to distinguish language-state issues from stale-response or backend-lag issues.
- If HF deployed voice remains inconsistent, include voice stability in the custom-backend/provider A/B
  matrix alongside latency, STT accuracy, TTS smoothness, and interruption behavior.

Full-replay instrumentation priority, ignoring physical robot response:

| Rank | Gap | Ease | Diagnosis lift | Why |
| ---: | --- | --- | --- | --- |
| 1 | Run/session/config snapshot | Easy | High | Record backend, connection mode, session id, requested/resolved voice, profile/instructions hash, tool list, and model config so every run explains which stack was actually used. |
| 2 | Full realtime event envelopes + IDs | Easy-Medium | Very High | Preserve response ids, item ids, event payload summaries, timestamps, error payloads, cancellation/rejection, and usage metadata so timeline review has stable joins. |
| 3 | Per-response output audio clips | Medium | Very High | Split assistant output audio by response and link clips to transcript, timing, voice, interruption status, and response completion. This is key for accent, lag, stale-response, and barge-in review. |
| 4 | Backend-input audio tap after processing | Medium | High | Record the exact audio chunks sent to the backend after gate/resample/int16 conversion, plus append success/failure timing, to prove whether the backend received clean audio. |
| 5 | Tool path tracing | Easy-Medium | Medium-High | Log tool call args, start/end, result sent back to backend, errors, and follow-up response so tool failures do not appear as unexplained chat failures. |
| 6 | Video/capture frame alignment | Medium | Medium-High | Add stable frame ids and frame timestamp mapping so video, detector output, and wave/approach/depart policy events line up exactly. |
| 7 | Backend-internal visibility | Hard/provider-dependent | Very High | Highest potential lift, but deployed providers may not expose internals; this is a reason to evaluate custom backend options such as LiveKit Agents after acceptance. |

Recommended implementation order: start with ranks 1-3, then add backend-input audio taps and tool
tracing, then tighten video frame alignment. Treat provider-internal observability as a backend/provider
selection topic rather than an app-only task.

## Open Questions

- Which realtime provider should be the first implementation target?
- Should the official app be a dependency, a vendored subtree, or source for selective ports?
- How much of official `MovementManager` should replace our current gesture code?
- How should clinic facts be exposed: system prompt, tool, RAG, or static realtime instructions?
- What exact event schema should the realtime data harness use?
- How should barge-in interact with deterministic policies and conversation close?

## Near-Term Next Step

Do a small code spike:

1. Instantiate an official-style realtime handler inside our process with a fake audio source/sink.
2. Verify `receive()` / `emit()` can run without the full official app UI.
3. Add an event observer that logs transcript and audio-delta timings.
4. Only then wire it to robot audio.
