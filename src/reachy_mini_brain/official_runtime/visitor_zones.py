"""Tracked polygon-zone occupancy for visitor landmarks such as a doorway."""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .visitor_triggers import TrackBox


class ZoneOccupancy(str, Enum):
    UNKNOWN = "UNKNOWN"
    OUTSIDE = "OUTSIDE"
    INSIDE = "INSIDE"


class ZoneAnchor(str, Enum):
    BOX_CENTER = "BOX_CENTER"
    BOTTOM_CENTER = "BOTTOM_CENTER"


@dataclass(frozen=True)
class TrackedPolygonZoneConfig:
    name: str
    polygon: tuple[tuple[float, float], ...]
    anchor: ZoneAnchor = ZoneAnchor.BOTTOM_CENTER
    enter_dwell_s: float = 0.4
    exit_dwell_s: float = 0.4
    stale_after_s: float = 1.5
    accept_clipped_boxes: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("zone name must not be empty")
        if len(self.polygon) < 3:
            raise ValueError("zone polygon must contain at least three points")
        if self.enter_dwell_s < 0.0 or self.exit_dwell_s < 0.0:
            raise ValueError("zone dwell durations must be non-negative")
        if self.stale_after_s <= 0.0:
            raise ValueError("stale_after_s must be positive")
        for point in self.polygon:
            if len(point) != 2 or not all(math.isfinite(value) for value in point):
                raise ValueError("zone polygon points must be finite x/y pairs")
            if not all(0.0 <= value <= 1.0 for value in point):
                raise ValueError("zone polygon points must use normalized coordinates in [0, 1]")
        contour = np.asarray(self.polygon, dtype=np.float32)
        if abs(float(cv2.contourArea(contour))) <= 1e-8:
            raise ValueError("zone polygon must enclose a non-zero area")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": self.name,
            "polygon": [list(point) for point in self.polygon],
            "anchor": self.anchor.value,
            "enter_dwell_s": self.enter_dwell_s,
            "exit_dwell_s": self.exit_dwell_s,
            "stale_after_s": self.stale_after_s,
            "accept_clipped_boxes": self.accept_clipped_boxes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackedPolygonZoneConfig:
        schema_version = int(data.get("schema_version", 1))
        if schema_version != 1:
            raise ValueError(f"unsupported zone config schema_version: {schema_version}")
        return cls(
            name=str(data["name"]),
            polygon=tuple((float(point[0]), float(point[1])) for point in data["polygon"]),
            anchor=ZoneAnchor(str(data.get("anchor", ZoneAnchor.BOTTOM_CENTER.value))),
            enter_dwell_s=float(data.get("enter_dwell_s", 0.4)),
            exit_dwell_s=float(data.get("exit_dwell_s", 0.4)),
            stale_after_s=float(data.get("stale_after_s", 1.5)),
            accept_clipped_boxes=bool(data.get("accept_clipped_boxes", False)),
        )


@dataclass(frozen=True)
class ZoneSnapshot:
    zone_name: str
    track_id: int
    previous: ZoneOccupancy
    current: ZoneOccupancy
    changed: bool
    raw_occupancy: ZoneOccupancy | None
    anchor: tuple[float, float] | None
    reliable: bool
    candidate: ZoneOccupancy | None
    candidate_elapsed_s: float | None

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "zone_name": self.zone_name,
            "zone_previous": self.previous.value,
            "zone_occupancy": self.current.value,
            "zone_change": f"{self.previous.value}->{self.current.value}" if self.changed else None,
            "zone_raw_occupancy": self.raw_occupancy.value if self.raw_occupancy is not None else None,
            "zone_anchor": list(self.anchor) if self.anchor is not None else None,
            "zone_reliable": self.reliable,
            "zone_candidate": self.candidate.value if self.candidate is not None else None,
            "zone_candidate_elapsed_s": self.candidate_elapsed_s,
        }


@dataclass
class _TrackZoneState:
    current: ZoneOccupancy = ZoneOccupancy.UNKNOWN
    candidate: ZoneOccupancy | None = None
    candidate_since: float | None = None
    last_seen_ts: float | None = None


class TrackedPolygonZone:
    """Classify dwell-qualified polygon occupancy independently for each track."""

    def __init__(self, config: TrackedPolygonZoneConfig) -> None:
        self.config = config
        self._contour = np.asarray(config.polygon, dtype=np.float32)
        self._states: dict[int, _TrackZoneState] = {}

    def update(self, ts: float, boxes: list[TrackBox]) -> dict[int, ZoneSnapshot]:
        if not math.isfinite(ts):
            raise ValueError("zone timestamp must be finite")
        self._expire_stale_tracks(ts)

        snapshots: dict[int, ZoneSnapshot] = {}
        for box in boxes:
            state = self._states.setdefault(box.track_id, _TrackZoneState())
            if state.last_seen_ts is not None and ts < state.last_seen_ts:
                raise ValueError(f"zone timestamp moved backwards for track {box.track_id}")
            state.last_seen_ts = ts
            snapshots[box.track_id] = self._update_track(ts, box, state)
        return snapshots

    def handoff(self, from_track_id: int, to_track_id: int) -> bool:
        """Move stable and pending zone history across an accepted tracker-ID handoff."""

        if from_track_id == to_track_id:
            return from_track_id in self._states
        previous = self._states.get(from_track_id)
        if previous is None or to_track_id in self._states:
            return False
        self._states[to_track_id] = previous
        del self._states[from_track_id]
        return True

    def reset(self, track_id: int | None = None) -> None:
        if track_id is None:
            self._states.clear()
        else:
            self._states.pop(track_id, None)

    def _update_track(self, ts: float, box: TrackBox, state: _TrackZoneState) -> ZoneSnapshot:
        previous = state.current
        anchor = self._anchor(box)
        reliable = anchor is not None and (self.config.accept_clipped_boxes or not box.clipped)
        raw_occupancy: ZoneOccupancy | None = None
        if reliable:
            assert anchor is not None
            raw_occupancy = (
                ZoneOccupancy.INSIDE
                if cv2.pointPolygonTest(self._contour, anchor, False) >= 0.0
                else ZoneOccupancy.OUTSIDE
            )
            self._apply_candidate(ts, raw_occupancy, state)
        else:
            state.candidate = None
            state.candidate_since = None

        elapsed = ts - state.candidate_since if state.candidate_since is not None else None
        return ZoneSnapshot(
            zone_name=self.config.name,
            track_id=box.track_id,
            previous=previous,
            current=state.current,
            changed=previous is not state.current,
            raw_occupancy=raw_occupancy,
            anchor=anchor,
            reliable=reliable,
            candidate=state.candidate,
            candidate_elapsed_s=round(elapsed, 3) if elapsed is not None else None,
        )

    def _apply_candidate(
        self,
        ts: float,
        observed: ZoneOccupancy,
        state: _TrackZoneState,
    ) -> None:
        if observed is state.current:
            state.candidate = None
            state.candidate_since = None
            return
        if observed is not state.candidate:
            state.candidate = observed
            state.candidate_since = ts

        assert state.candidate_since is not None
        dwell_s = self.config.enter_dwell_s if observed is ZoneOccupancy.INSIDE else self.config.exit_dwell_s
        if ts - state.candidate_since + 1e-9 >= dwell_s:
            state.current = observed
            state.candidate = None
            state.candidate_since = None

    def _anchor(self, box: TrackBox) -> tuple[float, float] | None:
        if self.config.anchor is ZoneAnchor.BOX_CENTER:
            point = (box.cx, box.cy)
        else:
            point = (box.cx, box.cy + box.height / 2.0)
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in point):
            return None
        return point

    def _expire_stale_tracks(self, ts: float) -> None:
        stale_ids = [
            track_id
            for track_id, state in self._states.items()
            if state.last_seen_ts is not None and ts - state.last_seen_ts > self.config.stale_after_s
        ]
        for track_id in stale_ids:
            del self._states[track_id]


def load_polygon_zone_config(path: str | Path) -> TrackedPolygonZoneConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("zone config must be a JSON object")
    return TrackedPolygonZoneConfig.from_dict(data)
