from __future__ import annotations

import pytest

from reachy_mini_brain.official_runtime.visitor_triggers import TrackBox
from reachy_mini_brain.official_runtime.visitor_zones import (
    TrackedPolygonZone,
    TrackedPolygonZoneConfig,
    ZoneAnchor,
    ZoneOccupancy,
)


DOOR_POLYGON = ((0.35, 0.15), (0.65, 0.15), (0.65, 0.75), (0.35, 0.75))


def _box(
    *,
    track_id: int = 1,
    cx: float = 0.5,
    cy: float = 0.4,
    height: float = 0.4,
    clipped: bool = False,
) -> TrackBox:
    return TrackBox(
        track_id=track_id,
        area=0.1,
        cx=cx,
        cy=cy,
        height=height,
        clipped=clipped,
        box=(0, 0, 10, 10),
    )


def _zone(**overrides) -> TrackedPolygonZone:
    values = {
        "name": "doorway",
        "polygon": DOOR_POLYGON,
        "anchor": ZoneAnchor.BOTTOM_CENTER,
        "enter_dwell_s": 0.4,
        "exit_dwell_s": 0.4,
        "stale_after_s": 1.0,
    }
    values.update(overrides)
    return TrackedPolygonZone(TrackedPolygonZoneConfig(**values))


def test_zone_config_rejects_invalid_normalized_polygon() -> None:
    with pytest.raises(ValueError, match="at least three"):
        TrackedPolygonZoneConfig(name="doorway", polygon=((0.0, 0.0), (1.0, 1.0)))
    with pytest.raises(ValueError, match="normalized coordinates"):
        TrackedPolygonZoneConfig(name="doorway", polygon=((0.0, 0.0), (1.2, 0.0), (0.0, 1.0)))
    with pytest.raises(ValueError, match="non-zero area"):
        TrackedPolygonZoneConfig(name="doorway", polygon=((0.0, 0.0), (0.5, 0.5), (1.0, 1.0)))


def test_dwell_qualifies_initial_door_occupancy() -> None:
    zone = _zone()

    first = zone.update(0.0, [_box()])[1]
    pending = zone.update(0.2, [_box()])[1]
    entered = zone.update(0.4, [_box()])[1]

    assert first.current is ZoneOccupancy.UNKNOWN
    assert first.raw_occupancy is ZoneOccupancy.INSIDE
    assert first.candidate is ZoneOccupancy.INSIDE
    assert pending.candidate_elapsed_s == 0.2
    assert entered.previous is ZoneOccupancy.UNKNOWN
    assert entered.current is ZoneOccupancy.INSIDE
    assert entered.changed is True


def test_door_exit_requires_continuous_outside_dwell() -> None:
    zone = _zone(enter_dwell_s=0.0)
    zone.update(0.0, [_box()])

    pending = zone.update(0.2, [_box(cx=0.8)])[1]
    cancelled = zone.update(0.4, [_box()])[1]
    zone.update(0.6, [_box(cx=0.8)])
    exited = zone.update(1.0, [_box(cx=0.8)])[1]

    assert pending.current is ZoneOccupancy.INSIDE
    assert pending.candidate is ZoneOccupancy.OUTSIDE
    assert cancelled.current is ZoneOccupancy.INSIDE
    assert cancelled.candidate is None
    assert exited.previous is ZoneOccupancy.INSIDE
    assert exited.current is ZoneOccupancy.OUTSIDE
    assert exited.changed is True


def test_unreliable_clipped_box_preserves_state_and_cancels_candidate() -> None:
    zone = _zone(enter_dwell_s=0.0)
    zone.update(0.0, [_box()])
    zone.update(0.2, [_box(cx=0.8)])

    clipped = zone.update(0.4, [_box(cx=0.8, clipped=True)])[1]
    after = zone.update(0.6, [_box(cx=0.8)])[1]

    assert clipped.reliable is False
    assert clipped.raw_occupancy is None
    assert clipped.current is ZoneOccupancy.INSIDE
    assert clipped.candidate is None
    assert after.candidate_elapsed_s == 0.0


def test_bottom_center_and_box_center_can_classify_differently() -> None:
    bottom_zone = _zone(enter_dwell_s=0.0)
    center_zone = _zone(anchor=ZoneAnchor.BOX_CENTER, enter_dwell_s=0.0, exit_dwell_s=0.0)
    box = _box(cy=0.05, height=0.4)

    bottom = bottom_zone.update(0.0, [box])[1]
    center = center_zone.update(0.0, [box])[1]

    assert bottom.anchor == pytest.approx((0.5, 0.25))
    assert bottom.current is ZoneOccupancy.INSIDE
    assert center.anchor == pytest.approx((0.5, 0.05))
    assert center.current is ZoneOccupancy.OUTSIDE


def test_polygon_boundary_counts_as_inside() -> None:
    zone = _zone(anchor=ZoneAnchor.BOX_CENTER, enter_dwell_s=0.0)

    snapshot = zone.update(0.0, [_box(cx=0.35, cy=0.4)])[1]

    assert snapshot.current is ZoneOccupancy.INSIDE


def test_track_handoff_preserves_stable_and_pending_zone_history() -> None:
    zone = _zone(enter_dwell_s=0.0)
    zone.update(0.0, [_box(track_id=1)])
    zone.update(0.2, [_box(track_id=1, cx=0.8)])

    transferred = zone.handoff(1, 2)
    exited = zone.update(0.6, [_box(track_id=2, cx=0.8)])[2]

    assert transferred is True
    assert exited.previous is ZoneOccupancy.INSIDE
    assert exited.current is ZoneOccupancy.OUTSIDE
    assert exited.changed is True


def test_stale_track_reappears_with_unknown_history() -> None:
    zone = _zone(enter_dwell_s=0.0, stale_after_s=0.5)
    zone.update(0.0, [_box()])
    zone.update(0.6, [])

    reappeared = zone.update(0.8, [_box(cx=0.8)])[1]

    assert reappeared.previous is ZoneOccupancy.UNKNOWN
    assert reappeared.current is ZoneOccupancy.UNKNOWN
    assert reappeared.candidate is ZoneOccupancy.OUTSIDE


def test_debug_snapshot_is_json_serializable_shape() -> None:
    zone = _zone(enter_dwell_s=0.0)

    debug = zone.update(0.0, [_box()])[1].to_debug_dict()

    assert debug == {
        "zone_name": "doorway",
        "zone_previous": "UNKNOWN",
        "zone_occupancy": "INSIDE",
        "zone_change": "UNKNOWN->INSIDE",
        "zone_raw_occupancy": "INSIDE",
        "zone_anchor": [0.5, pytest.approx(0.6)],
        "zone_reliable": True,
        "zone_candidate": None,
        "zone_candidate_elapsed_s": None,
    }
