from __future__ import annotations

import math
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from reachy_mini_brain.official_runtime.door_observation import (
    DoorDetectionInput,
    DoorMotionObserver,
    DoorObserverSettings,
    PersonBoxInput,
)
from reachy_mini_brain.official_runtime.door_review_rerun import DoorReviewRenderer


def test_door_state_moves_from_unknown_to_stable_then_moving_and_back() -> None:
    observer = DoorMotionObserver(
        DoorObserverSettings(
            stable_dwell_s=0.1,
            geometry_hold_s=0.5,
            motion_enter_threshold=0.1,
            motion_exit_threshold=0.03,
        )
    )
    frame = np.zeros((100, 120, 3), dtype=np.uint8)
    closed = [DoorDetectionInput(0.9, (10.0, 10.0, 40.0, 90.0))]
    open_door = [DoorDetectionInput(0.9, (40.0, 10.0, 80.0, 90.0))]

    first = observer.update(
        frame_index=0,
        frame_ts=0.0,
        frame_bgr=frame,
        door_detections=closed,
        people=[],
    )
    stable = observer.update(
        frame_index=1,
        frame_ts=0.2,
        frame_bgr=frame,
        door_detections=closed,
        people=[],
    )
    moving = observer.update(
        frame_index=2,
        frame_ts=0.4,
        frame_bgr=frame,
        door_detections=open_door,
        people=[],
    )
    settling = observer.update(
        frame_index=3,
        frame_ts=1.1,
        frame_bgr=frame,
        door_detections=None,
        people=[],
    )
    settled = observer.update(
        frame_index=4,
        frame_ts=1.3,
        frame_bgr=frame,
        door_detections=None,
        people=[],
    )

    assert first.state == "UNKNOWN"
    assert stable.state == "STABLE"
    assert moving.state == "MOVING"
    assert moving.geometry_change_score > moving.motion_enter_threshold
    assert settling.state == "MOVING"
    assert settled.state == "STABLE"


def test_multiple_associated_detections_select_one_continuity_aware_box() -> None:
    observer = DoorMotionObserver(DoorObserverSettings(retained_box_alpha=1.0))
    frame = np.zeros((100, 120, 3), dtype=np.uint8)
    observer.update(
        frame_index=0,
        frame_ts=0.0,
        frame_bgr=frame,
        door_detections=[DoorDetectionInput(0.9, (20.0, 5.0, 60.0, 95.0))],
        people=[],
    )

    observation = observer.update(
        frame_index=1,
        frame_ts=0.2,
        frame_bgr=frame,
        door_detections=[
            DoorDetectionInput(0.8, (5.0, 5.0, 35.0, 95.0)),
            DoorDetectionInput(0.7, (45.0, 5.0, 75.0, 95.0)),
        ],
        people=[],
    )

    assert observation.retained_box == (5.0, 5.0, 35.0, 95.0)
    assert observation.retained_box_normalized == pytest.approx(
        (20.0 / 120.0, 0.5, 30.0 / 120.0, 0.9)
    )


def test_person_interaction_reports_overlap_and_feet_distance() -> None:
    observer = DoorMotionObserver(DoorObserverSettings(retained_box_alpha=1.0))
    frame = np.zeros((100, 120, 3), dtype=np.uint8)

    observation = observer.update(
        frame_index=0,
        frame_ts=0.0,
        frame_bgr=frame,
        door_detections=[DoorDetectionInput(0.9, (20.0, 10.0, 60.0, 90.0))],
        people=[PersonBoxInput("track-1", (40.0, 50.0, 80.0, 100.0))],
    )

    interaction = observation.interactions[0]
    assert interaction.track_id == "track-1"
    assert interaction.overlap_ratio == pytest.approx(0.4)
    assert interaction.normalized_distance == pytest.approx(10.0 / np.hypot(120, 100))


def test_relative_motion_cancels_common_frame_translation() -> None:
    observer = DoorMotionObserver(
        DoorObserverSettings(
            stable_dwell_s=0.0,
            retained_box_alpha=1.0,
            relative_motion_enabled=True,
        )
    )
    rng = np.random.default_rng(7)
    first_frame = rng.integers(0, 256, (180, 240, 3), dtype=np.uint8)
    transform = np.asarray([[1.0, 0.0, 4.0], [0.0, 1.0, 2.0]], dtype=np.float32)
    translated = cv2.warpAffine(first_frame, transform, (240, 180), borderMode=cv2.BORDER_REFLECT)
    detection = [DoorDetectionInput(0.9, (70.0, 20.0, 150.0, 160.0))]
    observer.update(
        frame_index=0,
        frame_ts=0.0,
        frame_bgr=first_frame,
        door_detections=detection,
        people=[],
    )

    observation = observer.update(
        frame_index=1,
        frame_ts=0.2,
        frame_bgr=translated,
        door_detections=detection,
        people=[],
    )

    assert observation.global_frame_change_score > 0.25
    assert observation.relative_motion_valid
    assert observation.relative_door_motion_score < observation.motion_exit_threshold
    assert observation.motion_score == observation.relative_door_motion_score
    assert observation.state == "STABLE"


def test_relative_motion_detects_door_leaf_motion_against_static_background() -> None:
    observer = DoorMotionObserver(
        DoorObserverSettings(
            stable_dwell_s=0.0,
            retained_box_alpha=1.0,
            relative_motion_enabled=True,
        )
    )
    rng = np.random.default_rng(11)
    first_frame = rng.integers(0, 256, (180, 240, 3), dtype=np.uint8)
    moved_door = first_frame.copy()
    moved_door[20:160, 70:150] = np.roll(first_frame[20:160, 70:150], 4, axis=1)
    detection = [DoorDetectionInput(0.9, (70.0, 20.0, 150.0, 160.0))]
    observer.update(
        frame_index=0,
        frame_ts=0.0,
        frame_bgr=first_frame,
        door_detections=detection,
        people=[],
    )

    observation = observer.update(
        frame_index=1,
        frame_ts=0.2,
        frame_bgr=moved_door,
        door_detections=detection,
        people=[],
    )

    assert observation.relative_motion_valid
    assert observation.geometry_change_score == pytest.approx(0.0)
    assert observation.relative_door_motion_score > observation.motion_enter_threshold
    assert observation.motion_score == observation.relative_door_motion_score
    assert observation.state == "MOVING"


def test_relative_motion_is_disabled_by_default() -> None:
    observer = DoorMotionObserver(
        DoorObserverSettings(
            stable_dwell_s=0.0,
            retained_box_alpha=1.0,
        )
    )
    rng = np.random.default_rng(13)
    first_frame = rng.integers(0, 256, (180, 240, 3), dtype=np.uint8)
    moved_door = first_frame.copy()
    moved_door[20:160, 70:150] = np.roll(first_frame[20:160, 70:150], 4, axis=1)
    detection = [DoorDetectionInput(0.9, (70.0, 20.0, 150.0, 160.0))]
    observer.update(
        frame_index=0,
        frame_ts=0.0,
        frame_bgr=first_frame,
        door_detections=detection,
        people=[],
    )

    observation = observer.update(
        frame_index=1,
        frame_ts=0.2,
        frame_bgr=moved_door,
        door_detections=detection,
        people=[],
    )

    assert not observer.settings.relative_motion_enabled
    assert not observation.relative_motion_valid
    assert observation.relative_door_motion_score == 0.0
    assert observation.motion_score == observation.geometry_change_score == 0.0
    assert observation.state == "STABLE"


def test_door_review_renderer_logs_review_metrics() -> None:
    calls: list[tuple[str, object, object | None]] = []

    def archetype(name: str):
        return lambda *args, **kwargs: (name, args, kwargs)

    rr = SimpleNamespace(
        init=lambda *args, **kwargs: calls.append(("init", args, kwargs)),
        save=lambda path, **kwargs: calls.append(("save", path, kwargs)),
        set_time=lambda timeline, **kwargs: calls.append(("time", timeline, kwargs)),
        log=lambda entity, value, **kwargs: calls.append(("log", entity, (value, kwargs))),
        EncodedImage=archetype("EncodedImage"),
        Boxes2D=archetype("Boxes2D"),
        Scalars=archetype("Scalars"),
        SeriesLines=archetype("SeriesLines"),
        TextLog=archetype("TextLog"),
        Clear=archetype("Clear"),
        components=SimpleNamespace(
            InterpolationMode=SimpleNamespace(StepAfter="StepAfter"),
        ),
        send_blueprint=lambda blueprint, **kwargs: calls.append(
            ("blueprint", blueprint, kwargs)
        ),
        flush=lambda: None,
    )
    renderer = DoorReviewRenderer(
        save_path="review.rrd",
        recording_id="test",
        rr_module=rr,
    )
    observer = DoorMotionObserver(DoorObserverSettings(retained_box_alpha=1.0))
    frame = np.zeros((100, 120, 3), dtype=np.uint8)
    observation = observer.update(
        frame_index=0,
        frame_ts=10.0,
        frame_bgr=frame,
        door_detections=[DoorDetectionInput(0.9, (20.0, 10.0, 60.0, 90.0))],
        people=[PersonBoxInput("track-1", (40.0, 50.0, 80.0, 100.0))],
    )

    renderer.render(observation, frame)

    entities = {str(call[1]) for call in calls if call[0] == "log"}
    state_style = next(
        call
        for call in calls
        if call[0] == "log"
        and call[1] == "door_review/state/door"
        and call[2][0][0] == "SeriesLines"  # type: ignore[index]
    )
    blueprint_activation = next(call for call in calls if call[0] == "blueprint")
    assert blueprint_activation[2] == {"make_active": True, "make_default": True}
    assert state_style[2][1] == {"static": True}  # type: ignore[index]
    assert "door_review/camera/image" in entities
    assert "door_review/camera/raw_door_boxes" in entities
    assert "door_review/camera/retained_door_box" in entities
    assert "door_review/camera/people" in entities
    assert "door_review/state/door" in entities
    assert "door_review/signals/motion/combined" in entities
    assert "door_review/signals/motion/geometry" in entities
    assert "door_review/signals/motion/relative_flow" in entities
    assert "door_review/signals/flow_quality/valid" in entities
    assert "door_review/signals/flow_quality/door_coverage" in entities
    assert "door_review/signals/box/center_x" in entities
    assert "door_review/signals/person_overlap/max_observed" in entities
    assert "door_review/signals/person_overlap/track-1" in entities
    assert "door_review/signals/person_distance/track-1" in entities


def test_door_review_renderer_terminates_interaction_series_when_track_disappears() -> None:
    calls: list[tuple[str, object, object | None]] = []

    def archetype(name: str):
        return lambda *args, **kwargs: (name, args, kwargs)

    rr = SimpleNamespace(
        init=lambda *args, **kwargs: None,
        save=lambda *args, **kwargs: None,
        set_time=lambda *args, **kwargs: None,
        log=lambda entity, value, **kwargs: calls.append(("log", entity, (value, kwargs))),
        EncodedImage=archetype("EncodedImage"),
        Boxes2D=archetype("Boxes2D"),
        Scalars=archetype("Scalars"),
        SeriesLines=archetype("SeriesLines"),
        TextLog=archetype("TextLog"),
        Clear=archetype("Clear"),
        components=SimpleNamespace(
            InterpolationMode=SimpleNamespace(StepAfter="StepAfter"),
        ),
        flush=lambda: None,
    )
    renderer = DoorReviewRenderer(
        save_path="review.rrd",
        recording_id="test",
        rr_module=rr,
    )
    observer = DoorMotionObserver(DoorObserverSettings(retained_box_alpha=1.0))
    frame = np.zeros((100, 120, 3), dtype=np.uint8)
    detection = [DoorDetectionInput(0.9, (20.0, 10.0, 60.0, 90.0))]
    present = observer.update(
        frame_index=0,
        frame_ts=10.0,
        frame_bgr=frame,
        door_detections=detection,
        people=[PersonBoxInput("track-1", (40.0, 50.0, 80.0, 100.0))],
    )
    absent = observer.update(
        frame_index=1,
        frame_ts=10.2,
        frame_bgr=frame,
        door_detections=detection,
        people=[],
    )

    renderer.render(present, frame)
    calls.clear()
    renderer.render(absent, frame)

    terminated: dict[str, float] = {}
    for call, entity, payload in calls:
        if call != "log" or not str(entity).startswith("door_review/signals/person_"):
            continue
        value, _ = payload  # type: ignore[misc]
        terminated[str(entity)] = value[1][0]  # type: ignore[index]
    assert terminated.pop("door_review/signals/person_overlap/max_observed") == 0.0
    assert set(terminated) == {
        "door_review/signals/person_overlap/track-1",
        "door_review/signals/person_distance/track-1",
    }
    assert all(math.isnan(value) for value in terminated.values())
