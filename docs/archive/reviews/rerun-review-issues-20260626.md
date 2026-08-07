# Temporary Rerun Review Issues

Date: 2026-06-26

Context: review of `official-live-20260625-133754` Rerun output after the TODO #6 renderer changes.

## 1. Backend entity layout drift

Status: fixed in the TODO #6 renderer cleanup.

Issue: the current Rerun output has backend sub-entities that are not in the general timeline model.

Observed entities:

- `backend/processing`
- `backend/latency/*`
- `backend/turn`

Expected from `docs/general-timeline-model.md`:

- `backend/processing`

Notes:

- `backend/processing` is the intended model span.
- `backend/latency/*` is useful diagnostic output, but it is not part of the general model.
- `backend/turn` appears to be prototype/debug summary output and is not documented in the model.

Proposed cleanup to discuss:

- Remove `backend/turn` from the main Rerun renderer.
- Either remove `backend/latency/*` from the first model view or explicitly document it as optional diagnostic output outside the core model.

## 2. Span detail / inspectability gap

Status: fixed in the TODO #6 renderer cleanup.

Issue: span lanes are encoded as scalar `1.0` / `0.0` state changes, which makes them hard to inspect in Rerun.

Current behavior:

- `1.0` means the span is active.
- `0.0` means the span has ended.
- Clicking the point does not clearly show which raw event started or ended the modeled span.
- Nearby raw events on the same wall timeline can make the inspector look like the span came from the wrong event.

Example from the reviewed run:

- `policy.speech_requested` at `1782419970.080`
- `hf.realtime.response.created` at `1782419970.092`
- `backend/processing = 1.0` at `1782419970.092`

The backend span appears to start from the expected `hf.realtime.response.created` event, but the Rerun UI does not make that source relationship explicit.

Intended cleanup:

- Keep scalar `1.0` / `0.0` lanes for compact span visualization.
- For every scalar span point, add explicit detail at the same timestamp:
  - `1.0` point: matching `START` detail.
  - `0.0` point: matching `END` detail.
- Each detail should include:
  - modeled span name, such as `backend-processing`
  - model entity, such as `backend/processing`
  - source event type, such as `hf.realtime.response.created`
  - source timestamp
  - relevant IDs, such as `response_id` or `item_id` when available
  - short reason when applicable
- Apply this consistently to all modeled spans, not only `backend/processing`.

Example:

- `backend/processing = 1.0`
- detail: `START backend-processing from hf.realtime.response.created response_id=...`
- `backend/processing = 0.0`
- detail: `END backend-processing from hf.realtime.response.output_audio.done response_id=...`

## 3. Perception lane missing from Rerun output

Status: fixed for detection-trigger markers from `capture/*.jsonl`. Track boxes and gesture-score detail remain later physical/vision-layer work.

Issue: perception/detection data exists in raw artifacts, but the generated `.rrd` does not render it.

Evidence from `official-live-20260625-133754`:

- Raw capture file: `artifacts/official-runtime-live/capture/capture-official-live-20260625-133754-01.jsonl`
- `544` `vision_frame` rows.
- `433` frames with `people > 0`.
- `428` frames with `tracks`.
- `5` frames with detection trigger events:
  - `depart` at `1782419969.996`
  - `approach` at `1782419975.895`
  - `approach` at `1782419995.427`
  - `wave` at `1782419996.186`
  - `depart` at `1782420050.842`

Additional raw event-lane perception records:

- `340` `vision.gesture_candidate`
- `1` `vision.gesture_emitted`
- `1` `vision.gesture_detector_ready`
- `1` `vision.gesture_detector_init_start`

Observed `.rrd` issue:

- The generated Rerun file contains camera and policy entities.
- It does not contain `/perception/*` or `/vision/tracks` entities.

Intended cleanup:

- Render capture detection trigger events into the model perception lanes:
  - `perception/wave`
  - `perception/approach`
  - `perception/depart`
- At minimum, render them as markers aligned to wall time.
- Later, add physical/vision detail such as track boxes from `capture.tracks` and gesture candidate scores.

## 4. Robot antenna span semantics / detail gap

Status: fixed in the TODO #6 renderer cleanup.

Issue: robot span lanes have the same `1.0` / `0.0` inspectability problem as backend spans, and antenna cue end semantics are inconsistent across cue types.

Current Rerun lanes:

- `robot/speaker` as a span.
- `robot/antennas/thinking` as a span.
- `robot/antennas/pulse` as a span.
- `robot/antennas/ready_cue` as text logs.

Span detail cleanup:

- Apply the same explicit detail rule from issue 2 to all robot spans.
- Every `1.0` should have a matching `START` detail at the same timestamp.
- Every `0.0` should have a matching `END` detail at the same timestamp.
- Include source event type, source timestamp, cue, phase/event_phase, reason, and relevant IDs when available.

Raw antenna cue semantics from `official-live-20260625-133754`:

- Thinking cue:
  - start: `runtime.antenna_cue`, `cue=thinking`, `event_phase=started`
  - end: `runtime.antenna_cue`, `cue=thinking`, `event_phase=stopped`
  - observed count: `12 started`, `12 stopped`
  - note: intermediate `event_phase=position` rows show high/rest oscillation and should not define the outer thinking span.
- Policy pulse:
  - start: `runtime.antenna_cue`, `cue=policy_pulse`, `phase=high`
  - end: `runtime.antenna_cue`, `cue=policy_pulse`, `phase=rest`
  - observed count: `4 high`, `4 rest`
  - note: no explicit `event_phase=started/stopped` exists for this cue type.
- Ready cue:
  - start-like event: `runtime.ready_cue`, `cue=ready`, `phase=high`
  - end-like event: `runtime.ready_cue`, `cue=ready`, `phase=rest`
  - observed count: `1 high`, `1 rest`
  - current renderer logs this as text, not a span.

Model cleanup to discuss:

- Keep `robot/antennas/thinking` as a span: `event_phase=started -> event_phase=stopped`.
- Keep `robot/antennas/pulse` as a span: `phase=high -> phase=rest`.
- Decide whether `robot/antennas/ready_cue` should become a span too: `phase=high -> phase=rest`, or remain a pure startup marker. Given the raw data has a high/rest pair, span is more consistent.

## 5. Video frame timeline drift (constant-fps recording)

Status: in progress. Recording-side sidecar + renderer-side historical fallback. This is the GH
"frame alignment" gap.

Issue: video frames drift further from the event/wall timeline as the run progresses. Events stay on
accurate wall time, but the video slides increasingly out of sync — the lag grows over the run.

Root cause:

- `ArtifactRecorder._write_video_frame` sets **one fixed `fps`** on the first frame and writes every
  frame at that nominal rate, with **no per-frame timestamp**. The MKV stores nominal-fps frame
  indices, not real capture times.
- The vision loop captures frames at irregular real intervals (jitter, processing time, dropped
  frames), so nominal fps != real average fps.
- Any renderer placing frame `i` at `started_ts + i / fps` accumulates error, so the assumed video
  timeline drifts from the accurate-`ts` events, worse over time. This matches the observed symptom.
- The Reachy SDK's own `reachy_mini/utils/rerun.py:166-169` flags the same problem ("extend camera
  read() to return (frame, timestamp) tuples").

Rerun is not the problem: it aligns each frame by whatever `rr.set_time(...)` it is given. The fix is
to log each frame at a per-frame timestamp, not an fps-derived one. Current implementation uses the
runner-observed timestamp immediately after `get_latest_frame()` returns; this is not guaranteed to
be a true camera sensor timestamp.

Fix — two levels:

- Historical fallback (renderer only, no re-record): the per-frame timestamps already exist in the
  **capture JSONL** for runs with `--capture-vision` enabled. `vision_frame()` writes the capture row
  and the video frame in the same call for the same frame, so capture row `i` <-> video frame `i` for
  the overlapping prefix. Renderer:
  - `for i, frame in enumerate(decode(mkv)): rr.set_time("wall", timestamp=capture_rows[i]["ts"]); rr.log("camera/image", rr.Image(frame))`
  - Guard: if `len(capture_rows) != decoded_frame_count`, warn and timestamp the overlapping prefix
    from capture rows. Only unmatched frames use an approximate fps-derived timestamp.
- Proper fix (recording, closes GH): `_write_video_frame` also writes a per-frame timestamp
  **sidecar** `video/video-<run>-NN.jsonl` with `{frame_index: i, ts}` (mirror the audio sidecar).
  The vision loop passes one shared `frame_ts` into both capture JSONL and video sidecar writes, so
  video alignment is direct and independent of capture-vision on new runs.
- Longer-term option: if the Reachy SDK exposes true camera/sensor timestamps, carry that timestamp
  through `ReachyCameraFrameProvider.get_latest_frame()` and into the recorder instead of the
  runner-observed timestamp.

Note: once frames are logged at real `ts`, the MKV's internal fps is irrelevant — the renderer logs
decoded frames at their true times, it does not "play" the MKV at nominal rate.
