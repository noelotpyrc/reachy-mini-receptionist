# Rerun Vision Tracking Feature Plan

**Status:** Offline replay and configurable real-time tracking display implemented and exercised;
live Rerun streaming remains optional and off for the first production acceptance run

**Scope:** Add detailed vision-tracking review to Rerun in two stages:

1. Offline replay with per-frame tracking views.
2. Optional real-time tracking display during a live session.

This plan extends the existing [Rerun integration](rerun-integration.md) and the
[visitor trigger proposal](vision-visitor-state-proposal.md). It does not change greet/goodbye
rules by itself.

## Motivation

Current artifacts make it possible to identify when `vision.approach`, `vision.depart`, and
`vision.wave` occurred, but they do not provide one complete visual explanation of how each frame
became a trigger decision. Reviewing the detector, tracker, motion classifier, proximity classifier,
and doorway-zone classifier currently requires combining video, capture JSONL, replay console
output, and annotated videos.

Rerun should become the shared diagnosis surface for both recorded replay and optional live
observation. A reviewer should be able to select any frame and inspect the source image, boxes,
track ownership, doorway geometry, derived signals, pending decisions, and emitted events on one
timeline.

## Current Baseline

The repository now has an implemented offline path and an unimplemented live path:

- `reception-vision-replay` decodes a video and reruns RF-DETR, ByteTrack, and the selected visitor
  trigger profile. It persists versioned per-frame observations and can write Rerun tracking
  entities, a replay manifest, and an annotated video.
- `official_runtime.rerun_review` loads a recorded run, aligns video frames to recorded timestamps,
  and renders timeline spans and perception-event markers. It does not currently overlay track
  boxes or render per-frame classifier state.

The doorway-zone module is available in offline replay. Closed-door semantic localization has also
been compared offline with YOLO-World and Grounding DINO. No configurable live detector scheduler
or live Rerun sink exists yet.

## Design Principles

- **One observation contract:** offline replay and live display must render the same structured
  vision observation rather than maintain separate visualization logic.
- **Detection is configured, not hard-coded:** a run selects one or more named detector pipelines.
  Adding a target or model must not require a new live-loop branch or a model-specific Rerun view.
- **Detection, tracking, and policy are separate:** each detector has an optional tracker, while
  policy consumes only pipelines explicitly assigned a policy role. Diagnosis-only door pipelines
  cannot emit greet/goodbye actions.
- **Recorded and recomputed data stay distinct:** historical live outputs and candidate replay
  outputs use separate namespaces and carry source metadata.
- **Diagnosis never controls policy:** Rerun consumes observations after inference. It cannot emit
  robot actions or change trigger decisions.
- **Live display is optional and non-blocking:** viewer latency, disconnection, or backpressure must
  not delay detection, policy handling, recording, or robot shutdown.
- **Artifacts remain authoritative:** live streaming is disposable. Raw video, capture rows, run
  manifests, and replay traces are the durable evidence.
- **No hidden calibration:** doorway polygon, anchor choice, dwell values, visitor profile, detector
  threshold, and frame timestamp source must be visible in replay metadata.

## Shared Vision Observation

The existing serializable `VisionObservation` remains the visitor-policy observation. Add a generic
`DetectionLayerObservation` for independently scheduled detector and tracker results:

```text
schema_version
run_id
pipeline_id
frame_index
frame_ts
completed_ts
inference_latency_ms
scheduler_wait_ms
detector_config
tracker_config
detections[]
  detection_index
  class_id / class_name
  confidence
  box_xyxy
tracks[]
  track_id
  source_detection_index
  box_xyxy
  confidence
```

The live frame envelope contains the image, frame index, and capture timestamp. Detector layers
refer back to that envelope by frame index and timestamp. The existing visitor observation remains
produced once per processed frame:

```text
schema_version
mode                    recorded | replay | live
run_id
frame_index
frame_ts
timestamp_source
frame_width / frame_height
detector_config
visitor_trigger_profile
doorway_zone_config

detections[]
  detection_index
  class_id / class_name
  confidence
  box_xyxy
  normalized_center
  normalized_bottom_center

tracks[]
  source_track_id
  logical_track_id
  tracking_source
  box_xyxy
  normalized_center
  normalized_bottom_center
  previous_anchor
  image_displacement
  image_velocity_per_s
  track_age_s
  visible_sample_count
  area
  raw_height
  filtered_height
  clipped / reliable
  log_height_slope
  motion
  active_target
  handoff_from_track_id
  doorway_raw_occupancy
  doorway_occupancy
  doorway_candidate
  doorway_candidate_elapsed_s

scene
  people
  presence
  proximity
  motion
  greet_fired
  goodbye_fired
  goodbye_pending
  trigger_decision

events[]
```

Images are not duplicated inside JSONL. Offline and live detector layers refer to the source frame
index; live frames carry image data only through bounded in-memory queues and rely on the existing
video artifact for durable storage.

Raw detections and tracked boxes are intentionally separate. A raw RF-DETR person detection exists
before ByteTrack assigns or preserves an identity. The trace must make it possible to review
detector misses and box changes independently from tracker ID changes and logical visitor handoffs.

Image displacement and velocity describe movement across the image plane only. They are useful for
reviewing track continuity and lateral movement, but they are not physical room velocity and must
not be presented as equivalent to the log-height `APPROACHING`/`RECEDING` classifier. The renderer
derives short track trails deterministically from timestamped anchor observations and a recorded
trail-window setting.

The first schema should be additive and versioned. Unknown fields must be ignored by readers so new
diagnostics can be added without invalidating older replay traces.

## Shared Rerun Renderer

Create a `RerunVisionRenderer` that accepts `VisionObservation` plus an optional image. It owns Rerun
entity naming and archetype conversion, but it does not run detection or modify state.

Proposed entity layout:

```text
recorded/
  perception/approach
  perception/depart
  perception/wave

replay/ or live/
  camera
  detectors/<pipeline_id>/detections
  trackers/<pipeline_id>/tracks
  trackers/<pipeline_id>/paths/<track_id>
  diagnostics/inference_latency_ms/<pipeline_id>
  diagnostics/scheduler_wait_ms/<pipeline_id>
  diagnostics/result_age_ms/<pipeline_id>
  diagnostics/submitted_frames/<pipeline_id>
  diagnostics/completed_frames/<pipeline_id>
  diagnostics/dropped_frames/<pipeline_id>
  visitor/camera/detections
  visitor/camera/tracks
  camera/track_anchors
  camera/track_paths/<logical_track_id>
  camera/track_velocity/<logical_track_id>
  camera/active_target
  camera/doorway
  camera/doorway_anchors
  signals/people
  signals/height/<logical_track_id>
  signals/log_height_slope/<logical_track_id>
  states/presence
  states/proximity
  states/motion
  states/doorway_occupancy/<logical_track_id>
  states/doorway_candidate/<logical_track_id>
  decisions/greet
  decisions/goodbye
  diagnostics/handoff
  diagnostics/dropped_frames
```

Use native Rerun types:

- `EncodedImage` for JPEG-compressed live or replay frames;
- distinct `Boxes2D` entities for raw detections and tracked boxes with ID labels;
- `LineStrips2D` for the doorway polygon and recent per-track movement paths;
- `Points2D` for current track and doorway anchors;
- arrows or line segments for image-plane movement direction;
- scalar/time-series entities for height and slope;
- text or discrete state entities for classifications, candidates, handoffs, and decisions.

Provide a default blueprint with one large spatial image view, compact state/timeline views, and a
selection panel suitable for frame-by-frame inspection. The entity contract must remain usable
without the blueprint so saved `.rrd` files are not coupled to one layout.

## Feature 1: Offline Replay With Tracking Views

### Inputs

- Source MKV/MP4.
- Video sidecar timestamps when available.
- Capture timestamps as the historical fallback.
- Detector threshold and smoothing configuration.
- Versioned visitor trigger profile.
- Optional doorway-zone configuration.

The first pass runs one candidate configuration at a time. Multi-profile side-by-side replay is a
later optimization; separate replay outputs are sufficient for initial comparison.

### CLI

Extend `reception-vision-replay` rather than create a second inference command:

```bash
reception-vision-replay path/to/video.mkv \
  --visitor-trigger-profile visitor-v1-20260802 \
  --doorway-zone-config path/to/doorway-zone.json \
  --trace-jsonl artifacts/vision-replay/<replay-id>/frames.jsonl \
  --save-rrd artifacts/vision-replay/<replay-id>/review.rrd \
  --spawn-rerun
```

Keep `--annotate` as a portable fallback, but make the structured trace and Rerun entities the main
diagnosis outputs.

### Outputs

```text
artifacts/vision-replay/<replay-id>/
  replay-manifest.json
  frames.jsonl
  review.rrd
  events.jsonl
  annotated.mp4            optional
```

The replay manifest records source hashes, timestamp source, frame counts, detector configuration,
resolved visitor profile, resolved doorway-zone configuration, output paths, and completion status.

### Offline Review Workflow

1. Review raw person detections, tracked boxes, IDs, and movement trails without doorway or trigger
   interpretation.
2. Select a representative frame and define the normalized doorway polygon.
3. Render the polygon and anchor points without trigger integration.
4. Review whether occupancy agrees with the room geometry across the full clip.
5. Run the selected visitor profile and doorway classifier over the video once.
6. Open the `.rrd`, scrub each labeled event and negative clip, and inspect transitions.
7. Preserve the replay manifest and trace with the evaluation result.

Door calibration is a human-reviewed input. Interactive polygon drawing may be added later, but it
is not required for the first replay implementation.

### Offline Acceptance

- Every decoded frame has one deterministic timestamp and observation row.
- Every RF-DETR person detection includes its box and confidence before tracking.
- Every visible ByteTrack result includes source ID, logical ID, current anchor, and timestamped
  image-plane movement data.
- Track trails make ID continuity, ID churn, and accepted handoffs visually distinguishable.
- Boxes, labels, doorway polygon, and anchors align with the displayed frame.
- Rerun selection exposes complete track and state details for that timestamp.
- Event counts match the replay CLI summary.
- Replaying the same inputs and configuration produces equivalent observation rows.
- Recorded historical events are visually distinct from candidate replay decisions.
- A missing optional Rerun dependency does not prevent JSONL replay output.

## Feature 2: Configurable Real-Time Detection And Tracking

### Pipeline Configuration

Live detection uses a versioned JSON configuration selected by `--vision-pipelines-config`. The
configuration is safe to commit and contains no clinic profile data or credentials.

```json
{
  "schema_version": 1,
  "pipelines": [
    {
      "id": "door_yolo_world",
      "detector": "yolo-world",
      "model": "yolov8s-worldv2.pt",
      "targets": ["door", "doorway", "entrance door"],
      "threshold": 0.10,
      "inference_fps": 1.0,
      "tracker": "bytetrack",
      "role": "diagnosis"
    },
    {
      "id": "door_grounding_dino",
      "detector": "grounding-dino",
      "model": "IDEA-Research/grounding-dino-tiny",
      "targets": ["door", "doorway", "entrance door"],
      "threshold": 0.30,
      "text_threshold": 0.15,
      "inference_fps": 1.0,
      "tracker": "bytetrack",
      "role": "diagnosis"
    }
  ]
}
```

Pipeline IDs are unique and become stable artifact and Rerun namespaces. Supported first-pass
detectors are `yolo-world` and `grounding-dino`; supported tracker values are `none` and
`bytetrack`. The registry and observation contract allow later detector adapters without changing
the camera loop. Unsupported detectors or missing optional dependencies fail validation before the
robot starts.

The first moving-door profile runs both models on the same one-FPS frame selection. Raw video stays
at five FPS so both models can also be evaluated on every frame offline. Pipeline cadence is
independent, so a later profile may run YOLO-World at five FPS and Grounding DINO less frequently.

### Runtime Shape

Add a frame broker after camera capture. The existing visitor-policy pipeline and each configured
diagnosis pipeline consume the frame independently:

```text
camera frame broker
       |
       +--> visitor detector/tracker --> policy/event path
       |
       +--> existing artifact recorder
       |
       +--> bounded detector queues --> detector/tracker workers
       |                                  |
       |                                  +--> detector JSONL
       |                                  +--> Rerun event queue
       |
       +--> bounded Rerun event queue --> Rerun worker --> gRPC viewer
                                                    |
                                                    +--> optional .rrd file sink
```

Every detector queue holds at most one pending frame. Submission is non-blocking; a newer frame
replaces an unprocessed frame and increments that pipeline's dropped-frame counter. This prevents a
slow model such as Grounding DINO from accumulating stale work. A detector result keeps the source
frame timestamp and is logged to Rerun on that original timeline point even when inference finishes
later. The Rerun queue is separately bounded and uses the same replace-oldest rule for camera-frame
events. No detector or Rerun worker may delay camera recording, policy handling, or shutdown.

Detector workers publish only structured results. Models configured for Apple MPS share one
process-wide inference lock because concurrent command-buffer use is not reliable across these
frameworks. Queues and cadence remain independent, and scheduler wait is recorded separately from
model inference latency. The visitor-policy detector uses the same lock; therefore a diagnosis
model can add bounded contention on a single accelerator even though it cannot create an unbounded
frame backlog. The moving-door collection may disable visitor policy inference when isolation is
required. A single Rerun worker owns all SDK calls so model threads never call Rerun concurrently.
The renderer overlays all detector and tracker namespaces in one spatial view and lets the reviewer
toggle each pipeline independently.

### Configuration

Implemented runtime controls:

```text
RECEPTION_RERUN_MODE=off | grpc | file | grpc+file
RECEPTION_RERUN_GRPC_URL=rerun+http://<viewer-host>:9876/proxy
RECEPTION_RERUN_IMAGE_FPS=5
RECEPTION_RERUN_JPEG_QUALITY=80
RECEPTION_RERUN_QUEUE_SIZE=3
RECEPTION_VISION_PIPELINES_CONFIG=path/to/vision-pipelines.json
```

Equivalent explicit live CLI flags exist, and OPS records the resolved configuration and its SHA-256
in the run manifest. `off` and no additional pipelines remain the production defaults. If live
Rerun or a configured detector dependency is missing, fail configuration validation before robot
startup. After startup, viewer disconnection or a diagnosis-pipeline failure is recorded and must
not fail the live session.

The committed two-model diagnosis profile is
`config/vision/door-live-compare-v1.json`. Before using the robot, replay a recorded video through
the exact configured adapters with `scripts/preflight_live_detection.py`; pass `--grpc-url` to
inspect the same run in a live viewer while retaining the `.rrd` file.

Detector results are persisted independently of Rerun:

```text
artifacts/official-runtime-live/detections/detections-<run-id>-01.jsonl
```

Each row contains the pipeline configuration, source frame identity, detections, optional tracks,
inference latency, completion time, and drop counters. This JSONL plus raw MKV and its timestamp
sidecar remains sufficient for offline diagnosis if the viewer disconnects.

### Remote Viewer

The preferred setup is the native Rerun viewer on the review machine and a gRPC sink on M1Max over
Tailscale. Bind the viewer only to the intended local/Tailscale interface rather than exposing the
default port publicly.

Rerun supports simultaneous `GrpcSink` and `FileSink` output. Use the file sink only when an `.rrd`
is explicitly requested; the normal raw video/capture artifacts continue independently.

The browser viewer is acceptable for short convenience sessions, but the native viewer is the
default because live image streams consume memory and the web viewer has lower practical limits.

### Live Observability

Record diagnosis health through the normal event/artifact system, not through Rerun alone:

- sink mode and destination;
- pipeline configuration path, hash, and resolved non-secret values;
- detector model-load success or failure and load duration;
- per-pipeline frames submitted, completed, and dropped;
- per-pipeline inference latency and result age;
- worker ready/closed;
- frames submitted, rendered, and dropped;
- queue high-water mark;
- encode duration and output byte count distributions;
- connection and file-finalization errors.

Do not include credentials or unrestricted network addresses in public artifacts.

### Live Acceptance

- With Rerun disabled, runtime behavior and test results are unchanged.
- With no additional detector pipelines configured, existing person detection and policy behavior
  are unchanged.
- Multiple configured detectors receive the documented same-frame sample set.
- Enabling, disabling, or switching a diagnosis detector does not change policy event counts.
- With no viewer connected, the robot session continues and queue memory remains bounded.
- Closing the viewer does not stop inference, policy handling, recording, or robot teardown.
- Displayed boxes and states lag the processed camera frame by a measured and reported amount.
- Greet/goodbye event counts match the normal capture artifact.
- A short capture-enabled session produces both usable normal artifacts and, when requested, a
  finalized `.rrd` plus detector JSONL.
- No raw clinic video is sent outside the approved local/Tailscale destination.

## Implementation Sequence

1. Define and test `VisionObservation` serialization and schema versioning.
2. Capture raw person detections before ByteTrack and expose complete tracker observations without
   importing Rerun.
3. Add per-track anchor history, image displacement/velocity, and logical-ID handoff records.
4. Extend offline replay to write replay manifests and per-frame JSONL.
5. Implement raw-detection boxes, tracked boxes, paths, and movement overlays in the shared Rerun
   renderer.
6. Add the remaining classifier views and default blueprint.
7. Add doorway polygon and anchor overlays to offline replay.
8. Validate the June 25 and July 25 recordings with human-reviewed doorway calibration.
9. Define generic detector-layer observations, versioned pipeline configuration, and detector
   registry interfaces.
10. Add YOLO-World and Grounding DINO adapters plus optional per-pipeline ByteTrack.
11. Add bounded detector workers, detector JSONL, and a single background Rerun worker.
12. Generalize the Rerun renderer and blueprint for named live detector/tracker layers.
13. Add live CLI and OPS configuration, manifest provenance, and diagnosis health events.
14. Run a synthetic live stream test with fake detectors.
15. Run one supervised moving-door session with both real detectors at one FPS, raw video at five
    FPS, and Rerun `grpc+file` enabled.
16. Label door-state transitions and compare both models offline before selecting a production
    detector and cadence.

Steps 1-8 deliver offline review without increasing live-runtime risk. Real-time work starts only
after the renderer and observation contract have been accepted offline.

## Testing Strategy

### Unit

- Observation serialization and compatibility with unknown fields.
- Raw detection extraction, including confidence and box geometry before tracking.
- Track association, trajectory-window expiry, image velocity, ID churn, and logical handoff.
- Entity paths and Rerun archetype conversion using a fake Rerun module.
- Door polygon, anchor, stable occupancy, candidate dwell, and ID handoff rendering.
- Queue bounds, drop policy, worker shutdown, and disconnected-sink handling.
- Pipeline-config validation, unique IDs, registry dispatch, cadence, and same-frame sampling.
- Generic detection and track layer rendering with multiple enabled pipelines.
- Detector artifact rows remain complete when Rerun is disabled or disconnected.

### Integration

- Synthetic frames plus known boxes produce expected Rerun entities and timestamps.
- Two fake detector pipelines receive the same selected frames and produce independent namespaces.
- A deliberately slow fake detector drops stale work without delaying a fast detector or policy.
- Saved `.rrd` and JSONL carry the same frame/event counts.
- Sidecar and capture timestamp fallbacks remain aligned.
- Diagnosis mode does not alter policy events or existing artifact manifests beyond additive config.

### Human Review

- Door polygon remains aligned throughout each recorded camera pose.
- Track labels, anchors, and state colors are readable without hiding the visitor.
- Timeline selection makes pending/cancelled/confirmed decisions understandable.
- Live display latency is acceptable for diagnosis and does not affect the interaction.

## Performance And Safety Constraints

- Rerun remains in the optional `diagnosis` dependency group and is imported lazily.
- JPEG encoding and network writes run outside the vision/policy execution path.
- Queue memory is statically bounded and drop counts are visible.
- Live image rate is configurable independently from inference rate.
- Viewer and file sinks use unique run IDs and never overwrite normal run artifacts.
- Rerun failures are isolated after successful startup configuration.
- The viewer is read-only with respect to robot and policy state.

## Deferred Work

- Production selection for automatic semantic door detection. The closed-door feasibility benchmark
  is documented in
  `artifacts/vision-door-detection-benchmark/closed-door-v1/README.md`; model selection is deferred
  until YOLO-World and Grounding DINO are evaluated on recordings with actual door movement.
- Camera-pose compensation or projection of room geometry from robot kinematics.
- Interactive polygon editing inside Rerun.
- Simultaneous multi-profile inference in the live runner.
- A production operator dashboard or robot controls inside Rerun.
- Long-term centralized storage or remote access outside the approved local network.

## References

- [Rerun SDK operating modes](https://rerun.io/docs/reference/sdk/operating-modes)
- [Rerun sinks and simultaneous live/file output](https://rerun.io/docs/concepts/logging-and-ingestion/sinks)
- [Rerun video ingestion](https://rerun.io/docs/concepts/logging-and-ingestion/video)
- [Rerun viewer memory limits](https://rerun.io/docs/howto/visualization/limit-ram)
