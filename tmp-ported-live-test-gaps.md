# Ported Runtime Live-Test Gaps

Date: 2026-06-17

Goal: prepare a live test today using the ported `reachy_mini_brain.official_runtime` path, with
the m1max local S2S backend first and HF remote as fallback/comparison.

## Blocking Gaps

1. No live entrypoint.
   - Existing commands cover offline replay/benchmark only.
   - Need a command that starts robot media, backend, policies, artifacts, and teardown.

2. No robot media adapters.
   - Need robot mic source backed by `mini.media.get_audio_sample()`.
   - Need robot speaker sink backed by `mini.media.push_audio_sample()`.
   - Need camera frame provider backed by `mini.media.get_frame()`.

3. No live composition wiring.
   - Need to connect `ArtifactRecorder`, `ReceptionPolicy`, `PlaybackMovementGate`,
     `PerceptionPipeline`, camera/head-tracking capabilities, backend handler, and robot IO.

4. Backend selection is not live-wired in the ported package.
   - Preferred: m1max local S2S backend through the HF-compatible realtime websocket.
   - Fallback: HF remote realtime backend.
   - Current benchmark code can invoke official-app HF handler, but live runner should expose this
     clearly as an adapter so we know what is ported and what still depends on official app code.

5. Movement is only primitives.
   - `AntennaPulseMove` and `PlaybackMovementGate` exist, but there is not yet a live movement adapter.
   - For first live test, keep movement minimal: antenna pulse if a movement manager exists, and no
     continuous motion unless explicitly wired.

6. Head tracking is only a toggle/capability.
   - First live test should not rely on full face-offset head tracking.
   - Camera Q&A and head-tracking live loop can be tested after the basic audio/policy path works.

7. Artifact recorder is ready but not attached to live sources.
   - Need input/output audio taps, backend events, policy events, config snapshot, and run manifest.

## Minimum Implementation For Today's Test

1. `official_runtime/robot_io.py`
   - `ReachyAudioSource`
   - `ReachyAudioSink`
   - `ReachyCameraFrameProvider`
   - SDK patch and warmup helpers
   - Status: implemented. After the first live smoke, `ReachyAudioSink` was patched to split backend
     audio into exact 20ms WebRTC frames derived from sample rate, then push with monotonic realtime
     pacing; local and m1max direct checks pass.

2. `official_runtime/live_app.py`
   - `official-runtime-live` CLI
   - start robot SDK/media
   - start selected backend
   - wire artifact recorder + runtime observers
   - clean shutdown
   - Status: implemented as `official-runtime-live`.

3. Backend choices
   - `--backend hf-official`
   - `--hf-connection-mode local|remote`
   - `--hf-realtime-ws-url ws://100.127.86.67:8765/v1/realtime` for m1max local backend
   - optional HF token/remote config from `.env`
   - Status: implemented. `hf-official` is a temporary backend adapter around the official app HF
     handler. The live runner itself is in the ported path.

4. Focused dry tests
   - no robot hardware
   - fake source/sink/backend
   - validate CLI imports/options, artifact creation, backend config, and observer wiring
   - Status: implemented for CLI/help and fake robot IO; focused suite passes locally.

## Remaining Live-Test Cautions

- `hf-official` still depends on the official app checkout and its installed dependencies.
- The first live command should use `--no-perception --no-audio-gate` for a backend/audio smoke, or
  `--perception --gestures` only after we confirm required detector dependencies are installed on m1max.
- The ported path does not yet put the robot to sleep on exit; teardown closes SDK media/client only.
- Full face-offset head tracking is not wired yet. The head-tracking capability only preserves the
  official toggle boundary.
- The first ported smoke produced robot output, but user feedback was choppy/low-high volume. The first
  retest should repeat the same minimal setup after the 20ms sink patch before enabling perception or
  wave/chat.
