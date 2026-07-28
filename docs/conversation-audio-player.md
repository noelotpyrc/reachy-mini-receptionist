# Conversation / audio review — design

**Status:** v1 exporter + local aligned-WAV viewer + recovered-text sidecar implemented
(2026-07-01). The conversation/audio review path first produces one aligned stereo WAV, then
optionally serves a local browser viewer over that WAV with semantic event lanes. It exists because
Rerun **can't play audio**, and the whole point here is to *listen* while reading the
backend/runtime timeline. It consumes a focused conversation/audio subset of
`docs/general-timeline-model.md`; read-only over existing artifacts except for optional review
sidecars generated from those artifacts.

> "Replay" here means **scrub recorded data on a timeline** (the Rerun experience), **not**
> re-running the backend. Re-running the s2s backend is explicitly out of scope.

## Scope — minimal export

v1 exports one aligned review bundle per recorded run:

| File | Purpose |
|---|---|
| `audio-review-<run>.wav` | Stereo WAV: left=input mic, right=output speaker |
| `audio-review-<run>.labels.txt` | Audacity-compatible label track for turns, backend events, and human markers |
| `audio-review-<run>.json` | Structured metadata: timeline bounds, placements, labels, source paths |

Input placement is an inferred alignment, not physical ground truth. The practical mode validated on
`official-live-20260625-133754` anchors the input WAV sample clock to the first backend/VAD
`input_audio_buffer.speech_started` event by matching it to the nearest input sidecar chunk. Output
is placed per response, anchored at `assistant.audio.started` when present, so response gaps remain
audible instead of compressing the output stream.

**Explicitly *not* here:** this is not a replacement for the L1/Rerun physical timeline. The viewer
does include policy/cue markers and robot audio playback spans where they help explain audio, but it
does not try to render the full camera/motion/perception layer. No backend re-execution /
replay-as-rerun.

## Entry point

This is a developer diagnosis tool, not an OPS control surface. Do **not** expose it through
`reception-ops`.

Canonical v1 command:

```bash
.venv/bin/reception-audio-review artifacts/official-runtime-live --run-id <run_id>
```

It writes:

- a stereo aligned WAV
- an Audacity labels file
- a metadata JSON sidecar

To open the local viewer:

```bash
.venv/bin/reception-audio-review artifacts/official-runtime-live --run-id <run_id> --serve
```

The viewer plays the aligned stereo WAV through one native audio element. The timeline playhead is
`audio.currentTime`; event overlays are drawn from metadata relative to the WAV zero point. A
response dropdown in the playback controls exposes per-response WAV playback so a reviewer can hear
one robot utterance without scrubbing the full aligned mix. The UI does not perform runtime
alignment.

If present, `audio-review/<run_id>/recovered-text-<run_id>.jsonl` is loaded as an optional recovered
text sidecar. Those rows must include `response_id`, `text`, `source`, and `model`; generated rows
also include the source `audio_path`, sample metadata, and `generated_on`. They are displayed as
STT-recovered derived text, never as backend-emitted transcript.

To regenerate that sidecar for responses where the robot spoke but the backend did not log assistant
text, run the recovery wrapper on m1max. It uses the S2S backend Python environment so it can import
the deployed `speech_to_speech` Parakeet STT handler:

```bash
scripts/m1max/recover_audio_review_text.sh <run_id>
```

By default, recovery preserves existing sidecar rows and only transcribes missing response IDs. Use
`--overwrite-recovered-text` when intentionally refreshing all recovered text for the run. The
generated file is still Cat-2 derived data: useful for human review, but not a replacement for
backend-emitted transcript events.

Transcript provenance is intentionally lane-separated:

- **Assistant text transcript** contains only backend-emitted assistant text
  (`response.output_audio_transcript.done` or scoped assistant `handler.output`).
- **STT recovered transcript** contains only sidecar text recovered from per-response robot WAVs.
- **Transcript availability** says whether each robot-audio response has backend transcript evidence.

Do not backfill the Assistant text transcript lane with STT-recovered text or inferred placeholders.
An empty Assistant text transcript lane beside robot audio means "backend did not log assistant text";
use the availability and recovered lanes to inspect that gap.

No raw audio leaves the machine. The exporter/viewer is a convenience wrapper around existing
artifacts, not a recorder, backend replay, or robot runtime.

## The backend timeline track = L1's backend span, *uncollapsed*

L1 shows the backend as one span (`speech_started` → `response.output_audio.done`) with internals
hidden. This exporter **uncollapses** that span for listening/debugging — the same `hf.*` events L1's
allowlist drops, now shown as the per-turn breakdown:

- `…input_audio_buffer.speech_started` / `speech_stopped` (VAD turn bounds)
- `…conversation.item.input_audio_transcription.completed` (transcript text)
- `…response.created` (sparse S2S response signal, not a reliable start marker for every response)
- `…response.done` (reliable response completion/status marker when recorded)
- `…response.output_audio.done` (TTS done)

Aligned as labels over the stereo WAV, so you can hear the input/output relationship in a real audio
editor while reading turn and backend timing markers on the same timeline.

## Why in-house, not Rerun

The two primary tracks are the **input and output audio *playing***. Rerun has no audio playback, so
this can't be Rerun. v1 exports standard audio artifacts so playback, playhead, seeking, zoom, and
selection come from a proven audio editor. **Local / private** matters: raw clinic audio must not
leave the machine.

## Inputs — all already recorded (read-only)

The exporter reads existing artifacts; no new instrumentation, works on historical runs:
- input/output WAVs + per-chunk sidecars (`ts`, `sample_start`, `samples`, `rms`) — sidecars anchor
  each audio source and map output responses back to exact WAV sample ranges.
- per-response WAVs + sidecars for individual robot utterance playback and optional STT recovery.
- backend `hf.*` sub-stage events — in the events lane (today tagged `hf.*`; see the lane note in
  `general-timeline-model.md`).
- `markers-<run>.jsonl`.
- optional `audio-review/<run_id>/recovered-text-<run_id>.jsonl` review sidecar.

All exported labels align on **wall-clock `ts`** relative to the WAV zero point.

## Alignment Semantics

The raw WAV bytes are reliable sample streams. The audio sidecar `sample_start` / `samples` fields
are reliable locations inside those WAV files. The sidecar `ts` field is weaker: it is the M1Max
recorder-observed write/drain timestamp, not a guaranteed physical mic-capture or speaker-playback
timestamp.

For current artifacts, the most useful full-run review alignment is:

```text
input_anchor_mode = first_speech_vad_sync
input_wav_start_ts = first_speech_started_ts - nearest_input_chunk.sample_start / sample_rate
output_response_start_ts = assistant.audio.started_ts for that response_id
```

This works for our current test-style wave-chat runs because there is a known first user speech event
and the policy surface is limited. It is not a general proof of physical timing. It can be wrong or
unavailable when:

- the run has no user speech / no VAD speech-start event,
- the first speech-start event is itself delayed or wrong,
- policies generate output before a reliable input sync point,
- multiple independent input/audio paths are mixed,
- the goal is exact physical overlap between room speech and robot speaker output.

The exporter metadata must record the anchor mode, anchor event, nearest chunk, and inferred
`input_wav_start_ts` so reviewers know which timeline they are listening to.

Trust chain for the current practical solution:

1. Trust the input WAV sample order and sample spacing: sample `N` occurs `N / sample_rate` seconds
   after sample 0 inside the WAV.
2. Trust `sample_start` / `samples` in the sidecar as exact locations inside the WAV.
3. Trust the first backend/VAD `input_audio_buffer.speech_started` timestamp as the best available
   external timestamp for one point inside that WAV.
4. Infer the input WAV's wall-clock start from that sync point:

```text
input_wav_start_ts =
  first_speech_started_ts - nearest_input_chunk.sample_start / sample_rate
```

This is why the approach can produce a useful review timeline even when the recorder's first input
chunk timestamp is not a reliable WAV-start anchor. It assumes the concat/sample path follows real
time once the WAV sample clock is established; it does not prove the physical capture timestamp for
sample 0.

## Viewer Lanes

The local viewer organizes events into separate lanes instead of a single mixed lane:

- **VAD** — backend speech start/stop spans.
- **STT partial** — streaming transcript deltas.
- **STT final** — completed user transcripts.
- **Transcript to LLM** — runtime `handler.output` user messages.
- **S2S response lifecycle** — response status events plus the `first audio` / `audio done`
  anchors used by turn windows. `response.created` is shown as a sparse signal, not the primary
  response start anchor.
- **Assistant text transcript** — assistant text / output transcript events when recorded.
- **STT recovered transcript** — optional derived text from response WAV STT sidecars. This is
  fallible Cat-2 data and visually separate from backend transcript.
- **Transcript availability** — per-response diagnostics showing whether backend assistant transcript
  events were logged for robot audio. This lane does not contain inferred assistant text.
- **TTS / audio generation** — response output-audio generation spans/done markers.
- **Robot audio playback** — `assistant.audio.started` → `assistant.audio.done` spans.
- **Policy / cues** — policy, antenna, and ready-cue events.
- **Human markers** — marker tool notes when present.

Finite-value diagnostic lanes use value-specific colors rather than one lane-wide color: S2S
response lifecycle (`response.created signal`, `first audio`, `response done`, `audio done`),
Transcript availability (`backend transcript logged`, `no backend transcript event`), and
TTS / audio generation (`audio generation`, `audio done`).

## Relationship to the model and Rerun

- **Uses the same timeline model** (`general-timeline-model.md`) for shared anchors, then deliberately
  uncollapses backend internals into audio-review-only lanes.
- **Rerun handles the physical/vision layer** (camera, motion, detections); this exporter handles
  conversation/audio. Two tools, one recorded run.
- **Human markers appear in both** — the shared thread that ties a moment in this player to the same
  moment in Rerun.

## Open / deferred

- Conversation-script panel with per-turn playback: deferred. The current UI has timeline lanes and
  response-WAV dropdown playback, but not a full user/assistant script view.
- Optional run-on cluster annotation for speculative-turn text drops.
- Audio-listening UX details beyond v1: A/B overlap controls, loop selected range, keyboard
  shortcuts.
