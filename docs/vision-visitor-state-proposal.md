# Vision Greet/Goodbye Trigger Proposal

**Status:** Implemented and passed captured offline evaluation; live acceptance pending
**Scope:** Vision-triggered greet and goodbye policy events
**Related work:** [`todo-official-runtime.md`](todo-official-runtime.md), item 7a

## Decision Summary

Replace the current raw bounding-box-area peak heuristic with two layers:

1. Three independent perception dimensions: presence, proximity, and motion.
2. Explicit trigger rules that combine changes in those dimensions into greet and goodbye events.

`FAR` and `APPROACHING` are not mutually exclusive states. A visitor can be present, far, and
approaching at the same time. The implementation must therefore keep the three dimensions
orthogonal rather than combining them into one visitor lifecycle state machine.

```text
RF-DETR person boxes
        |
        v
ByteTrack IDs and per-track height histories
        |
        v
robust height smoothing and classification
        |
        v
presence:  ABSENT / PRESENT
proximity: UNKNOWN / FAR / NEAR
motion:    UNKNOWN / APPROACHING / STATIONARY / RECEDING
        |
        v
greet rule + pending-goodbye confirmation rule
        |
        v
vision.approach / vision.depart
        |
        v
ReceptionPolicy greet / farewell
```

Depth estimation is not part of the first implementation. It can later supply another proximity
measurement without changing the trigger contract.

## Problem

`ApproachTracker` currently selects the largest tracked person box, measures its normalized area,
and stores the raw maximum area for the visit. It emits departure when two recent areas are no more
than `depart_factor * visit_peak`.

This makes a single oversized RF-DETR box act as the distance baseline for the rest of the visit.
Arm movement, pose changes, partial boxes, and track changes can inflate that peak. When the box
returns to its normal size, the tracker can emit goodbye even though the visitor remains in place.

The 2026-07-25 run `official-live-20260725-111932` contains the primary regression evidence:

- False sequence 1: area jumped to `0.579`, settled near `0.30`, and emitted departure while the
  visitor remained present.
- False sequence 2: area reached `0.447`, dropped to `0.251`, then remained near `0.28-0.30`; the
  same visitor waved a few seconds later.
- Genuine departure: area fell from `0.244` through `0.144` toward `0.087` before the visitor left
  the tracked scene.

The policy and direct LLM path correctly acted on the events they received. The defect is in the
vision event decision, before speech generation.

## Goals

- Prevent isolated box-size spikes from causing greet/goodbye pairs.
- Distinguish sustained approach, stationary/lateral movement, and sustained recession.
- Emit at most one greet and one goodbye for a coherent visit.
- Preserve trigger latches and stable classifications through short occlusions and reasonable
  ByteTrack ID churn.
- Use captured timestamps so behavior is stable when detector cadence varies.
- Make each classification change and trigger decision explainable from recorded inputs.
- Validate offline against recorded captures before a robot test.

## Non-Goals

- Estimating metric distance from a monocular camera.
- Solving arbitrary crowded-scene identity association in the first pass.
- Changing wave recognition, Hermes, direct policy prompts, TTS, or policy speech wording.
- Treating disappearance by itself as proof of departure. The camera has a known close-range blind
  spot, so a nearby visitor can temporarily disappear without leaving.

## Perception Dimensions

### Shared Height Measurement

For each ByteTrack ID, record:

```text
h_raw(t) = max(0, y2 - y1) / frame_height
```

Normalized box height is preferred to width or area because an upright person's apparent height is
less affected by an arm raise or lateral pose change. The existing vision capture already stores
`ts` and `[x1, y1, x2, y2]`, so historical normalized heights can be derived without changing old
artifacts.

A sample is unreliable for motion estimation when the box is clipped at the top or bottom frame
boundary, has invalid geometry, or belongs to a track without enough history. Retain such a track
for presence continuity, but do not let it update the height trend.

### Presence

Presence is independent of distance and motion:

```text
ABSENT
PRESENT
```

A credible tracked person establishes `PRESENT`, including a person represented by a clipped box.
Use a short persistence requirement to reject one-frame detections. Return to `ABSENT` only after
the existing sustained-absence reset period so short occlusions do not start a new visit.

### Proximity

Proximity is classified from robust normalized height:

```text
UNKNOWN
FAR
NEAR
```

Use separate thresholds for entering and leaving `NEAR`. The `FAR -> NEAR` threshold must be higher
than the `NEAR -> FAR` threshold so measurements near one boundary do not oscillate between the two
classes.

`UNKNOWN` is expected while the tracker lacks reliable scale evidence. A person first seen nearby
may initialize as `UNKNOWN -> NEAR`; this is not a `FAR -> NEAR` transition and must not trigger a
greeting.

A top- or bottom-clipped box can establish presence but normally cannot change an established
proximity classification. Sustained strong near-clipping may initialize `UNKNOWN -> NEAR`, but a
clipped sample must never create the event-bearing `FAR -> NEAR` transition or update the motion
trend. This protects the behind-to-front case from an oversized partial box.

### Motion

Use a small timestamped ring buffer per track. The proposed first-pass filter is:

1. A short rolling median to reject one-frame detector spikes.
2. An exponential moving average to reduce remaining frame-to-frame jitter.

EMA alone is insufficient because a large outlier still moves the filtered value and decays slowly.
All windows and persistence rules must be expressed in elapsed seconds, not frame counts. The
nominal detector cadence is about 5 Hz, but inference time can vary.

Estimate the slope of log-height over a recent window:

```text
slope = d(log(h_filtered)) / dt
```

Log-height makes the signal relative: changing from `0.20` to `0.24` is comparable to changing from
`0.40` to `0.48`. Fit the slope over approximately 0.5-1.0 seconds of valid observations rather than
using a two-sample delta.

Classify the current trend with hysteresis:

```text
slope >= approach_threshold  -> APPROACHING observation
slope <= recede_threshold    -> RECEDING observation
otherwise                    -> STATIONARY observation
```

The approach and recession thresholds may differ. A classification must persist for a configured
duration before it can contribute to a trigger. Trigger rules use recent sustained motion evidence,
not the value of one detector frame.

## Track And Visit Ownership

Height and motion histories are per ByteTrack ID; stable classifications and trigger latches belong
to the visit.

The runtime should maintain one active visitor target for the first pass. Prefer the current target
while it remains valid. Do not switch immediately just because another box is momentarily larger.
When the target ID disappears, allow a short handoff window to a spatially and geometrically
compatible person track while retaining the stable classifications and trigger latches.

This split is intentional. Earlier live tests showed that a person can receive a new tracker ID when
turning to leave. Resetting the entire visit on every ID change can lose the departure, while blindly
using the largest box each frame can combine measurements from different people.

Multi-visitor identity management remains a later extension. The first pass should behave
conservatively when target ownership is ambiguous: preserve presence, but do not emit a new greet or
goodbye from an uncertain handoff.

## Trigger Rules

`last proximity` and `current proximity` below mean consecutive stable classifications, not adjacent
raw detector frames. `Recent motion` means a sustained classification over the configured lookback
window.

### Greet

Emit one `vision.approach` when all of the following are true:

```text
presence          = PRESENT
last proximity    = FAR
current proximity = NEAR
recent motion     = APPROACHING
greet latch       = not fired
```

The visitor must therefore be observed moving from reception-far to reception-near. First-seen-near
is deliberately not a greeting condition:

```text
UNKNOWN -> NEAR = no greet
```

### Goodbye Candidate

Arm a pending goodbye, without emitting speech, when:

```text
presence          = PRESENT
last proximity    = NEAR
current proximity = FAR
recent motion     = RECEDING
goodbye latch     = not fired
```

### Goodbye Confirmation

Emit one `vision.depart` only when the pending goodbye remains supported for another confirmation
interval:

```text
pending goodbye = armed
presence         = PRESENT
proximity        = FAR
motion           = RECEDING
```

The confirmation must be duration-based, initially evaluated over approximately 0.5-1.0 seconds,
not a single additional frame. The filtered height must also continue shrinking after the
`NEAR -> FAR` crossing so a slope window containing only the earlier drop cannot confirm goodbye
after the visitor stops. Disappearance alone does not confirm goodbye in the first pass.

### Goodbye Cancellation

Cancel the pending goodbye without emitting an event when the stable observations show any of:

```text
proximity = NEAR
motion    = STATIONARY
motion    = APPROACHING
```

This is the expected outcome when a person first appears nearby from behind the robot, moves into
the front view, and then stops:

```text
UNKNOWN -> NEAR                 no greet
NEAR -> FAR + RECEDING          arm pending goodbye
FAR + STATIONARY                cancel pending goodbye
```

### Visit Reset

Keep independent greet and goodbye latches so each event fires at most once per visit. Reset both
latches, proximity history, and a pending goodbye only after sustained `ABSENT`. A pending goodbye
that loses reliable target ownership must expire without speech.

Exact slope thresholds, proximity thresholds, hysteresis gap, and persistence durations are tuning
outputs. Select them from replay results rather than assigning them from the summarized area values.
Prefer a missed event over speaking greet or goodbye from ambiguous evidence.

## Versioned Runtime Profiles

Rollout uses one runtime selector, `RECEPTION_VISITOR_TRIGGER_PROFILE`, shared by live operation and
offline replay:

- `legacy` is the safe default and preserves the original dominant-area implementation and values.
- `visitor-v1-20260802` selects this proposal's height, motion, proximity, and track-handoff logic.

Unknown names are rejected during startup. The live run manifest records the selected name,
implementation, smoothing window, and all resolved parameters. Rollback therefore does not require
a code deployment: restore `legacy` and restart the live session.

The evaluated `visitor-v1-20260802` values are:

```text
near enter / exit height:       0.71 / 0.69
proximity persistence:          0.0 s
height median / EMA alpha:      3 samples / 1.0
log-height slope window:        1.0 s
minimum slope span:             0.5 s
approach / recede slope:        +0.04 / -0.05
motion persistence:             0.0 s
goodbye confirmation:           0.2 s
additional goodbye shrink:      0.01
```

All parameters not listed above retain the complete values serialized in the named profile and run
manifest; there are no implicit environment-level threshold overrides.

## Event Contract And Observability

Keep the external event kinds unchanged:

- `vision.approach` continues to drive the greeting policy.
- `vision.depart` continues to drive the farewell policy.

Add enough derived information to capture/debug output to explain decisions:

- active tracker ID and whether a handoff occurred;
- raw and filtered normalized height;
- trend-window duration and log-height slope;
- presence classification;
- previous and current proximity classifications;
- motion classification;
- greet/goodbye latch values;
- pending-goodbye start, confirmation, cancellation, and expiry;
- classification/trigger reason and threshold values.

Classification changes and pending-goodbye decisions should be recorded even when they do not
produce a policy event. This distinguishes a correctly cancelled goodbye from a missing detector
event.

## Implementation Shape

Keep the logic independent from RF-DETR and ByteTrack where practical:

- A small per-track signal component accepts timestamped boxes and returns filtered measurements and
  motion observations.
- A proximity classifier applies stable `UNKNOWN`/`FAR`/`NEAR` classifications with hysteresis.
- A trigger evaluator combines presence, proximity transitions, recent motion, and visit latches to
  return optional `approach`/`depart` events.
- `ApproachTracker` remains the adapter that updates ByteTrack, selects or hands off the active
  visitor, and translates outputs into the existing event dictionaries.

This separation allows regression tests to feed recorded measurements directly, without loading
RF-DETR or requiring the robot.

## Validation Plan

### 1. Pure signal tests

- Constant height plus realistic jitter remains stationary.
- One oversized box does not create a receding classification after it settles.
- Sustained growth becomes approaching.
- Sustained shrinkage becomes receding.
- Clipped and invalid samples do not poison the filtered reference.
- Results remain equivalent under reasonable detector-cadence variation.

### 2. Classification And Trigger Tests

- Distant presence alone emits nothing.
- First-seen `UNKNOWN -> NEAR` emits no approach event.
- `FAR -> NEAR` with sustained approaching evidence emits exactly one approach event.
- `FAR -> NEAR` without approaching evidence emits nothing.
- `NEAR -> FAR` with receding evidence only arms a pending goodbye.
- Continued `FAR + RECEDING` confirms exactly one departure event.
- `FAR + STATIONARY`, `FAR + APPROACHING`, or a return to `NEAR` cancels the pending goodbye.
- The behind-to-front sequence emits neither approach nor departure after it settles.
- Short occlusion while a visitor is near emits no departure.
- Track-ID handoff preserves the visit without a duplicate greeting.
- A second dominant box does not immediately steal visit ownership and trigger speech.

### 3. Captured regression tests

Replay the measurements from `official-live-20260725-111932`:

- `0.579 -> ~0.30` while continuously present: no departure.
- `0.447 -> 0.251 -> ~0.28-0.30` followed by a wave: no departure.
- `0.244 -> 0.144 -> 0.087` and scene exit: one departure.

Where available, replay the complete timestamped bounding boxes rather than only these summarized
areas, because the new algorithm uses height and elapsed time.

### 4. Video replay

Run the normal perception replay over the retained walk-up, walk-away, stationary-interaction, and
wave captures. Confirm event counts and inspect classification/trigger diagnostics. No robot is
required.

### 5. Interaction regression

Feed the resulting vision events through `ReceptionPolicy`. A continuously present visitor who
walks in and waves must not produce a stacked greet/goodbye pair. This test verifies the behavioral
boundary even though the root fix belongs in perception.

### 6. Short live acceptance

Only after offline replay passes, run one capture-enabled user-present session containing:

- enter the camera view from visible FAR, approach, and stop at reception;
- appear from behind/beside the robot already NEAR, then move into the front view and stop;
- wave and converse while changing pose naturally;
- briefly leave and return to the same position;
- genuinely walk away.

Expected result: one greet for the observed FAR-to-NEAR approach; no greet or goodbye for the
first-seen-near behind-to-front entry; no goodbye during stationary interaction or pose changes;
and one goodbye during the genuine walk-away. Preserve vision JSONL and video for review.

## Rollout Criteria

The first pass is ready for live acceptance when:

- both captured false-positive sequences emit neither `vision.approach` nor `vision.depart` where
  the visitor was first seen nearby and then remained present;
- the captured genuine walk-away emits one `vision.depart`;
- existing clean walk-up clips still emit one `vision.approach`;
- first-seen-near, stationary jitter, clipping, and ID-handoff tests emit no contradictory events;
- replay diagnostics explain every classification change and trigger decision from recorded values;
- the interaction-level policy regression passes.

After live acceptance, record the selected windows and thresholds in this document and in runtime
configuration. Evaluate monocular depth only if height-based replay leaves specific, captured cases
that the additional signal can separate.

## Captured Evaluation Result

The `visitor-v1-20260802` profile passed the nine human-reviewed clips from the 2026-06-25 and
2026-07-25 sessions: both genuine goodbye/greet sequences emitted `depart -> approach`, and all
seven negative or wave-only clips emitted neither event. It also replayed each full decodable
session with exactly one `depart -> approach` sequence and no extra triggers. The June MKV retained
its known premature-end decode warning; evaluation covered its complete decodable prefix.

These recordings are a narrow evaluation set, so passing them promotes the profile only to live
acceptance, not directly to the default. The `legacy` profile remains the runtime default until a
successful controlled live session is reviewed.
