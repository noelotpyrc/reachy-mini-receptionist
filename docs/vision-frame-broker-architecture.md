# Vision Frame-Broker Architecture

## Status

Implemented, deployed, and accepted as the production runtime architecture for raising MediaPipe
and diagnostic recording to a real 15 FPS without forcing RF-DETR or Grounding DINO to run at that
cadence. Live runs verified advancing broker/media counters, finalized artifacts, and healthy audio
under the added workers. `serial-v1` remains an explicit next-run rollback.

## Problem

The current async vision task is internally serial:

1. Retrieve the latest camera frame.
2. Run RF-DETR and ByteTrack.
3. Run MediaPipe when gestures are enabled.
4. Write capture metadata and video.
5. Submit diagnosis and door work.
6. Sleep for `vision_interval`.

With `vision_interval=0.2`, recent sidecars show about `3.1-3.2` distinct frames per second rather
than 5 FPS. MediaPipe therefore receives too few close-wave samples for reliable temporal hand
motion, and the fixed-5-FPS MKV header does not reflect source time.

The runtime needs independent execution cadences while preserving the dependencies among RF-DETR,
ByteTrack, Grounding DINO, door geometry, and policy generation.

## Goals

- Produce one canonical, timestamped frame stream at a configured 15 FPS.
- Give MediaPipe and raw recording every canonical frame.
- Let policy vision and DINO select lower-rate subsets without stale queues.
- Preserve same-source-frame person/door fusion and ordered temporal state.
- Persist enough identity and selection data for exact-selection offline replay.
- Keep recording, inference, Rerun, and policy work from blocking camera acquisition or audio.
- Provide a one-setting rollback to the current serial runtime.

## Non-Goals

- Changing RF-DETR, DINO, MediaPipe, door, visitor, or policy thresholds.
- Running every model at 15 FPS.
- Guaranteeing bit-identical model output from lossy MKV replay.
- Switching vision architectures inside an active session.
- Removing `serial-v1` during the first broker release.

## Runtime Modes

`RECEPTION_VISION_RUNTIME` and the matching CLI option select one complete execution path:

| Mode | Behavior |
|---|---|
| `serial-v1` | Existing `_vision_loop` behavior, unchanged; explicit rollback. |
| `broker-v1` | Accepted production default: canonical capture producer plus per-consumer subscriptions described here. |

Every run manifest records the mode, configured cadences, queue capacities, and effective consumer
counters. A mode change requires a clean stop and a new run ID.

## Canonical Frame Contract

The camera producer is the only owner that calls `mini.media.get_frame()`. At each capture deadline
it publishes an immutable-by-convention packet:

```text
FramePacket
  source_frame_id: monotonically increasing integer
  source_frame_ts: runner-observed wall timestamp
  frame_bgr: 720p BGR pixels
```

The SDK currently exposes pixels without a camera/sensor timestamp. `source_frame_ts` therefore has
the same runner-observed provenance as the existing video sidecar and must not be represented as a
sensor timestamp.

The broker is fan-out publication, not a competing-consumer work queue. Publishing frame `N` puts a
reference to the same immutable packet in each subscriber's own inbox. MediaPipe removing `N` from
its inbox does not remove `N` from the recorder or policy inbox.

## Data Flow

```text
Reachy camera/WebRTC (nominal source up to 30 FPS)
                         |
                         v
              canonical sampler: 15 FPS
                         |
                         v
     FramePacket(source_frame_id, source_frame_ts, pixels)
                         |
       +-----------------+------------------+-----------------+
       |                 |                  |                 |
       v                 v                  v                 v
 recorder FIFO     MediaPipe FIFO      policy latest      Rerun latest
   all frames        all frames           selector           selector
       |                 |                  |                 |
       v                 v                  v                 v
 15 FPS MKV       ordered hand       RF-DETR/ByteTrack   image display
 + sidecar         observations      person observation
                         |                  |
                         v                  +--------------------+
                    wave event                                  |
                                                               v
                                                    DINO subset selector
                                                               |
                                                               v
                                                        door detection
                                                               |
                                      same-frame join <--------+
                                                               |
                                                               v
                                              door state + greet/goodbye
                         |                                     |
                         +------------------+------------------+
                                            v
                                 serialized policy-event queue
                                            |
                                            v
                                  ReceptionPolicy + capabilities
```

## Dependency Rules

### Gesture Path

MediaPipe depends only on ordered canonical frames and source timestamps. One worker owns the
recognizer and temporal wave state. Calls are never parallelized because out-of-order completion
would corrupt the temporal history. A wave event retains its source frame ID and timestamp and is
bridged to the main asyncio policy loop.

`broker-v1` disables inline gesture inference in `PerceptionPipeline`; exactly one gesture owner may
emit wave events.

### Person and Tracking Path

RF-DETR selects the newest canonical frame after each policy cycle. ByteTrack and visitor-state
updates run immediately after that RF result on the same ordered worker. Frames may be skipped before
inference, but selected frames are never reordered and completed work is never queued behind stale
frames.

The first broker pass keeps the policy loop's configurable post-processing idle interval. Reducing
that interval may raise effective RF cadence, but no FPS is inferred from the idle value. Selected,
completed, skipped, and effective rates are measured.

### DINO and Door Fusion

DINO receives only frames selected by the policy path, at its existing configured ceiling. A policy
frame is buffered with its matching RF person observation before it is eligible for DINO submission.

When DINO result `N` completes, `LiveDoorPolicyCoordinator` joins it only to canonical frame `N` and
the RF/ByteTrack person observation from `N`. Ordered policy frames between the previous semantic
result and `N` remain available for door geometry. This preserves current door-policy semantics.

RF-DETR and DINO remain mutually exclusive under the existing MPS inference guard. Their threads may
be runnable concurrently, but model calls are serialized. MediaPipe resource contention is measured
separately because its MediaPipe/Metal runtime does not currently use that guard.

### Policy Events

Worker threads do not invoke `ReceptionPolicy` or capabilities directly. Wave, greet, and goodbye
events enter one main-loop queue and are handled serially. Each event records source time, decision
time, and arrival order. The runtime does not delay prompt events to reorder them by source time.

### Recording and Capture Metadata

Raw video persistence is separated from derived vision capture. The recorder writes every canonical
frame and a sidecar row containing video index, source frame ID, and source timestamp. RF/person,
gesture, DINO, and policy observations are written independently at their own cadences and reference
the canonical source frame.

This avoids attaching stale person or policy results to high-rate video frames. Existing artifact
paths remain unchanged, and new fields are additive so current review tools can continue to read the
files.

## Queue Policy

| Subscriber | Inbox | Overflow behavior | Acceptance |
|---|---|---|---|
| Recorder | Bounded ordered FIFO | Do not block capture; report exact dropped IDs and mark video incomplete. | Zero drops for gesture evaluation. |
| MediaPipe | Small ordered FIFO | Drop oldest unprocessed packet, keep newest, and report the gap. | Zero drops at configured 15 FPS. |
| Policy/RF | Latest-frame slot | Replacement is intentional sampling; record selected IDs and counters. | No stale backlog; effective rate reported. |
| DINO | Existing latest-frame queue | Replace stale pending work and report drops. | Stay within configured ceiling. |
| Rerun image | Latest-frame slot | Replace stale image and report counters. | Must not affect production paths. |
| Policy event | Ordered main-loop queue | Bounded health fault; never run capabilities concurrently. | Zero event drops. |

Queue capacities are configuration, not hidden constants. Publication is non-blocking. For recording,
overflow is an artifact-integrity failure even though the robot session may continue to fail-stop and
finalize according to the media-liveness policy.

## Offline Replay

The 15 FPS video is the durable superset of model inputs. Each consumer result records the source
frame IDs it selected. Replay supports two meanings:

- **Exact-selection replay:** process the same frame IDs and source timestamps selected live.
- **Counterfactual replay:** select a different cadence from the canonical recording, such as
  MediaPipe at 3.3, 5, 10, 15, or 30 FPS.

Lossy `mp4v` pixels are suitable for practical diagnosis but are not bit-identical to the raw arrays
used live. A future strict pixel-reproduction mode would require lossless or selectively retained raw
frames and is outside this first pass.

## Health and Diagnostics

Each consumer reports at least:

```text
published_frames
selected_frames
completed_frames
dropped_frames
last_source_frame_id
last_source_frame_ts
last_completed_ts
effective_fps
queue_depth and queue_capacity, where applicable
inference or write latency distribution
```

The run manifest records final counters. Runtime heartbeat may expose compact ages and drop totals,
but diagnostics must not turn optional Rerun streaming into a liveness dependency.

## Rollout and Rollback

Implementation is additive:

1. Add broker primitives and deterministic tests without changing `serial-v1`.
2. Add `broker-v1` behind the explicit runtime selector.
3. Split raw video and derived capture APIs while retaining the serial wrapper.
4. Add consumer selection and health artifacts.
5. Replay recorded sources and run the existing door-policy regression set.
6. Benchmark 15 FPS capture, recording, and MediaPipe on m1max with RF/DINO enabled.
7. Run a short controlled live wave and door acceptance.
8. Promote `broker-v1` only after acceptance; retain `serial-v1` as an explicit rollback. **Complete.**

Rollback normally means stopping the current run and starting a new run with:

```text
RECEPTION_VISION_RUNTIME=serial-v1
```

The previous frozen m1max release remains the second rollback level. Automatic mid-session fallback
is prohibited because it would reset temporal model state, risk duplicate policy events, and mix
frame namespaces in one artifact set.

## Acceptance Gates

- OPS preserves explicit runtime selection: production selects `broker-v1`, while `serial-v1`
  remains the tested next-run rollback.
- Broker unit tests prove fan-out delivery, per-subscriber removal, ordering, replacement, overflow,
  and clean close behavior.
- A 15 FPS source recording has zero recorder drops and a sidecar row for every decoded frame.
- MediaPipe completes the configured 15 FPS stream with zero drops in the evaluation run.
- RF/DINO frame joins always use identical source frame IDs.
- Existing labelled door clips preserve accepted greet/goodbye outcomes.
- Policy events are serialized with zero drops and no duplicate wave owner.
- Audio/media liveness remains healthy under the added workers.
- One clean restart in `serial-v1` restores current behavior without a code checkout.
