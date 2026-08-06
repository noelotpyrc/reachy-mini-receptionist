from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from reachy_mini_brain.official_runtime.perception import PerceptionPipeline
from reachy_mini_brain.official_runtime.rerun_vision import RerunVisionRenderer
from reachy_mini_brain.official_runtime.visitor_triggers import TrackBox
from reachy_mini_brain.official_runtime.vision_observation import (
    DetectionObservation,
    LogicalTrackResolver,
    TrackMovementHistory,
    TrackObservation,
    VisionObservation,
    detection_observations,
)


def test_vision_observation_round_trip_ignores_unknown_additive_fields() -> None:
    observation = _observation()
    payload = observation.to_dict()
    payload["future_top_level"] = "ignored"
    payload["tracks"][0]["future_track_field"] = 42

    loaded = VisionObservation.from_dict(payload)

    assert loaded == observation
    assert loaded.schema_version == 1


def test_track_movement_history_derives_velocity_and_expires_trail() -> None:
    history = TrackMovementHistory(trail_window_s=1.0, stale_after_s=2.0)

    first = history.update_frame(10.0, {"track-0001": (0.2, 0.4)})["track-0001"]
    second = history.update_frame(10.5, {"track-0001": (0.3, 0.5)})["track-0001"]
    third = history.update_frame(11.25, {"track-0001": (0.5, 0.5)})["track-0001"]

    assert first.velocity is None
    assert second.displacement == pytest.approx((0.1, 0.1))
    assert second.velocity == pytest.approx((0.2, 0.2))
    assert third.track_age_s == pytest.approx(1.25)
    assert third.visible_sample_count == 3
    assert [sample[0] for sample in third.trail] == [10.5, 11.25]


def test_logical_track_resolver_preserves_id_across_accepted_handoff() -> None:
    resolver = LogicalTrackResolver()
    first = resolver.resolve(7)

    resolver.apply_handoff(7, 12)

    assert resolver.resolve(12) == first
    assert resolver.resolve(13) != first


def test_perception_pipeline_observation_keeps_raw_detection_and_exact_track_box() -> None:
    detections = _Detections(
        xyxy=np.asarray([[10.25, 20.5, 70.75, 90.125]]),
        confidence=np.asarray([0.875]),
        class_id=np.asarray([1]),
    )
    tracker = _ObservationTracker()
    pipeline = PerceptionPipeline(
        detector=_Detector(detections),
        tracker_factory=lambda _: tracker,
        observation_mode="replay",
        observation_run_id="unit-test",
    )
    frame = np.zeros((100, 200, 3), dtype=np.uint8)

    pipeline.process(frame, ts=5.0, frame_index=11, timestamp_source="sidecar:test.jsonl")
    observation = pipeline.last_observation

    assert observation is not None
    assert observation.frame_index == 11
    assert observation.timestamp_source == "sidecar:test.jsonl"
    assert observation.detections[0].confidence == pytest.approx(0.875)
    assert observation.detections[0].box == pytest.approx((10.25, 20.5, 70.75, 90.125))
    assert observation.tracks[0].box == pytest.approx((10.25, 20.5, 70.75, 90.125))
    assert observation.tracks[0].source_track_id == 7


def test_nested_person_box_is_marked_as_possible_duplicate_without_removal() -> None:
    detections = _Detections(
        xyxy=np.asarray(
            [
                [10.0, 10.0, 90.0, 90.0],
                [11.0, 11.0, 89.0, 40.0],
            ]
        ),
        confidence=np.asarray([0.65, 0.51]),
        class_id=np.asarray([1, 1]),
    )

    observations = detection_observations(detections, (100, 100))

    assert len(observations) == 2
    assert observations[0].possible_duplicate is False
    assert observations[1].possible_duplicate is True
    assert observations[1].duplicate_of_detection_index == 0


def test_rerun_renderer_logs_detection_track_movement_zone_and_states() -> None:
    calls: list[tuple[str, object, object | None]] = []

    def archetype(name: str):
        return lambda *args, **kwargs: (name, args, kwargs)

    rr = SimpleNamespace(
        init=lambda *args, **kwargs: calls.append(("init", args, kwargs)),
        save=lambda path, **kwargs: calls.append(("save", path, kwargs)),
        set_time=lambda timeline, **kwargs: calls.append(("time", timeline, kwargs)),
        log=lambda entity, value, **kwargs: calls.append(("log", entity, (value, kwargs))),
        flush=lambda: calls.append(("flush", None, None)),
        Image=archetype("Image"),
        Boxes2D=archetype("Boxes2D"),
        Points2D=archetype("Points2D"),
        LineStrips2D=archetype("LineStrips2D"),
        Arrows2D=archetype("Arrows2D"),
        Scalars=archetype("Scalars"),
        TextLog=archetype("TextLog"),
        Clear=archetype("Clear"),
    )
    renderer = RerunVisionRenderer(save_path="review.rrd", recording_id="test", rr_module=rr)

    renderer.render(_observation(), np.zeros((100, 200, 3), dtype=np.uint8))
    renderer.close()

    entities = [str(call[1]) for call in calls if call[0] == "log"]
    assert "replay/camera" in entities
    assert "replay/camera/detections" in entities
    assert "replay/camera/tracks" in entities
    assert "replay/camera/track_paths/track-0001" in entities
    assert "replay/camera/track_velocity/track-0001" in entities
    assert "replay/camera/doorway" in entities
    assert "replay/signals/person_counts/raw_person_detections" in entities
    assert "replay/signals/person_counts/possible_duplicate_person_detections" in entities
    assert "replay/signals/person_counts/byte_track_tracks" in entities
    assert "replay/signals/person_detection_confidence/raw/detection_0" in entities
    assert "replay/signals/person_detection_confidence/detector_threshold" in entities
    assert "replay/signals/image_plane_track_speed/track-0001" in entities
    assert "replay/signals/height/raw/track-0001" in entities
    assert "replay/signals/height/filtered/track-0001" in entities
    assert "replay/signals/height/threshold_near_enter" in entities
    assert "replay/signals/height/threshold_near_exit" in entities
    assert "replay/signals/log_height_slope/track-0001" in entities
    assert "replay/signals/log_height_slope/threshold_approaching" in entities
    assert "replay/signals/log_height_slope/threshold_receding" in entities
    assert "replay/states/presence/observed" in entities
    assert "replay/states/presence/retained" in entities
    assert "replay/states/proximity/observed" in entities
    assert "replay/states/proximity/retained" in entities
    assert "replay/states/motion/observed" in entities
    assert "replay/states/motion/retained" in entities
    assert "replay/states/doorway_occupancy/track-0001" in entities
    assert "replay/decisions/approach" in entities


class _Detections:
    def __init__(self, *, xyxy: np.ndarray, confidence: np.ndarray, class_id: np.ndarray) -> None:
        self.xyxy = xyxy
        self.confidence = confidence
        self.class_id = class_id

    def __len__(self) -> int:
        return len(self.xyxy)


class _Detector:
    def __init__(self, detections: _Detections) -> None:
        self.detections = detections

    def detect(self, frame: np.ndarray, *, bgr: bool = False) -> _Detections:
        return self.detections


class _ObservationTracker:
    def __init__(self) -> None:
        self.last_track_boxes: list[TrackBox] = []
        self.frame_debug: list[dict[str, object]] = []

    @property
    def debug_state(self) -> dict[str, object]:
        return {
            "active_track_id": 7,
            "handoff": False,
            "handoff_from_track_id": None,
            "presence": "PRESENT",
            "proximity": "FAR",
            "motion": "APPROACHING",
        }

    def update(self, persons: _Detections, *, ts: float | None = None) -> list[dict[str, object]]:
        del ts
        self.last_track_boxes = [
            TrackBox(
                track_id=7,
                source_track_id=7,
                tracking_source="byte_track",
                area=0.21,
                cx=0.2025,
                cy=0.553125,
                height=0.69625,
                clipped=False,
                box=(10.25, 20.5, 70.75, 90.125),  # type: ignore[arg-type]
            )
        ]
        self.frame_debug = [
            {
                "id": 7,
                "active": True,
                "height_filtered": 0.69,
                "log_height_slope": 0.08,
                "motion": "APPROACHING",
            }
        ]
        return []


def _observation() -> VisionObservation:
    detection = DetectionObservation(
        detection_index=0,
        class_id=1,
        class_name="person",
        confidence=0.91,
        box=(10.0, 20.0, 70.0, 90.0),
        center=(0.2, 0.55),
        bottom_center=(0.2, 0.9),
    )
    track = TrackObservation(
        logical_track_id="track-0001",
        track_id=12,
        source_track_id=12,
        tracking_source="byte_track",
        box=(10.0, 20.0, 70.0, 90.0),
        center=(0.2, 0.55),
        bottom_center=(0.2, 0.9),
        previous_anchor=(0.19, 0.88),
        displacement=(0.01, 0.02),
        velocity=(0.05, 0.1),
        track_age_s=1.2,
        visible_sample_count=7,
        trail=((10.0, 0.18, 0.85), (10.2, 0.2, 0.9)),
        area=0.21,
        height=0.7,
        height_filtered=0.69,
        height_slope=0.08,
        clipped=False,
        height_reliable=True,
        active=True,
        handoff_from_track_id=7,
        motion="APPROACHING",
        zone={
            "zone_occupancy": "INSIDE",
            "zone_candidate": None,
            "zone_anchor": [0.2, 0.9],
        },
    )
    return VisionObservation(
        frame_index=4,
        frame_ts=10.2,
        timestamp_source="sidecar:test.jsonl",
        frame_width=200,
        frame_height=100,
        mode="replay",
        run_id="test",
        detector={"threshold": 0.5},
        visitor_profile={
            "name": "visitor-v1-20260802",
            "parameters": {
                "near_enter_height": 0.71,
                "near_exit_height": 0.69,
                "height_signal": {"approach_slope": 0.04, "recede_slope": -0.05},
            },
        },
        movement={"trail_window_s": 3.0},
        zone_config={
            "schema_version": 1,
            "name": "doorway",
            "polygon": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.9], [0.1, 0.9]],
        },
        detections=(detection,),
        tracks=(track,),
        scene={
            "target_visible": True,
            "byte_track_track_count": 1,
            "observed_presence": "PRESENT",
            "retained_presence": "PRESENT",
            "observed_proximity": "FAR",
            "retained_proximity": "FAR",
            "observed_motion": "APPROACHING",
            "retained_motion": "APPROACHING",
        },
        events=({"kind": "approach", "id": 12},),
    )
