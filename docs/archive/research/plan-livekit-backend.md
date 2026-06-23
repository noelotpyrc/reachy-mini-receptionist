# LiveKit Backend Implementation Plan

Date: 2026-06-15

## Decision

Build the LiveKit backend prototype inside the writable `reachy_mini` repo, using the isolated
official-style runtime sandbox:

```text
src/reachy_mini_brain/official_runtime/
```

Do not modify the legacy daemon path and do not depend on write access to
`/Users/noel/projects/reachy_mini_conversation_app`.

The key comparison boundary is the official app handler interface:

```text
AudioFrame -> handler.receive((sample_rate, frame))
handler.emit() -> AudioFrame | metadata
```

This lets us replay the same WAV through different backends without robot hardware.

## Architecture

```text
existing WAV
  -> WavAudioSource
  -> OfficialStyleStreamRuntime
  -> LiveKitRealtimeHandler
  -> LiveKitBridge
  -> LiveKit AgentSession
  -> handler.emit()
  -> WavAudioSink + event JSONL
```

For the first implementation, `LiveKitRealtimeHandler` should not directly embed LiveKit SDK details.
It should wrap a `LiveKitBridge` protocol:

```text
LiveKitRealtimeHandler
  start_up()  -> bridge.start()
  receive()   -> bridge.send_audio(frame)
  emit()      -> bridge.next_output()
  shutdown()  -> bridge.stop()
```

This gives us a clean seam:

- unit tests use a fake bridge
- offline replay uses the same handler contract
- real LiveKit room/agent code can be added behind the bridge later

## Build Phases

### Phase 1 — Offline Replay Harness

Implement:

- `WavAudioSource`
  - reads PCM WAV
  - chunks into realtime-sized frames, default 20 ms
  - returns `(sample_rate, np.int16 mono_frame)`
- `WavAudioSink`
  - writes handler output audio frames to WAV
  - validates sample-rate consistency
- `run_wav_replay()`
  - runs `OfficialStyleStreamRuntime`
  - feeds a handler through `receive()`
  - collects output WAV and runtime events

Acceptance:

- Existing tests pass.
- A generated WAV can be replayed through a fake/echo handler.
- Runtime emits input/output frame events with duration metadata.

### Phase 2 — LiveKit Handler Boundary

Implement:

- `LiveKitBackendConfig`
- `LiveKitBridge` protocol
- `LiveKitRealtimeHandler`

Acceptance:

- Handler starts/stops an injected bridge.
- `receive()` forwards frames to the bridge.
- `emit()` returns audio frames and metadata from the bridge.
- Handler emits useful events for replay:
  - `livekit.handler.started`
  - `livekit.audio.sent`
  - `livekit.output.audio`
  - `livekit.output.event`
  - `livekit.handler.stopped`

Status: implemented in the isolated sandbox with a fake bridge and tests.

### Phase 3 — Real LiveKit Room Bridge

Implement a real bridge that:

- connects to a LiveKit room
- publishes robot input audio as a local audio track
- subscribes to agent output audio/transcript/events
- handles reconnect and shutdown
- records LiveKit room/session metadata

Status: client-side bridge and replay CLI are scaffolded. The bridge publishes replay audio to a
LiveKit room and subscribes to remote audio/transcript/data events. It still requires LiveKit packages,
credentials, and a running LiveKit agent to produce real backend output.

Acceptance:

- WAV replay can produce a response through LiveKit without robot hardware.
- Output WAV is saved.
- Transcript and timing events explain the response path.

Replay command shape:

```bash
LIVEKIT_URL=wss://... \
LIVEKIT_API_KEY=... \
LIVEKIT_API_SECRET=... \
.venv/bin/livekit-replay artifacts/remote-runs/<run-id>/turn-....wav \
  --run-id livekit-smoke-001
```

Expected artifacts:

```text
artifacts/livekit-replays/<run-id>/
  input.wav
  output.wav
  events.jsonl
  transcript.jsonl
  manifest.json
```

### Phase 4 — LiveKit Agent

Implement or configure a LiveKit `AgentSession`:

```text
STT -> LLM -> TTS
turn detection
interruptions
preemptive generation
tools
```

Start with a modular STT-LLM-TTS pipeline, not a speech-to-speech realtime model, because the pipeline
gives better replay visibility.

Acceptance:

- Clear short utterance gets a coherent spoken answer.
- Long-speech WAVs show visible turn boundaries.
- Interruption/stale-input cases are explainable from events.

Status: minimal agent entry point scaffolded as `livekit-agent`. It follows the documented
STT-LLM-TTS `AgentSession` shape and is configured by environment variables:

```bash
LIVEKIT_AGENT_NAME=reachy-mini-receptionist \
LIVEKIT_STT_MODEL=deepgram/nova-3 \
LIVEKIT_LLM_MODEL=openai/gpt-5.2-chat-latest \
LIVEKIT_TTS_MODEL=cartesia/sonic-3 \
LIVEKIT_TTS_VOICE=<voice-id> \
.venv/bin/livekit-agent dev
```

The exact model names/keys still need to be confirmed in the target LiveKit project.

### Phase 5 — Official Runtime Integration

After offline replay is useful, port the same handler/bridge shape back into the official app checkout
or keep running this isolated package as the backend experiment.

Acceptance:

- m1max can run the LiveKit backend against robot mic/speaker.
- Existing reception policies still own greet/wave/goodbye.
- Artifacts include both robot events and LiveKit stage events.

## Offline Backend Benchmark — 2026-06-15

Purpose: compare the deployed official Hugging Face realtime backend against the current LiveKit
prototype on the same approved replay inputs, without using old live-session segmentation as the timing
source.

Input set:

```text
artifacts/official-runtime/full-retest-sohee-20260614-1346/input_speech_review/
  full-retest-sohee-20260614-1346-speech-01.wav
  full-retest-sohee-20260614-1346-speech-02.wav
  full-retest-sohee-20260614-1346-speech-03.wav
  full-retest-sohee-20260614-1346-speech-04.wav
  full-retest-sohee-20260614-1346-speech-05.wav
  full-retest-sohee-20260614-1346-speech-06.wav
```

Benchmark artifact:

```text
artifacts/backend-benchmarks/full-retest-sohee-backend-compare-001/
```

Method:

- Replay each WAV through the same handler boundary:
  `WavAudioSource -> OfficialStyleStreamRuntime -> backend handler -> WavAudioSink`.
- Measure `input_start_to_first_output_audio_s` and `input_done_to_first_output_audio_s`.
- Treat negative `input_done_to_first_output_audio_s` as valid realtime behavior: the backend started
  speaking before the full WAV finished streaming.
- Compare response coverage and backend-emitted transcripts, not human-judged audio quality.

Backends/configs in this run:

- `hf-official`: official Hugging Face realtime handler from the official app checkout, voice `Sohee`,
  deployed HF session proxy, no tools, short clinic-receptionist benchmark instructions.
- `livekit`: current local `livekit-agent` prototype using default env-backed choices:
  STT `deepgram/nova-3`, LLM `openai/gpt-5.2-chat-latest`, TTS `cartesia/sonic-3`, Silero VAD,
  multilingual turn detector.

Result summary:

| Backend | Output coverage | Median start -> first audio | Mean start -> first audio | Notes |
| --- | ---: | ---: | ---: | --- |
| Official HF | 5 / 6 | 2.581s | 3.853s | One no-output clip; several transcripts were still imperfect. |
| LiveKit prototype | 2 / 6 | 6.280s | 6.280s | Four no-output clips; user transcripts were often missing or partial. |

Transcript observations:

- Clip 001: both backends heard the main "much better / clearly" content; LiveKit also produced an
  extra erroneous user segment, `"All you're doing"`.
- Clip 002: both responded, but LiveKit dropped the beginning of the user text.
- Clip 003: HF heard `"What soccer?"` and responded; LiveKit produced no transcript/response.
- Clip 004: neither backend produced a useful final response; LiveKit only emitted partial `"Hi."`.
- Clip 005: HF heard `"Stalker."` and responded; LiveKit produced no transcript/response.
- Clip 006: HF split the input into two user turns and two responses; LiveKit heard only
  `"Supporting current team."` and did not respond.

Conclusion:

- For this replay set and current model choices, the LiveKit prototype is clearly worse than the
  deployed official HF backend on response coverage, transcript usefulness, and first-audio latency.
- This should not be read as a final rejection of LiveKit as an architecture. The likely variables are
  the current LiveKit provider/model choices, turn handling, and replay/session wiring.
- Do not prioritize LiveKit as the near-term replacement backend until a model/provider sweep improves
  coverage on these same approved WAVs.

Next LiveKit experiments, if we revisit it:

- Keep the same `backend-benchmark` harness and the same reviewed WAV set.
- Sweep STT first, because missing/partial user transcripts are the dominant failure mode in this run.
- Test whether longer post-speech drain, different turn detector settings, or explicit end-of-input
  signaling improves coverage.
- Compare against HF with the same benchmark instructions and no tools before running another live robot
  session.

## Gaps

- Real LiveKit SDK dependency and exact plugin set are not installed in this repo yet.
- Need LiveKit credentials or local server setup.
- Need STT/LLM/TTS provider choices and keys.
- Need real audio bridge implementation:
  - frame duration
  - sample-rate conversion
  - mono/stereo handling
  - backpressure
  - output track subscription
- Need event schema for LiveKit-specific telemetry.
- Need replay fixture selection from existing artifacts.
- Need robot-side live validation after offline replay is stable.

## First Implementation Scope

Build only Phase 1 and Phase 2 now. Do not add real LiveKit network code until the handler boundary and
WAV replay harness are tested locally.
