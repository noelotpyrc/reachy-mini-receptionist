from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from reachy_mini_brain.official_runtime.door_observation import (
    DoorFrameObservation,
    DoorPersonInteraction,
    PersonBoxInput,
)
from reachy_mini_brain.official_runtime.door_policy import (
    DoorPolicySettings,
    DoorPolicyTriggerEngine,
)
from reachy_mini_brain.official_runtime.door_policy_live import LiveDoorPolicyCoordinator
from reachy_mini_brain.official_runtime.live_detection import (
    DetectionLayerObservation,
    FramePacket,
    LayerDetection,
)


def test_goodbye_requires_distance_crossing_then_later_door_movement() -> None:
    engine = DoorPolicyTriggerEngine()

    first = engine.update(_frame(0, 0.0, "STABLE", distance=0.12))
    armed = engine.update(_frame(1, 0.2, "STABLE", distance=0.04))
    confirmed = engine.update(_frame(2, 0.4, "MOVING", distance=0.03))

    assert not first.events
    assert not armed.events
    assert armed.decision == "goodbye_armed"
    assert [event["kind"] for event in confirmed.events] == ["depart"]
    assert confirmed.reason == "interaction_then_door_moving"


def test_distance_crossing_alone_never_emits_goodbye() -> None:
    engine = DoorPolicyTriggerEngine(DoorPolicySettings(goodbye_candidate_timeout_s=0.5))

    engine.update(_frame(0, 0.0, "STABLE", distance=0.12))
    armed = engine.update(_frame(1, 0.1, "STABLE", distance=0.04))
    still_armed = engine.update(_frame(2, 1.0, "STABLE", distance=0.03))

    assert armed.goodbye_candidate_armed
    assert not armed.events
    assert still_armed.goodbye_candidate_armed
    assert not still_armed.events


def test_goodbye_stays_armed_while_interaction_remains_inside() -> None:
    engine = DoorPolicyTriggerEngine(DoorPolicySettings(goodbye_candidate_timeout_s=0.5))

    engine.update(_frame(0, 0.0, "STABLE", distance=0.12))
    armed = engine.update(_frame(1, 0.1, "STABLE", distance=0.04))
    supported = engine.update(_frame(2, 1.0, "STABLE", distance=0.03))
    confirmed = engine.update(_frame(3, 1.2, "MOVING", distance=0.03))

    assert armed.goodbye_candidate_last_supported_ts == 0.1
    assert supported.goodbye_candidate_armed
    assert supported.goodbye_candidate_last_supported_ts == 1.0
    assert [event["kind"] for event in confirmed.events] == ["depart"]


def test_goodbye_expires_after_interaction_support_is_lost() -> None:
    engine = DoorPolicyTriggerEngine(DoorPolicySettings(goodbye_candidate_timeout_s=0.5))

    engine.update(_frame(0, 0.0, "STABLE", distance=0.12))
    engine.update(_frame(1, 0.1, "STABLE", distance=0.04))
    recently_lost = engine.update(_frame(2, 0.4, "STABLE"))
    expired = engine.update(_frame(3, 0.7, "STABLE"))
    moving = engine.update(_frame(4, 0.8, "MOVING"))

    assert recently_lost.goodbye_candidate_armed
    assert not expired.goodbye_candidate_armed
    assert expired.goodbye_candidate_last_supported_ts is None
    assert not moving.events


def test_greet_requires_door_movement_without_person_then_later_interaction() -> None:
    engine = DoorPolicyTriggerEngine()

    engine.update(_frame(0, 0.0, "STABLE"))
    armed = engine.update(_frame(1, 0.2, "MOVING"))
    greeted = engine.update(_frame(2, 0.4, "MOVING", distance=0.03))

    assert armed.decision == "greet_armed"
    assert not armed.events
    assert [event["kind"] for event in greeted.events] == ["approach"]


def test_person_and_door_first_seen_moving_on_same_frame_is_ambiguous() -> None:
    engine = DoorPolicyTriggerEngine()

    result = engine.update(_frame(0, 0.0, "MOVING", distance=0.03))

    assert not result.greet_candidate_armed
    assert not result.events


def test_boundary_clipped_person_cancels_armed_greet_without_hiding_presence() -> None:
    engine = DoorPolicyTriggerEngine()

    engine.update(_frame(0, 0.0, "STABLE"))
    armed = engine.update(_frame(1, 0.2, "MOVING"))
    blocked = engine.update(
        _frame(
            2,
            0.4,
            "MOVING",
            distance=0.03,
            person_area_ratio=0.35,
            boundary_clearance_ratio=0.0,
        )
    )

    assert armed.greet_candidate_armed
    assert blocked.observed_presence == "PRESENT"
    assert blocked.interaction_ineligible_reasons == {
        "person-1": "person_box_boundary_clipped"
    }
    assert not blocked.greet_candidate_armed
    assert not blocked.events


def test_oversized_person_cannot_arm_or_sustain_goodbye() -> None:
    engine = DoorPolicyTriggerEngine()

    engine.update(_frame(0, 0.0, "STABLE", distance=0.12))
    armed = engine.update(_frame(1, 0.2, "STABLE", distance=0.03))
    blocked = engine.update(
        _frame(
            2,
            0.4,
            "STABLE",
            distance=0.03,
            person_area_ratio=0.65,
        )
    )
    moving = engine.update(
        _frame(
            3,
            0.6,
            "MOVING",
            distance=0.03,
            person_area_ratio=0.65,
        )
    )

    assert armed.goodbye_candidate_armed
    assert blocked.interaction_ineligible_reasons == {
        "person-1": "person_box_oversized"
    }
    assert not blocked.goodbye_candidate_armed
    assert not moving.events


def test_retained_person_presence_blocks_greet_arm_during_short_detection_gap() -> None:
    engine = DoorPolicyTriggerEngine(DoorPolicySettings(person_retention_s=0.75))

    engine.update(_frame(0, 0.0, "STABLE", distance=0.12))
    engine.update(_frame(1, 0.2, "STABLE"))
    moving = engine.update(_frame(2, 0.4, "MOVING"))

    assert moving.retained_presence == "PRESENT"
    assert not moving.greet_candidate_armed
    assert not moving.events


def test_live_coordinator_aligns_delayed_dino_result_to_buffered_source_frames() -> None:
    results = []
    coordinator = LiveDoorPolicyCoordinator(result_callback=lambda door, policy: results.append((door, policy)))
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    no_people = SimpleNamespace(tracks=())
    person = SimpleNamespace(
        tracks=(SimpleNamespace(logical_track_id="logical-1", box=(95.0, 5.0, 145.0, 98.0)),)
    )

    coordinator.submit_frame(FramePacket(0, 0.0, frame), no_people)
    coordinator.submit_frame(FramePacket(1, 0.2, frame), person)
    coordinator.submit_detection(_detection(1, 0.2, 0.7, box=(100.0, 0.0, 150.0, 100.0)))

    assert [door.frame_index for door, _ in results] == [0, 1]
    assert results[-1][0].people[0].track_id == "logical-1"
    assert results[-1][1].decision_ts == 0.7
    assert results[-1][1].decision_latency_s == pytest.approx(0.5)


def test_live_coordinator_ignores_non_policy_detection_layers() -> None:
    results = []
    coordinator = LiveDoorPolicyCoordinator(result_callback=lambda door, policy: results.append((door, policy)))
    coordinator.submit_frame(
        FramePacket(0, 0.0, np.zeros((10, 10, 3), dtype=np.uint8)),
        SimpleNamespace(tracks=()),
    )

    coordinator.submit_detection(_detection(0, 0.0, 0.5, role="diagnosis"))

    assert results == []


def _frame(
    frame_index: int,
    frame_ts: float,
    state: str,
    *,
    distance: float | None = None,
    overlap: float = 0.0,
    person_area_ratio: float = 0.10,
    boundary_clearance_ratio: float = 0.10,
) -> DoorFrameObservation:
    people = ()
    interactions = ()
    if distance is not None:
        people = (PersonBoxInput(track_id="person-1", box=(10.0, 10.0, 40.0, 90.0)),)
        interactions = (
            DoorPersonInteraction(
                track_id="person-1",
                overlap_ratio=overlap,
                normalized_distance=distance,
                person_area_ratio=person_area_ratio,
                person_boundary_clearance_ratio=boundary_clearance_ratio,
            ),
        )
    return DoorFrameObservation(
        frame_index=frame_index,
        frame_ts=frame_ts,
        frame_width=640,
        frame_height=480,
        state=state,
        motion_score=0.2 if state == "MOVING" else 0.0,
        motion_enter_threshold=0.1,
        motion_exit_threshold=0.035,
        geometry_change_score=0.2 if state == "MOVING" else 0.0,
        relative_door_motion_score=0.0,
        relative_door_displacement=0.0,
        relative_motion_valid=False,
        door_flow_tracked_points=0,
        door_flow_inlier_ratio=0.0,
        door_flow_coverage=0.0,
        background_flow_tracked_points=0,
        background_flow_inlier_ratio=0.0,
        global_frame_change_score=0.0,
        semantic_updated=True,
        raw_door_detections=(),
        retained_box=(100.0, 20.0, 220.0, 460.0),
        retained_box_normalized=(0.15625, 0.04167, 0.34375, 0.95833),
        people=people,
        interactions=interactions,
    )


def _detection(
    frame_index: int,
    frame_ts: float,
    completed_ts: float,
    *,
    box: tuple[float, float, float, float] = (1.0, 1.0, 8.0, 9.0),
    role: str = "policy",
) -> DetectionLayerObservation:
    return DetectionLayerObservation(
        run_id="test",
        pipeline_id="door_grounding_dino",
        frame_index=frame_index,
        frame_ts=frame_ts,
        completed_ts=completed_ts,
        inference_latency_ms=(completed_ts - frame_ts) * 1000.0,
        detector_config={"role": role},
        tracker_config={"implementation": "none"},
        detections=(
            LayerDetection(
                detection_index=0,
                class_id=0,
                class_name="door",
                confidence=0.8,
                box=box,
            ),
        ),
    )
