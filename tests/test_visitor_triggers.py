from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from reachy_mini_brain.official_runtime.perception import (
    ApproachTracker,
    LegacyApproachTracker,
    PerceptionPipeline,
    build_approach_tracker,
)
from reachy_mini_brain.official_runtime.visitor_trigger_profiles import (
    DEFAULT_VISITOR_TRIGGER_PROFILE,
    DOOR_V1_20260805,
    DOOR_V2_20260809,
    VISITOR_V1_20260802,
    resolve_visitor_trigger_profile,
)
from reachy_mini_brain.official_runtime.visitor_triggers import (
    HeightMotionSignal,
    HeightSignalConfig,
    Motion,
    Proximity,
    ProximityClassifier,
    TrackBox,
    VisitorTriggerConfig,
    VisitorTriggerEngine,
)


def _config(**overrides) -> VisitorTriggerConfig:
    values = {
        "present_area_frac": 0.01,
        "absent_reset_s": 1.0,
        "near_enter_height": 0.60,
        "near_exit_height": 0.50,
        "proximity_persist_s": 0.0,
        "goodbye_confirm_s": 0.4,
        "goodbye_candidate_timeout_s": 1.5,
        "goodbye_additional_shrink": 0.03,
        "height_signal": HeightSignalConfig(
            median_window=1,
            ema_alpha=1.0,
            slope_window_s=0.8,
            min_slope_span_s=0.4,
            motion_persist_s=0.0,
            approach_slope=0.10,
            recede_slope=-0.10,
            reset_gap_s=2.0,
        ),
    }
    values.update(overrides)
    return VisitorTriggerConfig(**values)


def _box(height: float, *, track_id: int = 1, clipped: bool = False) -> TrackBox:
    return TrackBox(
        track_id=track_id,
        area=0.10,
        cx=0.50,
        cy=0.50,
        height=height,
        clipped=clipped,
        box=(20, 10, 80, 90),
    )


def _run(engine: VisitorTriggerEngine, heights: list[float], *, start: float = 0.0) -> list[dict]:
    events = []
    for index, height in enumerate(heights):
        events.extend(engine.update(start + index * 0.2, [_box(height)]))
    return events


def test_height_signal_rejects_one_sample_spike() -> None:
    signal = HeightMotionSignal(
        HeightSignalConfig(
            median_window=3,
            ema_alpha=1.0,
            slope_window_s=0.8,
            min_slope_span_s=0.4,
            motion_persist_s=0.0,
            approach_slope=0.10,
            recede_slope=-0.10,
        )
    )

    snapshots = [signal.update(index * 0.2, height, valid=True) for index, height in enumerate([0.4] * 5)]
    snapshots.append(signal.update(1.0, 0.9, valid=True))
    snapshots.append(signal.update(1.2, 0.4, valid=True))

    assert snapshots[-2].median_height == pytest.approx(0.4)
    assert snapshots[-1].filtered_height == pytest.approx(0.4)
    assert snapshots[-1].motion is Motion.STATIONARY


def test_height_signal_classifies_sustained_relative_motion() -> None:
    signal = HeightMotionSignal(_config().height_signal)

    growing = [signal.update(index * 0.2, height, valid=True) for index, height in enumerate([0.3, 0.3, 0.3, 0.4])]
    assert growing[-1].motion is Motion.APPROACHING
    assert growing[-1].log_slope is not None and growing[-1].log_slope > 0.10

    receding = HeightMotionSignal(_config().height_signal)
    shrinking = [
        receding.update(index * 0.2, height, valid=True)
        for index, height in enumerate([0.7, 0.7, 0.7, 0.6])
    ]
    assert shrinking[-1].motion is Motion.RECEDING
    assert shrinking[-1].log_slope is not None and shrinking[-1].log_slope < -0.10


def test_clipped_sample_does_not_update_height_trend() -> None:
    signal = HeightMotionSignal(_config().height_signal)
    for index in range(3):
        before = signal.update(index * 0.2, 0.4, valid=True)

    clipped = signal.update(0.6, 0.95, valid=False)

    assert clipped.sample_valid is False
    assert clipped.filtered_height == before.filtered_height
    assert clipped.log_slope == before.log_slope


def test_proximity_is_unknown_far_or_near_with_hysteresis() -> None:
    proximity = ProximityClassifier(near_enter_height=0.6, near_exit_height=0.5, persist_s=0.0)

    first = proximity.update(0.0, filtered_height=0.7, valid=True)
    deadband = proximity.update(0.2, filtered_height=0.55, valid=True)
    far = proximity.update(0.4, filtered_height=0.49, valid=True)
    still_far = proximity.update(0.6, filtered_height=0.55, valid=True)
    near = proximity.update(0.8, filtered_height=0.61, valid=True)

    assert first.previous is Proximity.UNKNOWN and first.current is Proximity.NEAR
    assert deadband.current is Proximity.NEAR
    assert far.previous is Proximity.NEAR and far.current is Proximity.FAR
    assert still_far.current is Proximity.FAR
    assert near.previous is Proximity.FAR and near.current is Proximity.NEAR


def test_first_seen_near_does_not_greet() -> None:
    engine = VisitorTriggerEngine(_config())

    events = _run(engine, [0.70, 0.70, 0.70, 0.66, 0.66])

    assert events == []
    assert engine.debug_state["proximity"] == "NEAR"
    assert engine.debug_state["greet"] is False


def test_far_to_near_with_recent_approach_greets_once() -> None:
    engine = VisitorTriggerEngine(_config())

    events = _run(engine, [0.35, 0.35, 0.35, 0.40, 0.48, 0.56, 0.62, 0.66, 0.70])

    assert [event["kind"] for event in events] == ["approach"]
    assert events[0]["reason"] == "far_to_near_while_approaching"
    assert events[0]["proximity"] == "NEAR"
    assert events[0]["motion"] == "APPROACHING"


def test_target_loss_reports_unknown_observation_while_visit_state_is_retained() -> None:
    engine = VisitorTriggerEngine(_config(absent_reset_s=8.0))
    _run(engine, [0.35, 0.35, 0.35, 0.40, 0.48, 0.56, 0.62, 0.66, 0.70])

    engine.update(1.8, [])
    state = engine.debug_state

    assert state["target_visible"] is False
    assert state["presence"] == "PRESENT"
    assert state["visit_presence"] == "PRESENT"
    assert state["observed_presence"] == "ABSENT"
    assert state["retained_presence"] == "PRESENT"
    assert state["proximity"] == "NEAR"
    assert state["retained_proximity"] == "NEAR"
    assert state["observed_proximity"] == "UNKNOWN"
    assert state["retained_motion"] == "APPROACHING"
    assert state["observed_motion"] == "UNKNOWN"
    assert state["motion"] == "UNKNOWN"


def test_near_to_far_requires_continued_recession_before_goodbye() -> None:
    engine = VisitorTriggerEngine(_config())

    events = _run(engine, [0.70, 0.70, 0.70, 0.66, 0.60, 0.54, 0.48, 0.43, 0.38, 0.34])

    assert [event["kind"] for event in events] == ["depart"]
    assert events[0]["reason"] == "far_and_still_receding"
    assert engine.debug_state["depart"] is True


def test_near_to_far_that_settles_cancels_goodbye() -> None:
    engine = VisitorTriggerEngine(_config())

    events = _run(engine, [0.70, 0.70, 0.70, 0.58, 0.48, 0.48, 0.48, 0.48, 0.48, 0.48, 0.48])

    assert events == []
    assert engine.debug_state["proximity"] == "FAR"
    assert engine.debug_state["motion"] == "STATIONARY"
    assert engine.debug_state["goodbye_pending"] is False


def test_disappearance_does_not_confirm_pending_goodbye() -> None:
    engine = VisitorTriggerEngine(_config(absent_reset_s=1.0))
    events = _run(engine, [0.70, 0.70, 0.70, 0.58, 0.48])

    events.extend(engine.update(1.0, []))
    events.extend(engine.update(1.6, []))
    events.extend(engine.update(2.2, []))

    assert events == []
    assert engine.debug_state["presence"] == "ABSENT"
    assert engine.debug_state["depart"] is False


def test_clipped_first_seen_near_does_not_greet_or_seed_motion() -> None:
    engine = VisitorTriggerEngine(_config(proximity_persist_s=0.2, clipped_near_height=0.8))

    events = []
    events.extend(engine.update(0.0, [_box(0.95, clipped=True)]))
    events.extend(engine.update(0.2, [_box(0.95, clipped=True)]))
    events.extend(engine.update(0.4, [_box(0.65)]))
    events.extend(engine.update(0.6, [_box(0.65)]))

    assert events == []
    assert engine.debug_state["proximity"] == "NEAR"
    assert engine.debug_state["greet"] is False


def test_compatible_track_handoff_preserves_greet_latch() -> None:
    engine = VisitorTriggerEngine(_config(handoff_grace_s=1.0))
    events = _run(engine, [0.35, 0.35, 0.35, 0.40, 0.48, 0.56, 0.62])

    events.extend(engine.update(1.4, [_box(0.64, track_id=2)]))
    events.extend(engine.update(1.6, [_box(0.66, track_id=2)]))

    assert [event["kind"] for event in events] == ["approach"]
    assert engine.debug_state["active_track_id"] == 2
    assert engine.debug_state["greet"] is True


def test_sustained_absence_resets_event_latches_for_a_new_visit() -> None:
    engine = VisitorTriggerEngine(_config(absent_reset_s=0.5))
    first = _run(engine, [0.35, 0.35, 0.35, 0.40, 0.48, 0.56, 0.62])
    engine.update(1.4, [])
    engine.update(2.0, [])
    second = _run(engine, [0.35, 0.35, 0.35, 0.40, 0.48, 0.56, 0.62], start=2.2)

    assert [event["kind"] for event in first + second] == ["approach", "approach"]


class _TrackedDetections:
    def __init__(
        self,
        boxes: list[tuple[float, float, float, float]],
        tracker_ids: list[int] | None,
    ) -> None:
        self.xyxy = np.asarray(boxes, dtype=float).reshape((-1, 4))
        self.tracker_id = None if tracker_ids is None else np.asarray(tracker_ids, dtype=int)

    def __len__(self) -> int:
        return len(self.xyxy)


def test_approach_tracker_adapter_preserves_event_contract(monkeypatch) -> None:
    class FakeByteTrack:
        def update_with_detections(self, detections):
            return detections

    fake_supervision = types.SimpleNamespace(ByteTrack=FakeByteTrack)
    monkeypatch.setitem(sys.modules, "supervision", fake_supervision)
    tracker = ApproachTracker((100, 100), trigger_config=_config())
    events = []
    heights = [0.35, 0.35, 0.35, 0.40, 0.48, 0.56, 0.62]
    for index, height in enumerate(heights):
        detections = _TrackedDetections([(30, 20, 70, 20 + height * 100)], [7])
        events.extend(tracker.update(detections, ts=index * 0.2))

    assert [event["kind"] for event in events] == ["approach"]
    assert events[0]["id"] == 7
    assert {"area", "cx", "cy"} <= events[0].keys()
    assert tracker.frame_debug[0]["height"] == pytest.approx(0.62)
    assert tracker.frame_debug[0]["active"] is True
    assert tracker.frame_debug[0]["proximity_change"] == "FAR->NEAR"
    assert tracker.frame_debug[0]["near_enter_height"] == 0.6
    assert tracker.debug_state["proximity"] == "NEAR"


def test_unambiguous_id_change_preserves_active_visit() -> None:
    engine = VisitorTriggerEngine(_config(handoff_center_distance=0.05))
    engine.update(0.0, [_box(0.35, track_id=1)])

    replacement = TrackBox(
        track_id=2,
        area=0.10,
        cx=0.90,
        cy=0.50,
        height=0.40,
        clipped=False,
        box=(70, 10, 100, 90),
    )
    events = engine.update(0.2, [replacement], scene_person_count=1)

    assert events == []
    assert engine.debug_state["active_track_id"] == 2
    assert engine.debug_state["presence"] == "PRESENT"
    assert engine.debug_state["handoff"] is True


def test_unambiguous_id_change_preserves_motion_history() -> None:
    engine = VisitorTriggerEngine(
        _config(
            near_enter_height=0.75,
            near_exit_height=0.70,
            goodbye_confirm_s=0.2,
            goodbye_additional_shrink=0.01,
        )
    )
    events = []
    for ts, height in [(0.0, 0.80), (0.3, 0.78), (0.6, 0.74)]:
        events.extend(engine.update(ts, [_box(height, track_id=1)], scene_person_count=1))

    events.extend(engine.update(0.9, [_box(0.69, track_id=2)], scene_person_count=1))
    handoff_state = engine.debug_state
    events.extend(engine.update(1.2, [_box(0.65, track_id=2)], scene_person_count=1))

    assert handoff_state["handoff"] is True
    assert handoff_state["handoff_from_track_id"] == 1
    assert handoff_state["motion"] == "RECEDING"
    assert handoff_state["proximity_change"] == "NEAR->FAR"
    assert [event["kind"] for event in events] == ["depart"]
    assert events[0]["id"] == 2


def test_ambiguous_id_change_does_not_force_handoff() -> None:
    engine = VisitorTriggerEngine(_config(handoff_center_distance=0.05))
    engine.update(0.0, [_box(0.35, track_id=1)])
    candidates = [
        TrackBox(2, 0.10, 0.80, 0.50, 0.40, False, (70, 10, 100, 90)),
        TrackBox(3, 0.10, 0.20, 0.50, 0.40, False, (0, 10, 30, 90)),
    ]

    events = engine.update(0.2, candidates, scene_person_count=2)

    assert events == []
    assert engine.debug_state["active_track_id"] == 1
    assert engine.debug_state["handoff"] is False


def test_approach_tracker_bridges_unambiguous_bytetrack_gaps(monkeypatch) -> None:
    outputs = iter(
        [
            _TrackedDetections([(30, 20, 70, 55)], [1]),
            _TrackedDetections([], []),
            _TrackedDetections([(30, 20, 70, 68)], [2]),
            _TrackedDetections([(30, 20, 70, 76)], [2]),
            _TrackedDetections([(30, 20, 70, 82)], [2]),
        ]
    )

    class FakeByteTrack:
        def update_with_detections(self, detections):
            return next(outputs)

    monkeypatch.setitem(sys.modules, "supervision", types.SimpleNamespace(ByteTrack=FakeByteTrack))
    tracker = ApproachTracker((100, 100), trigger_config=_config())
    events = []
    frame_sources = []
    for index, height in enumerate([0.35, 0.40, 0.48, 0.56, 0.62]):
        raw = _TrackedDetections([(30, 20, 70, 20 + height * 100)], None)
        events.extend(tracker.update(raw, ts=index * 0.2))
        frame_sources.append(tracker.frame_debug[0]["tracking_source"])

    assert [event["kind"] for event in events] == ["approach"]
    assert frame_sources == [
        "byte_track",
        "raw_detection_fallback",
        "byte_track",
        "byte_track",
        "byte_track",
    ]
    assert tracker.debug_state["active_track_id"] == 2
    assert tracker.debug_state["presence"] == "PRESENT"


def test_versioned_profile_metadata_contains_complete_evaluated_configuration() -> None:
    legacy = resolve_visitor_trigger_profile(DEFAULT_VISITOR_TRIGGER_PROFILE).metadata(smooth=2)
    visitor = resolve_visitor_trigger_profile(VISITOR_V1_20260802).metadata()
    door_v1 = resolve_visitor_trigger_profile(DOOR_V1_20260805).metadata()
    door_v2 = resolve_visitor_trigger_profile(DOOR_V2_20260809).metadata()

    assert legacy == {
        "name": "legacy",
        "implementation": "legacy_area_v1",
        "parameters": {
            "growth_factor": 1.3,
            "greet_floor": 0.10,
            "min_area_frac": 0.06,
            "depart_factor": 0.6,
            "present_frac": 0.03,
            "reset_absent": 40,
            "history": 30,
        },
        "tracker_smoothing_window": 2,
    }
    assert visitor["name"] == "visitor-v1-20260802"
    assert visitor["parameters"]["near_enter_height"] == 0.71
    assert visitor["parameters"]["near_exit_height"] == 0.69
    assert visitor["parameters"]["goodbye_confirm_s"] == 0.2
    assert visitor["parameters"]["goodbye_additional_shrink"] == 0.01
    assert visitor["parameters"]["height_signal"] == {
        "median_window": 3,
        "ema_alpha": 1.0,
        "slope_window_s": 1.0,
        "min_slope_span_s": 0.5,
        "motion_persist_s": 0.0,
        "approach_slope": 0.04,
        "recede_slope": -0.05,
        "reset_gap_s": 1.5,
        "max_samples": 30,
    }
    assert door_v1["parameters"]["door_observer"]["close_person_guard_enabled"] is False
    assert door_v1["parameters"]["door_policy"]["interaction_eligibility_enabled"] is False
    assert door_v2["parameters"]["door_observer"]["occluding_person_area_ratio"] == 0.35
    assert door_v2["parameters"]["door_policy"]["interaction_person_max_area_ratio"] == 0.40


def test_unknown_profile_is_rejected_before_detector_initialization() -> None:
    with pytest.raises(ValueError, match="unknown visitor trigger profile"):
        PerceptionPipeline(visitor_trigger_profile="does-not-exist")


def test_profile_can_switch_to_visitor_and_roll_back_to_legacy(monkeypatch) -> None:
    class FakeByteTrack:
        def update_with_detections(self, detections):
            return detections

    monkeypatch.setitem(sys.modules, "supervision", types.SimpleNamespace(ByteTrack=FakeByteTrack))
    legacy_profile = resolve_visitor_trigger_profile(DEFAULT_VISITOR_TRIGGER_PROFILE)
    visitor_profile = resolve_visitor_trigger_profile(VISITOR_V1_20260802)

    first = build_approach_tracker((100, 100), profile=legacy_profile)
    candidate = build_approach_tracker((100, 100), profile=visitor_profile)
    rolled_back = build_approach_tracker((100, 100), profile=legacy_profile)

    assert isinstance(first, LegacyApproachTracker)
    assert isinstance(candidate, ApproachTracker)
    assert candidate._engine.config.near_enter_height == 0.71
    assert isinstance(rolled_back, LegacyApproachTracker)


def test_legacy_profile_preserves_original_area_trigger_behavior(monkeypatch) -> None:
    class FakeByteTrack:
        def update_with_detections(self, detections):
            return detections

    monkeypatch.setitem(sys.modules, "supervision", types.SimpleNamespace(ByteTrack=FakeByteTrack))
    tracker = build_approach_tracker(
        (100, 100),
        profile=resolve_visitor_trigger_profile(DEFAULT_VISITOR_TRIGGER_PROFILE),
    )
    events = []
    for index, area in enumerate([0.04, 0.08, 0.11, 0.06, 0.05]):
        height = area / 0.4
        detections = _TrackedDetections([(30, 10, 70, 10 + height * 100)], [7])
        events.extend(tracker.update(detections, ts=index * 0.2))

    assert [event["kind"] for event in events] == ["approach", "depart"]
    assert all(set(event) == {"kind", "id", "area", "cx", "cy"} for event in events)
