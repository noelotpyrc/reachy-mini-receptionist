# Data & recording harness

What the reception robot records, how to reason about it (raw vs opinionated vs derived), and where
the gaps are for debugging/tuning. Reference for anyone — **including other agents** — working on
instrumentation, the eval framework, or performance tuning.

**Status:** official-runtime artifact layout updated 2026-08-06. Legacy daemon artifacts may still
exist in historical runs, but they are not the current recording contract.

## Channels — what gets recorded

| Channel | File | Content | When | Code |
|---|---|---|---|---|
| **Run manifest** | `official-runtime-live/runs/run-<run_id>.json` | Resolved config, provenance, lane paths, recording state, counts, and close status | always | `official_runtime.artifacts.ArtifactRecorder` |
| **Runner log** | `artifacts/logs/<run_id>.log` | Human-readable startup, media, backend, policy, and shutdown diagnostics | always | OPS-launched `official_runtime.live_app` |
| **Events** | `official-runtime-live/events/events-<run_id>-NN.jsonl` | Runtime, perception, conversation, speaker, and robot events | always | `ArtifactRecorder.event` |
| **Policies** | `official-runtime-live/policies/policies-<run_id>-NN.jsonl` | Policy inputs, decisions, suppression, and speech requests | always | `ArtifactRecorder.policy` |
| **Realtime** | `official-runtime-live/realtime/realtime-<run_id>-NN.jsonl` | S2S protocol lifecycle, transcripts, audio lifecycle, milestones, and door-policy observations | always | `ArtifactRecorder.realtime` |
| **Raw video** | `official-runtime-live/video/video-<run_id>-NN.mkv` plus `.jsonl` | Raw camera frames plus runner-observed per-frame timestamps; no annotations or audio track | `--record-video` | `ArtifactRecorder.vision_frame` |
| **Raw audio** | `official-runtime-live/audio/audio-<stream>-<run_id>-NN.wav` plus `.jsonl` | Continuous input/output samples with exact sample offsets, timestamps, RMS, and forwarding/speaking context | `--record-audio` | `ArtifactRecorder.audio_frame` |
| **Vision capture** | `official-runtime-live/capture/capture-<run_id>-NN.jsonl` | Per-frame people, logical tracks, visitor state, and emitted perception events | `--capture-vision` | official-runtime perception pipeline |
| **Detection layers** | `official-runtime-live/detections/detections-<run_id>-NN.jsonl` | Configured semantic detector outputs, confidence, boxes, latency, and queue counters | additional vision pipeline configured | `LiveDetectionManager` through `ArtifactRecorder` |
| **Per-response audio** | `official-runtime-live/audio/playable/*.wav` plus metadata | Robot-playable response audio associated with realtime response IDs | response audio present | S2S handler/artifact recorder |
| **Markers** | `official-runtime-live/markers/markers-<run_id>.jsonl` | Human feedback anchors aligned by wall timestamp | manual live test | `scripts/m1max/mark.py` |
| **Derived review** | `audio-review/<run_id>/...`, `.rrd`, and review JSON | Reconstructed timelines, aligned listening, recovered transcript sidecars, and vision overlays | offline review | audio/Rerun/door review tools |

Use `reception-vision-replay`, `reception-door-review`, the audio-review app, and the S2S replay
harnesses for offline reproduction. Derived review output must remain provenance-separated from raw
recordings and backend-emitted text.

## Audio input flow & echo cancellation (verified 2026-06-25)

How to reason about the mic-input stream (official-runtime path):

- **Continuous + full-duplex once the gate opens.** A wave opens the audio gate
  (`reception.should_forward_audio` → `_conversation_active`); from then the runtime forwards
  *every* mic frame to the backend (`stream_runtime._input_loop`, **no client-side VAD/filtering**),
  including background noise and **while the robot is speaking** — forwarding never pauses for robot
  speech. Turn-taking / barge-in are the backend's job (its Silero VAD at `--thresh 0.6`).
- **The robot's own voice is NOT in the stream — hardware AEC.** The wireless robot captures through
  ALSA `reachymini_audio_src`, which routes through the **XMOS AEC loopback**
  (`reachy_mini/media/audio_gstreamer.py:145-150`); `get_audio_sample()` returns the post-echo-
  cancellation signal.
- **Evidence** (run `official-live-20260623-142850`, input vs output RMS sidecars): while the robot
  speaks, mic-input median RMS = **54** vs **69** when silent (**0.78×**, not elevated), while the
  robot's output is **~30× louder**. The robot's voice does not leak into the forwarded stream.
  → Background noise / real user speech / barge-in *are* in the input; robot self-echo is *not*
  (so it's ruled out as an STT-mishear cause; room noise is not).

## Data taxonomy — what to trust, what to record

### 1. Raw / ground truth — un-opinionated
The actual sensor reality; no model has touched it. **The only artifact you can re-run *every* model
against** → the reusable reference for tuning + eval.
- **Video frames** (`.mkv`) — raw pixels. ✅ have.
- **Raw continuous mic audio** (`audio-*.wav` + sidecar) — raw mic samples in reliable sample order.
  ✅ have. The sidecar's `sample_start` / `samples` fields map each chunk to the WAV exactly; its
  `ts` field is an observed recorder timestamp and is useful for rough windowing, not physical
  truth. The sidecar marks chunks recorded while the robot is speaking; VAD/STT still ignore those
  chunks, but the Cat-1 signal remains available for review.
- **Per-turn WAVs** — a *hybrid*: the bytes are raw (Cat-1), but *which* audio exists and where it's
  cut is a **VAD decision** (Cat-2). Keep using raw continuous audio as the source of truth.

### 2. Opinionated / conditional — a model's interpretation
Output of some model, conditional on its weights + thresholds. Tunable and fallible; **validate against
Cat-1, never trust as truth.** Worth recording only to see *what the model decided at the time*.
- **Detections / tracks** (`capture` and `detections`) — RF-DETR, MediaPipe, and configured semantic
  detectors, conditional on model versions and thresholds.
- **Events** (`events` and `policies`) — visitor geometry, door state, debounce, policy rules, and
  suppression decisions.
- **STT transcripts** — backend Parakeet output plus endpointing/timing decisions.
- **STT-recovered assistant text sidecars** (`audio-review/<run_id>/recovered-text-<run_id>.jsonl`)
  — offline ASR over the robot's own per-response WAVs, used only when backend assistant transcript
  events are missing. This is fallible model output and must stay visually/provenance-separated from
  backend-emitted assistant transcript.
- **Wave `score`** — MediaPipe `Open_Palm` probability for the live/default detector. Experimental
  offline `Hand_Motion` events use normalized horizontal displacement and identify that meaning as
  `score_kind=normalized_horizontal_displacement` in gesture diagnostics.
- **Assistant response text** — Hermes/direct-provider LLM generation, conditional on model,
  profile-owned context, tools, and session state.

### 3. Derived / aggregated — computed from 1 + 2
Re-derivable; inherits Cat-2's errors. Convenient for monitoring / debugging logic, not a source of truth.
- **Visitor and door state** — observed/retained presence, proximity, motion, door movement, logical
  track handoff, policy candidates, and latches.
- **Conversation lifecycle** — policy-owned session state plus backend response lifecycle.
- **Counts, latency summaries, and renderer spans** — convenient views reconstructed from raw and
  opinionated lanes.
- **The runner-log narrative** — a human-readable rendering of model and runtime decisions.

## Gaps (debugging/tuning blind spots)
1. **MKV playback time is not wall time.** The fixed-`5 FPS` writer compresses recordings when the
   vision loop produces frames more slowly. All 14 retained live MKVs showed this behavior; encoded
   duration was `43-63%` of the sidecar frame span. This is an accepted limitation for qualitative
   review, not a production blocker, because replay/Rerun timing uses recorded sidecar timestamps.
   When reporting an issue found by eyeballing an MKV, provide the run id and player position as
   `position / total duration` (and optionally the normalized percentage). Convert that position to
   the corresponding decoded frame index, then use that frame's video-sidecar `ts` to anchor logs,
   audio, detections, and policy events. Do not linearly interpolate between the first and last wall
   timestamps because frame cadence varies during a run. This mapping assumes the MKV was not
   trimmed/transcoded and its decoded frame count still matches the sidecar.
2. **Raw audio is separate from video.** The review tools align channels from sidecars; there is no
   single audiovisual recording container.
3. **Recording lifecycle is still runner-coupled.** WAV/video/capture files are streamed during
   the run, but finalization still depends on the live runner reaching `ArtifactRecorder.close()`.
   Immediate fix: graceful runner shutdown. Planned stronger fix: recorder sidecar process owned by
   OPS, so artifacts can flush/finalize even if the runner crashes.
4. **Audio chunk timestamps are recorder-observed, not physical audio timestamps.** Current audio
   sidecar `ts` values are written by M1Max when frames are drained/written. They can be bursty and
   should not be treated as exact mic-capture or speaker-playback times. Current audio review can use
   `first_speech_vad_sync` as a practical inferred anchor for controlled wave-chat runs, but the real
   fix is to record explicit capture/read/playback-submit timestamps and sequence numbers.
5. **Camera timestamps are runner-observed, not sensor PTS.** The SDK still returns pixels without a
   camera timestamp. The video sidecar is the current practical alignment source.
6. **Production health requires deployment acceptance.** OPS now reports Hermes/provider,
   camera/microphone progress, managed services, recording age, and disk headroom. The checks still
   need validation from the activated m1max release and a controlled media-fault test.
7. **Recording policy is defined.** Production keeps audio and derived vision diagnostics, defaults
   raw MKV video off, and reports audio/video older than 30 days for reviewed cleanup. The reminder
   never deletes automatically.

## Takeaway + instrumentation priority
- **Cat-1 is the reusable asset; Cat-2/3 are disposable** (re-derivable from Cat-1 + a model).
- **Vision already has its Cat-1** (raw video) → replayable + tunable offline. That's why vision tuning works.
- **Audio and video have Cat-1**, and both have offline review/replay consumers.
- **Priority order for production:** define recording privacy and retention, make finalization
  failure explicit/crash-resilient, and add active media/artifact health checks. Treat MKV playback
  as qualitative and use the video sidecar for timing. See
  [`production-readiness.md`](production-readiness.md).
