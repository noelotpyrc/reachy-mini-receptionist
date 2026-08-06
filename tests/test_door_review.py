from __future__ import annotations

from reachy_mini_brain.official_runtime.door_review import (
    DoorDetectionFrame,
    _next_detection_completion_ts,
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
