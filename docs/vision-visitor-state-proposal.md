# Vision Greet/Goodbye Trigger Proposal

> The planned frame-broker runtime preserves the same-frame RF-DETR/DINO joins required by this
> policy while allowing MediaPipe and recording to run at a higher cadence. See
> [`vision-frame-broker-architecture.md`](vision-frame-broker-architecture.md).

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

## Dynamic Door Policy Follow-Up

The door observation layer supplies two inputs to a versioned policy profile:

- a categorical door state: `STABLE`, `MOVING`, or `UNKNOWN`;
- a retained logical door-region box and timestamp-aligned spatial measurements against person
  boxes.

`STABLE` means the observed door is not moving, regardless of whether it is open or closed.
`UNKNOWN` means the observer cannot make a reliable motion decision because localization is
unavailable or stale, or the door is not sufficiently observable.

Grounding DINO provides periodic semantic localization. Raw detections can split between the door
leaf and doorway while the door moves, so the highest-confidence box and ByteTrack ID are not a
stable door identity. The observer instead associates compatible detections and retains one logical
door-region box through short gaps. YOLO-World remains an offline comparison signal until it
matches Grounding DINO's open-door coverage.

Door movement uses detection-box geometry change by default. Relative door-leaf motion remains an
optional additive fallback, disabled by default because it adds about 13 ms per processed frame and
missed motion that geometry detected in the initial two-clip evaluation. When enabled, it tracks
sparse image features inside the retained door box and in a surrounding background ring, masks
current and previous person boxes, rejects inconsistent tracks with forward/backward checks and
RANSAC, and compares the fitted door transform with the fitted background transform. Valid relative
evidence can raise the combined motion score but cannot suppress geometry evidence. When disabled,
the observer skips feature detection, optical flow, and affine fitting entirely. The full-frame
changed pixel fraction remains recorded as `global_frame_change_score` for diagnosis only and does
not affect classification.

Offline review generation defaults to `--geometry-only`. Pass `--relative-motion` to evaluate the
fallback and populate its diagnostic lanes.

An optional robust sequential-change mode replaces the absolute `MOVING` entry threshold with a
session-local one-sided CUSUM. It learns the median and robust noise scale of accepted DINO motion
scores over a rolling window, converts each new score to a normalized deviation, and accumulates
only sustained upward evidence. Each update is capped and at least two accepted DINO updates are
required, so a single detection spike cannot enter `MOVING`. Baseline updates are limited to
unoccluded, no-person observations; person-present observations are evaluated against the frozen
baseline. Held geometry values between DINO completions are not counted as new evidence. The
existing absolute exit threshold and `0.8 s` dwell still control `MOVING -> STABLE`.

Sequential mode is enabled in the `door-v3-20260825` live-test candidate. `door-v2-20260809`
retains absolute-threshold entry as the immediate rollback. Offline review can enable sequential
entry with `--sequential-change`; `--single-threshold` retains the earlier behavior. The recorded-
trace evaluator is `scripts/evaluate_door_sequential_change.py`.

For each logical person track, record the fraction of the person box intersecting the retained door
box and the normalized distance from the person's feet anchor to the nearest door-box edge. The
policy uses those measurements with hysteresis: an interaction enters when distance is at most
`0.06` or overlap is at least `0.10`, and exits only when distance exceeds `0.08` and overlap falls
below `0.05`. Both measurements must be available before the interaction can participate in a
trigger.

### Door-Ordered Trigger Contract

The `door-v1-20260805` profile leaves the existing height-based profile available for rollback and
uses opposite evidence ordering to distinguish arrival from departure:

1. **Greet:** a `STABLE -> MOVING` door edge with no observed or recently retained person arms a
   greet candidate. A later person interaction entering the threshold emits one `vision.approach`.
2. **Goodbye:** a person interaction entering the threshold arms a goodbye candidate. A later
   `STABLE/UNKNOWN -> MOVING` door edge emits one `vision.depart`.

The interaction crossing alone never emits goodbye. Door movement and a newly observed interaction
on the same source frame are treated as ambiguous and emit neither event. A person remains retained
for `0.75 s` through ordinary detector gaps, including a clipped but credible person box. Both
greet candidates expire after `4.0 s`. A goodbye candidate remains supported while at least one
interaction stays inside the exit hysteresis (`distance <= 0.08` or `overlap >= 0.05`); its `4.0 s`
expiry starts only after that support is lost. Goodbye does not require a prior greeting or
conversation.

### Close-Person Door Reliability and Interaction Eligibility

Run `official-live-20260807-110807` added two labeled false-positive regressions while the door
remained closed:

- frame `155` / video `00:31.0`: false `vision.approach` and greet;
- frame `195` / video `00:39.0`: false `vision.depart` and goodbye.

The person was moving close to the camera and occluding the door during this sequence. The fix has
two independent layers, with all thresholds captured in the versioned `door-v2-20260809` profile.
The unchanged `door-v1-20260805` profile remains the immediate behavior rollback:

1. **Door-observation reliability:** while a raw person detection is oversized or frame-clipped,
   reject semantic door updates and report the door state as `UNKNOWN`. Also reject an almost fully
   nested door candidate that abruptly shrinks or grows while the trusted door is stable. After the
   retained box goes stale, the first credible door detection establishes a fresh baseline instead
   of creating a false motion edge against stale geometry.
2. **Policy interaction eligibility:** an oversized or frame-clipped logical person box still
   establishes or retains `PRESENT`, but cannot arm or sustain goodbye or complete an armed greet.
   Rejection reasons and track IDs remain in the frame trace and Rerun diagnostics.

Offline acceptance on 2026-08-09 removed both false events from frames `130-220` of
`official-live-20260807-110807`. The accepted real-door replay at frames `40-110` of
`official-live-20260804-144621` retained depart at frame `63` and approach at frame `86`; frames
`600-690` of `official-live-20260804-145713` retained departs at frames `619` and `670`.

Grounding DINO runs continuously as an asynchronous policy-role pipeline at up to `2 Hz`; RF-DETR
person perception remains on the camera path. DINO output is fused with the person observation and
image from the DINO source frame, not the frame current when inference completes. The first M1 Max
benchmark used the 2026-08-04 14:46:21 clip, frames 40-110:

| DINO shortest edge | Detected samples | Median inference | Maximum inference |
| --- | ---: | ---: | ---: |
| model default | 45/45 | 546.6 ms | 564.1 ms |
| 640 px | 45/45 | 334.1 ms | 393.3 ms |
| 480 px | 45/45 | 226.5 ms | 231.0 ms |

The 480 px setting preserves detection coverage and the retained door geometry on this source while
leaving input size configurable. It is the first policy-profile default, subject to the captured
acceptance set below.

The earlier `TrackedPolygonZone` implementation remains available for fixed, manually calibrated
doorway occupancy experiments. The dynamic door observer does not require that polygon and must not
silently substitute polygon occupancy for semantic door localization.

### Offline Door Policy Review Contract

Manual acceptance uses one spatial view plus focused observation and policy timelines. All timeline panels link their
X axes to the global frame timeline so an independently retained Rerun zoom cannot make populated
series appear empty. The generator embeds the blueprint as both active and default; merely storing
it as the default allows an older active layout for the same Rerun application to override the
artifact's intended panels and time ranges.

1. The spatial view overlays thin raw Grounding DINO boxes, a thick retained door box, person boxes,
   logical person IDs, and the current door state on the source frame.
2. **Door State** shows `STABLE`, `MOVING`, or `UNKNOWN` as a categorical lane.
3. **Combined Door Motion** shows the combined score and enter/exit hysteresis thresholds.
4. **Door Geometry Score** shows detection-box geometry change independently.
5. **Relative Door-Leaf Motion** shows background-relative feature motion independently.
6. **Sequential Change Evidence** shows the learned baseline and noise scale, normalized current
   score, accumulated CUSUM evidence, decision limit, and baseline-ready state.
7. **Relative-Flow Quality** shows whether relative motion is valid plus door inlier ratio, door
   feature coverage, and background inlier ratio. Detailed point counts and normalized relative
   displacement remain available in `frames.jsonl`.
8. **Door Box Geometry** shows normalized retained-box center X, center Y, width, and height. A gap
   means there is no valid retained box.
9. **Person-Door Overlap** shows `intersection(person_box, door_box) / person_box_area` as one series
   per logical person track plus an always-present maximum-overlap series. The aggregate is zero
   when no person is observed; track-specific series end when that track disappears.
10. **Person-Door Distance** shows feet-anchor distance to the nearest door-box edge, normalized by
   frame diagonal, as one series per logical person track. The distance is zero when the anchor is
   inside the box.
11. **Observed and Retained Presence** distinguishes current RF-DETR evidence from the short policy
    retention window.
12. **Policy Candidate** records idle, greet-armed, or goodbye-armed state, and **Policy Trigger**
    records `approach` and `depart` decisions.
13. **DINO Latency** records inference latency and source-frame age. **Source-to-Decision Latency**
    records when the asynchronous policy decision became available relative to its source frame.

### Sequential-Change Regression Set

The first full-trace regression on 2026-08-25 used the default experimental sequential settings:

- `official-live-20260806-114813`: 8,361 accepted semantic updates and zero change entries;
- `official-live-20260807-063649`: sequential entry detects the real door opening at frame `7159`
  and arms greet. RF-DETR first reports the entering visitor after the last successful DINO result;
  the historical DINO label-count failure then stops policy evaluation, so this recording validates
  door-state detection but cannot validate the final greet event. Commit `21ad327` fixed that worker
  failure before `door-v3-20260825` promotion;
- `official-live-20260825-124601`: the missed-greet window enters `MOVING` at frame `14081`, before
  first person presence at frame `14085` and before the old absolute threshold crossing at frame
  `14089`; the existing policy emits one `approach` at frame `14085`.
- `official-live-20260825-145234`: the clinic visit enters `MOVING` at frame `4829`, arms greet, and
  emits `approach` when the visitor appears at frame `4831`. The departure interaction arms goodbye
  before a second sequential `MOVING` edge emits `depart` at frame `5009`. The absolute-threshold
  profile emitted neither event.

The source trace hashes, parameters, and all detected change episodes are retained in
`artifacts/door-sequential-eval/regression-report-v2.json`. This result accepts the method for
promotion to the versioned `door-v3-20260825` live-test candidate; `door-v2-20260809` remains the
rollback until controlled live acceptance.

The first trigger acceptance set consists of two human-approved positive sequences and one negative:

- `official-live-20260804-144621`, frames 40-110: exactly `depart -> approach`;
- `official-live-20260804-145713`, frames 600-690: exactly two `depart` events and no
  `approach`; the operator intentionally approaches and opens/closes the door twice without leaving,
  and both door-opening sequences are intended goodbye triggers;
- `official-live-20260625-133754--trigger-02`: no greet or goodbye event.

Each Rerun artifact must make the evidence order reviewable from source frames. Door-motion to policy
decision latency must be at most `1.0 s`. During a future controlled live acceptance, the policy DINO
pipeline must not starve RF-DETR for more than `1.0 s` or reduce its effective cadence by more than
20 percent. Generate and review the acceptance artifacts one at a time; stop if an expected sequence fails
before proceeding to the next artifact.

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
