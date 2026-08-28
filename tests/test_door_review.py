from __future__ import annotations

from reachy_mini_brain.official_runtime.door_review import (
    DoorDetectionFrame,
    _next_detection_completion_ts,
    _warmup_frame_indices,
)


def test_intermediate_frame_is_released_by_next_dino_completion() -> None:
    rows = {
        10: DoorDetectionFrame((), 10.25, 250.0),
        13: DoorDetectionFrame((), 11.10, 220.0),
    }

    decision_ts = _next_detection_completion_ts(
        11,
        10.4,
        detection_frame_indices=(10, 13),
        detection_rows=rows,
    )

    assert decision_ts == 11.10


def test_semantic_frame_uses_its_own_dino_completion() -> None:
    rows = {10: DoorDetectionFrame((), 10.25, 250.0)}

    decision_ts = _next_detection_completion_ts(
        10,
        10.0,
        detection_frame_indices=(10,),
        detection_rows=rows,
    )

    assert decision_ts == 10.25


def test_warmup_uses_semantic_history_plus_full_policy_tail() -> None:
    indices = _warmup_frame_indices(
        source_frame_offset=100,
        warmup_source_frames=60,
        timestamp_frame_indices=tuple(range(120)),
        detection_frame_indices=(20, 40, 50, 70, 90, 110),
        policy_tail_frames=10,
    )

    assert indices == (40, 50, 70, *range(90, 100))
