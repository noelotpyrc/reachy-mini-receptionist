# Data & recording harness

What the reception robot records, how to reason about it (raw vs opinionated vs derived), and where
the gaps are for debugging/tuning. Reference for anyone — **including other agents** — working on
instrumentation, the eval framework, or performance tuning.

## Channels — what gets recorded

| Channel | File | Content | When | Code |
|---|---|---|---|---|
| **Run manifest** | `artifacts/runs/run-<run_id>.json` | Per-daemon manifest tying all artifacts together: config, log path, event log path, video/capture/audio/turn files, counts, open/closed status | always | `reception.py` manifest helpers |
| **Durable log** | `artifacts/logs/reception-<run_id>.log` | Human-readable timeline (HH:MM:SS): `N person(s)`, APPROACH events, visit-state (`dom/absent/peak/greet/depart`), `react`/`farewell`, conversation opened/ended, `voice: heard`/`reply`, errors | always | `reception.py` (`logging.basicConfig` + `log.info` throughout) |
| **Events** | `artifacts/events.jsonl` | Alert-engine trigger feed; one JSON line per event: `{run_id, type: approach\|depart\|wave, ts, id, area, cx, cy}`; wave: `{run_id, type, ts, gesture, score}` | always | `perception.py` (`DEFAULT_EVENTS_PATH`, event `rec` ~L55; wave ~L75) |
| **Video** | `artifacts/video-<run_id>-NN.mkv` | **Raw** camera frames (cv2 `mp4v` in mkv, ~5 fps = `--vision-interval`). **No audio track, no annotations.** mkv (not mp4) = crash-resilient | `record on` | `reception.py` `record_on` / `_write_video` |
| **Raw audio** | `artifacts/audio-<run_id>-NN.wav` + `.jsonl` | **Raw** 16 kHz mono float mic samples + sidecar: `{run_id, ts, sample_start, samples, rms, speaking/forwarded}`. `sample_start`/`samples` are exact WAV locations; `ts` is recorder-observed write/drain time, not guaranteed physical capture/playback time | `audio-record on` / official-runtime `--record-audio` | legacy `session.py`; official-runtime `ArtifactRecorder` |
| **Utterances** | `artifacts/utterances/utterances-<run_id>.jsonl` + per-utterance `.wav` | First-pass VAD-endpointed audio events with timing: `{run_id, utterance_id, speech_start_ts, speech_end_ts, queued_ts, wav, dur}` | `--save-turns` + voice transcripts | `session.py` VAD queue; `reception.py` `_save_transcript_artifacts` |
| **Transcripts** | `artifacts/transcripts/transcripts-<run_id>.jsonl` | First-pass STT worker output: `{run_id, utterance_id, speech_start_ts, speech_end_ts, queued_ts, stt_start_ts, stt_done_ts, model, text, error?}` | voice transcripts | `stt_worker.py`; `session.py` `listen_read`; `reception.py` |
| **Capture** | `artifacts/capture-<run_id>-NN.jsonl` | Per-frame detector output: `{run_id, ts, n, tracks:[{id, area, cx, cy, box}], events:[…]}` | `capture on` | `reception.py` `capture_on` / `_write_capture` |
| **Turns** | `artifacts/turns/turns-<run_id>.jsonl` + per-turn `.wav` | Per conversation turn: `{run_id, ts, n, dur, heard, reply, wav}` + the utterance audio (16 kHz) | `--save-turns` | `reception.py` `_save_turn` |
| **Markers** | `artifacts/markers-<run_id>.jsonl` | Live **human** feedback anchors: `{run_id, n, ts, clock, note}` — one line per Enter-press during a live test, annotated after. Turns subjective UX reactions into queryable timestamps aligned (by `ts`) to every other channel | live test (manual) | `scripts/m1max/mark.py` |

`replay.py` re-runs perception on a recorded `.mkv` (+ annotates boxes) → offline vision tuning/regression.
Raw audio is not yet replay-wired, but the WAV + JSONL sidecar preserves the Cat-1 signal needed to
re-run VAD/STT offline.

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
- **Detections / tracks** (`capture.tracks`) — RF-DETR, conditional on `threshold`.
- **Events** (`events.jsonl`: approach/depart/wave) — perception geometry + debounce (the false-greets live here).
- **STT `heard`** — faster-whisper transcript (e.g. the "Also they're going" errors).
- **Transcript events** — STT text plus the true speech timing and STT timing; first-pass brain input
  now drains ordered transcript batches instead of a synchronous one-utterance `listen_read` result.
- **STT-recovered assistant text sidecars** (`audio-review/<run_id>/recovered-text-<run_id>.jsonl`)
  — offline ASR over the robot's own per-response WAVs, used only when backend assistant transcript
  events are missing. This is fallible model output and must stay visually/provenance-separated from
  backend-emitted assistant transcript.
- **Wave `score`** — MediaPipe Open_Palm probability.
- **Brain `reply`** — LLM generation, conditional on model + persona + context.

### 3. Derived / aggregated — computed from 1 + 2
Re-derivable; inherits Cat-2's errors. Convenient for monitoring / debugging logic, not a source of truth.
- **Visit-state** (`dom/absent/peak/greet/depart` latches, `approach.py`) — smoothed/latched area signal.
- **Conversation lifecycle** (idle-45s / max-cap close) — from `last_heard` timestamps.
- **Counts / summaries** (capture frames/events; `buffer_duration` / `dur`).
- **The durable-log narrative** — a human-readable rendering of 2 + 3.

## Gaps (debugging/tuning blind spots)
1. **Raw audio is separate from video** — can't watch + listen in one file; align by sidecar `ts` for now.
2. **No audio replay tool yet** — raw WAV exists, but VAD/STT cannot yet be re-run from the same harness style as `replay.py`.
3. **STT-worker transcript stream needs live validation** — first pass is implemented offline, but queue
   age/backlog behavior and CPU contention still need controlled robot runs.
4. **No VAD/STT diagnostics** — VAD fire/miss + speech probabilities, and STT confidence, are not logged.
5. **Latency is partial** — transcript events capture VAD-endpoint → STT timing, but brain/TTS timing is
   still mostly inferred from durable logs.
6. **Per-frame gesture scores not captured** — only the debounced wave *event*, not every frame's Open_Palm probability.
7. **`save-turns` still not a full eval record** — turns now include transcript batch metadata, but not
   STT confidence, VAD probabilities, or a structured brain/TTS latency breakdown.
8. **Timeline still not rendered** — files now share `run_id` + wall-clock `ts`, but there is no merged human-readable timeline artifact yet.
9. **Recording lifecycle is still runner-coupled** — WAV/video/capture files are streamed during
   the run, but finalization still depends on the live runner reaching `ArtifactRecorder.close()`.
   Immediate fix: graceful runner shutdown. Planned stronger fix: recorder sidecar process owned by
   OPS, so artifacts can flush/finalize even if the runner crashes.
10. **Audio chunk timestamps are recorder-observed, not physical audio timestamps** — current audio
   sidecar `ts` values are written by M1Max when frames are drained/written. They can be bursty and
   should not be treated as exact mic-capture or speaker-playback times. Current audio review can use
   `first_speech_vad_sync` as a practical inferred anchor for controlled wave-chat runs, but the real
   fix is to record explicit capture/read/playback-submit timestamps and sequence numbers.

## Takeaway + instrumentation priority
- **Cat-1 is the reusable asset; Cat-2/3 are disposable** (re-derivable from Cat-1 + a model).
- **Vision already has its Cat-1** (raw video) → replayable + tunable offline. That's why vision tuning works.
- **Audio now has Cat-1** → the next step is making the voice path replayable from that raw signal.
- **Priority order:** (1) live-validate timestamped utterance artifacts + separate STT worker /
  transcript stream, (2) audio replay/eval from `audio-*.wav`, then fuller per-stage latency,
  VAD/STT diagnostics, and a merged timeline view over one run manifest.
