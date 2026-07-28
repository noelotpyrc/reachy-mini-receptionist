# Rerun integration — the Rerun renderer

**Role:** the **Rerun renderer** for the diagnosis tooling (TODO #6) — one of *two* renderers that
consume the settled model in **`docs/general-timeline-model.md`** (read that first; it owns the
actors, spans/markers, event allowlist, and `event→behavior` adapter). This doc only covers *how to
render the model in Rerun*: the **physical/vision layer** (camera, motion, detections — Rerun's
native strength) plus the **L1 spans as state-scalars** under `<actor>/<sub-entity>` folders (entity
layout below). The **conversation/audio** layer (listening to input/output audio) is the *other*
renderer — the in-house player, `docs/conversation-audio-player.md` — because Rerun can't play audio.

**Status:** a **flat-firehose prototype** exists (`official_runtime/rerun_review.py`) that logs every
event type as its own `TextLog` by lane/prefix — **not** the model. TODO #6 reworks it to the model
(allowlist → `<actor>/<sub-entity>` → spans-as-state-scalars). `rerun-sdk==0.33.1` is pinned as the
optional `diagnosis` extra.

> **Superseded sections below.** The "Why Rerun", "Inputs", and entity-layout sections are current.
> The **lane-reading description and the Stage 0/1 gap framing predate the model** and are kept only
> as background — the model doc + the entity layout are authoritative. Don't implement from the
> Stage 0/1 tables.

## Why Rerun

[Rerun](https://rerun.io) is a time-aligned, multimodal viewer for robotics: you log
*"at time T, set value V on named path P"* and it gives you a scrubbable timeline with
text-event lanes, scalar plots, images/video, and transforms, all sharing one clock.

It's the natural fit here for three reasons:
- **Officially blessed.** The Reachy Mini SDK already ships `reachy_mini/utils/rerun.py`,
  which streams the robot URDF + joints/antennas (50 Hz via `/api/state/full`) + camera into
  Rerun. We extend the *same substrate* from robot-state to the conversation layer.
- **Zero new runtime instrumentation for v1.** Every artifact row the `ArtifactRecorder`
  writes already carries `{run_id, ts, type, …}` (see `official_runtime/artifacts.py`
  `_write_jsonl`). One row → one `rr.set_time("wall", timestamp=ts)` + `rr.log(entity,
  archetype)`.
- **It's both the run-summary *and* the live monitor.** The same mapping renders a recorded
  run offline *and* (live mode) streams a running session — fixing the "console milestones go
  quiet mid-conversation" gap.

The whole model is two calls per row:
```python
rr.set_time("wall", timestamp=row["ts"])      # the run's own wall-clock
rr.log(entity_for(row), archetype_for(row))
```

## Inputs we already have (verified)

All under `artifacts/official-runtime-live/` for a run, except markers (one level up).
Record defaults: **`--record-audio` ON**, `--record-video` OFF, `--capture-vision` ON via
`live_ops.sh`.

| Artifact | Path | Rows carry | Have it? |
|---|---|---|---|
| Realtime lane | `realtime/realtime-<id>-NN.jsonl` | `ts` + `type`: runtime milestones, `session.snapshot`, `movement_gate`, antenna/ready cues | ✅ always |
| Policy events | `policies/policies-<id>-NN.jsonl` | `ts` + `type` (greet, wave, cue start/stop, suppression) | ✅ always |
| Events lane | `events/events-<id>-NN.jsonl` | superset — **and the only place backend conversation events land today**, tagged `hf.*` (speech start/stop, transcript, response.created, audio deltas/done) | ✅ always |
| Audio | `audio/audio-{input,output,response-*}-<id>-NN.wav` + `.jsonl` | sidecar: `ts`, `sample_start`, `samples`, **`rms`** per chunk | ✅ on by default |
| Detections | `capture/capture-<id>-NN.jsonl` | `ts`, `people`, `tracks[]`, `events[]` | ✅ on via live_ops |
| Video | `video/video-<id>-NN.mkv` + `.jsonl` sidecar on new runs | frames; sidecar rows carry runner-observed frame `ts` | ⚠️ only if `--record-video` |
| Manifest | `runs/run-<id>.json` | `responses{}` latencies, `session{}`, `config` | ✅ always |
| Markers | `../markers-<id>.jsonl` (top-level `artifacts/`) | `ts`, `n`, `note` (from `scripts/m1max/mark.py`) | ✅ when marking |

Key alignment facts: **audio aligns accurately** (sidecar gives real `ts` + `sample_start`);
**video aligns by per-frame runner-observed timestamps on new runs**. Historical `--record-video`
runs without a video sidecar fall back to `capture/*.jsonl` frame timestamps when available; only
frames with no timestamp source use approximate `started_ts + i/fps` timing and must be marked as
approximate. **Markers live one directory above** the run root. The canonical #6 clock is the
**runner machine's wall-clock `ts`**; use it for timeline alignment and latency math in v1.

**Lane model:** the renderer must read multiple lanes. The events lane is the superset of
emitted `RuntimeEvent`s, including backend conversation events tagged `hf.*`. The realtime
lane still matters because some rows are written directly with `recorder.realtime()` and never
become `RuntimeEvent`s (`session.snapshot`, `movement_gate`, runtime milestones). Therefore v1
reads `events`, `realtime`, `policies`, and `markers`, then classifies rows by `type` prefix.
Do **not** reroute `hf.*` into realtime as a #6 prerequisite; keep that as optional future
cleanup for other consumers such as live ops status.

## Runtime cleanup backlog — not a v1 prerequisite

#6 v1 is read-only and should work on historical runs. The renderer derives the conversation
lane, first-X milestones, and per-turn latencies offline from existing JSONL/WAV sidecars.
The gaps below are useful later, especially for live ops (#5), but they do **not** block the
first Rerun renderer.

| # | Gap | Recorded today | #6 v1 action | Future runtime cleanup | Cplx | Value |
|---|---|---|---|---|---|---|
| GA | Lane split | `hf.*` in events; direct milestones in realtime | Read both lanes and classify by `type` prefix | Optionally duplicate/reroute `hf.*` for future consumers | **Low** | **High** |
| GB | Derived milestones not persisted as named rows | Raw rows include first mic, forwarded mic, output frames, gate events | Derive first-mic / first-forwarded / first-backend-audio / gate open-close offline | Persist named milestones when ops status needs them live | **Low** | Med–High |
| GC | Per-turn latency not precomputed | `input_audio_transcription.completed`, `response.created`, `assistant.audio.started`, `assistant.audio.done` | Derive transcript→response→first-audio→done scalars offline | Reuse same derivation live for ops status | Low–Med | **High** |
| GD | Motion **execution result** | cue *intent* only (`runtime.antenna_cue`) | Surface intent and suppression rows only | Emit `set_target` ok/latency/error + wobble / idle-breathing toggles | Med | **High** |
| GE | Gate event + dead idle-tick | gate state via policy data; `runtime.tick` never emitted | Use existing policy gate rows where present | First-class gate open/close event; emit `runtime.tick` | Low–Med | Med |
| GF | Token usage / cost + backend first-byte | computed in backend, `logger.debug` only | Not in v1 | Forward usage + backend first-audio ts through the observer hook | Med | Med |
| GG | Tool / function-call traces | none (tool specs forced empty in this path) | Not in v1 | When tools re-enabled: args / start / end / result / error | Med | High* |
| GH | Frame alignment | capture rows + video frames; new runs add video timestamp sidecar | Use sidecar/capture timestamps; warn on mismatches | Stable frame id + true camera/sensor ts if SDK exposes it | Med–High | Med |

*High when tools are enabled (disabled in this path today). GE's idle-tick remains a separate
runtime bug candidate; handle it outside #6 with its own evidence/test loop.

## Entity layout — one folder per actor

This renders the **general-timeline model** (`docs/general-timeline-model.md`), which is also the
**event allowlist**: keep only the spans/markers it names, drop the rest of the firehose. The
top-level entity path = **actor**, so the Rerun tree mirrors the model and folds per actor.

**Encoding** (Rerun has no native span archetype): **spans → a 0/1 state-scalar** at the path
(step plot reads as the bar) plus same-timestamp `START` / `END` `TextLog` detail at the same path;
**markers → `TextLog`** at the path.

```
policy/                          (grouped by behavior — see model doc)
  greet               markers  greet/greet_suppressed/cooldown_skip(greet) + speech/antenna(reason=approach)
  farewell            markers  farewell/farewell_suppressed/cooldown_skip(farewell) + speech/antenna(reason=depart)
  wave_conversation   span(envelope) + markers  wave_received/conversation_opened|closed/cooldown_skip(open)/speech(wave)
  conversation_cue    markers  thinking_started/stopped/start_suppressed
backend/
  processing          span     speech_started | response.created → response.output_audio.done
robot/
  speaker             span     assistant.audio.started → assistant.audio.done
  antennas/thinking   span     antenna_cue thinking: started → stopped
  antennas/pulse      span     antenna_cue policy_pulse: high → rest
  antennas/ready_cue  span     ready_cue: high → rest
perception/
  wave · approach · depart · <future>  markers  one per detection type (option 2)
human/
  feedback            markers  mark.py
session/
  milestones          markers  setup (optional preamble)
```

Policy sub-entities are **grouped by behavior**; shared mechanism events
(`speech_requested` / `antenna_pulse` / `cooldown_skip`) route to their behavior by their
`reason`/`action` field. The conversation envelope span lives under `policy/wave_conversation`.
See `general-timeline-model.md` for the authoritative grouping + routing rule.

The **physical layer** stays as Rerun's native strength, under its own folders separate from the
conversation tree above: `audio/<stream>/rms` + `wave` (`Scalars`), `camera/image` (`Image`,
decode MKV as the SDK's `rerun.py` does), `vision/people` (`Scalars`), `vision/tracks` (`Boxes2D`,
needs the frame). Per the layer split, this physical/vision data is where Rerun shines; the
span-heavy conversation tree is better served by an in-house/trace view (see the model doc's
renderer notes).

## Two modes

- **Offline (build first):** read a recorded run's files, replay rows onto the timeline. No
  robot, no install beyond `rerun-sdk`. The merge-and-sort-by-`ts` logic is already validated
  by a dry-run prototype (printed the unified timeline with markers landing next to the events
  they describe).
- **Live (later, high value):** attach an in-process observer to the runtime's event sink (the
  same seam `ArtifactRecorder` consumes) and stream to a viewer *during* a session — the live
  monitor the console can't give.

## Audio / video review proposal

**Audio.** We have input/output/per-response WAVs + sidecars on by default, accurately aligned.
- *RMS envelope lane* — coarse loudness over time; spot dropouts, choppiness, and
  speech/output overlap at a glance (cheap, high value).
- *WAV path hints* — print/log the source WAV path plus sample offset / approximate time range
  for each response or flagged span so a human can listen outside Rerun.
- *Waveform lane* — downsample the WAV and log as a scalar anchored by the sidecar's first
  `ts`/`sample_start` for a finer aligned trace (deferred after v1).
- **Honest limit:** Rerun renders audio **visually** (waveform), it does **not play audio in
  the viewer** (verify against the installed version). To *listen*, the timeline gives the exact
  WAV + sample offset to open in a player (or reuse the legacy `review_audio` clip-cutter for
  ears). For most audio bugs here (choppy / dropout / volume / overlap) the visual trace is
  enough to localize; listening confirms.

**Video.** Off by default; when enabled, new runs write an MKV plus a per-frame JSONL timestamp
sidecar.
- *With pixels* (`--record-video` run): decode MKV frames with cv2 → `rr.Image` at the per-frame
  sidecar `ts`. For historical runs without a sidecar, use capture-row timestamps for the decoded
  prefix and warn if decoded frame count diverges from timestamp count; use nominal FPS timing only
  for frames with no timestamp source.
- *Without pixels* (default): the `capture/*.jsonl` detections give a *vision lane* —
  people-count scalar + track/gesture events — so you still see when the robot saw a person /
  a wave, just not the image. Boxes overlay is only meaningful once pixels exist.

## Stage 1 — render backlog (Rerun)

**Gate:** no runtime instrumentation gate for v1. Read existing artifacts, merge by runner
wall-clock `ts`, and derive milestones/latencies offline. Ordered by recommended sequence
(high value / low complexity first).

| # | Increment | Inputs | Complexity | Debug value | Depends |
|---|---|---|---|---|---|
| 1 | Event + policy + **marker** timeline (text lanes), including `hf.*` transcript/response narrative | events/realtime/policy/markers ✅ | **Low** | **High** | none |
| 2 | **Suppression / missed-cue** annotations (`start_suppressed`, `greet_suppressed`, etc.) | policy/realtime ✅ | **Low** | **High** | 1 |
| 3 | Per-stage **latency** derivation in text/JSON review (transcript→cue→response→first-audio→done; not rendered as extra backend sub-entities) | events/realtime/manifest ✅ | Low–Med | **High** | 1 |
| 4 | **Audio RMS** envelope lane (in/out/per-response) | audio sidecars ✅ | **Low** | Med–High | 1 |
| 5 | WAV path + sample-offset hints for listening | audio WAV+sidecar ✅ | **Low** | Med–High | 4 |
| 6 | Portable `.rrd` per run + ops "open latest run" command | any run ✅ | Low–Med | Med–High | 1 (ties to TODO #5) |
| 7 | **Audio waveform** lane (downsampled, sidecar-anchored) | audio WAV+sidecar ✅ | Med | Med | 4 |
| 8 | **Detections** lane (people count + track/gesture events) | capture ✅ | Low–Med | Med | 1 |
| 9 | **Video frames** lane (cv2 decode → images) | MKV (needs `--record-video`) ⚠️ | Med–High | Med–High | 1 |
| 10 | Track **boxes overlaid on video** | capture + video ⚠️ | Med | Med | 8, 9 |
| 11 | **Robot motion-state** lane (joints/antennas/head) | live daemon / new capture ⚠️ | Med–High | Med | live or new record path |
| 12 | **Live observer** mode (in-process tap → stream during a run) | runtime event sink ✅ | **High** | **High** | 1 |

**Minimum accepted v1:** #1–#5. This is the read-only renderer over existing artifacts:
timeline+markers, transcript/response narrative, suppression/missed-cue annotations, per-turn
latency in text/JSON review, audio RMS, and WAV path/sample-offset hints for human listening. Explicitly defer
portable `.rrd`/ops `open latest run` wiring, audio waveform, detections, video, and live mode
until after v1.

## Relationship to OPS (#5)

OPS design is settled in `docs/ops-design.md`. Rerun is a **read-only diagnosis** surface,
not the operator control app. Boundaries to hold:
1. **Diagnosis tool, not the control UI** — the (future) non-tech control app and the Rerun viewer
   are separate surfaces that *share artifacts as data*. The ops API exposes a `review`/`open-run`
   *action* that launches Rerun; Rerun is never the app itself.
2. **`rerun-sdk` optional, out of the control core** — `status/start/stop/sleep/shutdown` must not
   depend on it; Rerun lives in an optional diagnosis module behind `collect-artifacts`/`review`.
3. **`.rrd` server-side, viewing client-side** — `collect-artifacts` produces a portable `.rrd` on
   m1max; the viewer runs on the user's machine (headless m1max has no GUI).

Shared data source: both the ops status model and the Rerun timeline read the **run manifest +
JSONL**. #6 derives milestones offline first; #5 can later reuse the same derivation live if
the ops status panel needs first-mic / first-forwarded / first-audio fields during a run.

## Open questions / spikes to settle before the bigger increments

- **Audio playback** in the installed `rerun-sdk` version — confirm visual-only vs any
  in-viewer playback; decides how much of "audio review" lives in Rerun vs an external player.
- **Headless m1max + remote review** — record a `.rrd` per run and scrub locally (simplest),
  or `rr.serve`/connect a local viewer over Tailscale. Avoid `spawn=True` on m1max.
- **`.rrd` size** for a full session (esp. with video) — sampling/retention policy.
- **Motion-state source** (#11) — it's *not* in recorded artifacts today; needs either live
  mode or a new capture lane before it can be replayed offline.
- **Runtime cleanup timing** — lane duplication, named first-X milestones, token usage, and
  motion execution results are deferred until another consumer needs them.

## Prerequisites

- Add pinned `rerun-sdk==0.33.1` as an optional diagnosis dependency (for example
  `.[diagnosis]`), not a core runtime/ops dependency.
- A recorded run to point at (e.g. sync one from m1max under
  `artifacts/official-runtime-live/`). A self-contained synthetic-run generator is a cheap way
  to develop the renderer with no robot/m1max dependency.
