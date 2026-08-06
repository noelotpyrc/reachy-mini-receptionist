"""Versioned, JSON-serializable observations for vision replay diagnostics."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Mapping


VISION_OBSERVATION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DetectionObservation:
    detection_index: int
    class_id: int | None
    class_name: str
    confidence: float | None
    box: tuple[float, float, float, float]
    center: tuple[float, float]
    bottom_center: tuple[float, float]
    possible_duplicate: bool = False
    duplicate_of_detection_index: int | None = None


@dataclass(frozen=True)
class TrackObservation:
    logical_track_id: str
    track_id: int
    source_track_id: int | None
    tracking_source: str
    box: tuple[float, float, float, float]
    center: tuple[float, float]
    bottom_center: tuple[float, float]
    previous_anchor: tuple[float, float] | None
    displacement: tuple[float, float] | None
    velocity: tuple[float, float] | None
    track_age_s: float
    visible_sample_count: int
    trail: tuple[tuple[float, float, float], ...]
    area: float
    height: float
    height_filtered: float | None
    height_slope: float | None
    clipped: bool
    height_reliable: bool | None
    active: bool
    handoff_from_track_id: int | None
    motion: str
    zone: dict[str, Any] | None = None


@dataclass(frozen=True)
class VisionObservation:
    frame_index: int
    frame_ts: float
    timestamp_source: str
    frame_width: int
    frame_height: int
    mode: str
    run_id: str | None
    detector: dict[str, Any]
    visitor_profile: dict[str, Any]
    movement: dict[str, Any]
    zone_config: dict[str, Any] | None
    detections: tuple[DetectionObservation, ...] = ()
    tracks: tuple[TrackObservation, ...] = ()
    scene: dict[str, Any] = field(default_factory=dict)
    events: tuple[dict[str, Any], ...] = ()
    schema_version: int = VISION_OBSERVATION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VisionObservation:
        """Load a known observation while ignoring additive fields from newer schemas."""

        detections = tuple(
            DetectionObservation(
                detection_index=int(item["detection_index"]),
                class_id=_optional_int(item.get("class_id")),
                class_name=str(item.get("class_name", "person")),
                confidence=_optional_float(item.get("confidence")),
                box=_float_tuple(item["box"], 4),
                center=_float_tuple(item["center"], 2),
                bottom_center=_float_tuple(item["bottom_center"], 2),
                possible_duplicate=bool(item.get("possible_duplicate", False)),
                duplicate_of_detection_index=_optional_int(item.get("duplicate_of_detection_index")),
            )
            for item in data.get("detections", ())
        )
        tracks = tuple(_track_from_dict(item) for item in data.get("tracks", ()))
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            frame_index=int(data["frame_index"]),
            frame_ts=float(data["frame_ts"]),
            timestamp_source=str(data.get("timestamp_source", "unknown")),
            frame_width=int(data["frame_width"]),
            frame_height=int(data["frame_height"]),
            mode=str(data.get("mode", "unknown")),
            run_id=_optional_str(data.get("run_id")),
            detector=dict(data.get("detector", {})),
            visitor_profile=dict(data.get("visitor_profile", {})),
            movement=dict(data.get("movement", {})),
            zone_config=dict(data["zone_config"]) if data.get("zone_config") is not None else None,
            detections=detections,
            tracks=tracks,
            scene=dict(data.get("scene", {})),
            events=tuple(dict(event) for event in data.get("events", ())),
        )


@dataclass(frozen=True)
class MovementSample:
    previous_anchor: tuple[float, float] | None
    displacement: tuple[float, float] | None
    velocity: tuple[float, float] | None
    track_age_s: float
    visible_sample_count: int
    trail: tuple[tuple[float, float, float], ...]


@dataclass
class _MovementState:
    first_seen_ts: float
    last_seen_ts: float
    last_anchor: tuple[float, float]
    visible_sample_count: int
    trail: deque[tuple[float, float, float]]


class TrackMovementHistory:
    """Derive image-plane displacement, velocity, age, and trails per logical track."""

    def __init__(self, *, trail_window_s: float = 3.0, stale_after_s: float = 2.0) -> None:
        if trail_window_s <= 0.0:
            raise ValueError("trail_window_s must be positive")
        if stale_after_s <= 0.0:
            raise ValueError("stale_after_s must be positive")
        self.trail_window_s = float(trail_window_s)
        self.stale_after_s = float(stale_after_s)
        self._states: dict[str, _MovementState] = {}

    def update_frame(
        self,
        ts: float,
        anchors: Mapping[str, tuple[float, float]],
    ) -> dict[str, MovementSample]:
        if not math.isfinite(ts):
            raise ValueError("movement timestamp must be finite")
        self._expire(ts)
        samples: dict[str, MovementSample] = {}
        for logical_id, anchor in anchors.items():
            if not all(math.isfinite(value) for value in anchor):
                raise ValueError(f"movement anchor must be finite for {logical_id}")
            state = self._states.get(logical_id)
            if state is None:
                state = _MovementState(
                    first_seen_ts=ts,
                    last_seen_ts=ts,
                    last_anchor=anchor,
                    visible_sample_count=1,
                    trail=deque([(ts, anchor[0], anchor[1])]),
                )
                self._states[logical_id] = state
                previous = displacement = velocity = None
            else:
                if ts < state.last_seen_ts:
                    raise ValueError(f"movement timestamp moved backwards for {logical_id}")
                previous = state.last_anchor
                displacement = (anchor[0] - previous[0], anchor[1] - previous[1])
                elapsed = ts - state.last_seen_ts
                velocity = (
                    (displacement[0] / elapsed, displacement[1] / elapsed)
                    if elapsed > 0.0
                    else None
                )
                state.last_seen_ts = ts
                state.last_anchor = anchor
                state.visible_sample_count += 1
                state.trail.append((ts, anchor[0], anchor[1]))
            self._trim_trail(state, ts)
            samples[logical_id] = MovementSample(
                previous_anchor=previous,
                displacement=displacement,
                velocity=velocity,
                track_age_s=max(0.0, ts - state.first_seen_ts),
                visible_sample_count=state.visible_sample_count,
                trail=tuple(state.trail),
            )
        return samples

    def reset(self) -> None:
        self._states.clear()

    def _trim_trail(self, state: _MovementState, ts: float) -> None:
        cutoff = ts - self.trail_window_s
        while len(state.trail) > 1 and state.trail[0][0] < cutoff:
            state.trail.popleft()

    def _expire(self, ts: float) -> None:
        stale_ids = [
            logical_id
            for logical_id, state in self._states.items()
            if ts - state.last_seen_ts > self.stale_after_s
        ]
        for logical_id in stale_ids:
            del self._states[logical_id]


class LogicalTrackResolver:
    """Assign replay-only logical IDs and preserve them across accepted handoffs."""

    def __init__(self) -> None:
        self._source_to_logical: dict[int, str] = {}
        self._next_id = 1

    def apply_handoff(self, from_track_id: int | None, to_track_id: int | None) -> None:
        if from_track_id is None or to_track_id is None or from_track_id == to_track_id:
            return
        logical_id = self.resolve(from_track_id)
        self._source_to_logical[to_track_id] = logical_id

    def resolve(self, track_id: int) -> str:
        logical_id = self._source_to_logical.get(track_id)
        if logical_id is None:
            logical_id = f"track-{self._next_id:04d}"
            self._next_id += 1
            self._source_to_logical[track_id] = logical_id
        return logical_id

    def reset(self) -> None:
        self._source_to_logical.clear()


def detection_observations(persons: Any, frame_wh: tuple[int, int]) -> tuple[DetectionObservation, ...]:
    """Extract exact person detections from supervision-like or mapping inputs."""

    width, height = frame_wh
    rows = list(_iter_detection_rows(persons))
    observations: list[DetectionObservation] = []
    for index, row in enumerate(rows):
        box_value = row.get("box")
        if box_value is None:
            box_value = row.get("xyxy")
        if box_value is None:
            continue
        x1, y1, x2, y2 = _float_tuple(box_value, 4)
        center = (((x1 + x2) / 2.0) / width, ((y1 + y2) / 2.0) / height)
        bottom_center = (((x1 + x2) / 2.0) / width, y2 / height)
        observations.append(
            DetectionObservation(
                detection_index=int(row.get("detection_index", index)),
                class_id=_optional_int(row.get("class_id")),
                class_name=str(row.get("class_name", "person")),
                confidence=_optional_float(row.get("confidence")),
                box=(x1, y1, x2, y2),
                center=center,
                bottom_center=bottom_center,
            )
        )
    duplicate_sources = _possible_duplicate_sources(observations)
    return tuple(
        replace(
            observation,
            possible_duplicate=observation.detection_index in duplicate_sources,
            duplicate_of_detection_index=duplicate_sources.get(observation.detection_index),
        )
        for observation in observations
    )


def _iter_detection_rows(persons: Any) -> Iterable[dict[str, Any]]:
    xyxy = getattr(persons, "xyxy", None)
    if xyxy is not None:
        class_ids = getattr(persons, "class_id", None)
        confidences = getattr(persons, "confidence", None)
        for index in range(len(persons)):
            yield {
                "detection_index": index,
                "box": xyxy[index],
                "class_id": class_ids[index] if class_ids is not None else None,
                "confidence": confidences[index] if confidences is not None else None,
                "class_name": "person",
            }
        return
    for index, item in enumerate(persons):
        if isinstance(item, Mapping):
            yield {"detection_index": index, **item}


def _possible_duplicate_sources(
    detections: list[DetectionObservation],
    *,
    containment_threshold: float = 0.9,
) -> dict[int, int]:
    """Flag likely duplicate boxes for review without changing inference inputs."""

    duplicate_sources: dict[int, int] = {}
    for left_index, left in enumerate(detections):
        for right in detections[left_index + 1 :]:
            if left.class_id != right.class_id or _box_containment(left.box, right.box) < containment_threshold:
                continue
            preferred, duplicate = sorted(
                (left, right),
                key=lambda item: (
                    item.confidence if item.confidence is not None else -1.0,
                    _box_area(item.box),
                ),
                reverse=True,
            )
            duplicate_sources.setdefault(duplicate.detection_index, preferred.detection_index)
    return duplicate_sources


def _box_containment(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    intersection_width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    intersection_height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    smaller_area = min(_box_area(left), _box_area(right))
    if smaller_area <= 0.0:
        return 0.0
    return intersection_width * intersection_height / smaller_area


def _box_area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _track_from_dict(item: Mapping[str, Any]) -> TrackObservation:
    return TrackObservation(
        logical_track_id=str(item["logical_track_id"]),
        track_id=int(item["track_id"]),
        source_track_id=_optional_int(item.get("source_track_id")),
        tracking_source=str(item.get("tracking_source", "unknown")),
        box=_float_tuple(item["box"], 4),
        center=_float_tuple(item["center"], 2),
        bottom_center=_float_tuple(item["bottom_center"], 2),
        previous_anchor=_optional_tuple(item.get("previous_anchor"), 2),
        displacement=_optional_tuple(item.get("displacement"), 2),
        velocity=_optional_tuple(item.get("velocity"), 2),
        track_age_s=float(item.get("track_age_s", 0.0)),
        visible_sample_count=int(item.get("visible_sample_count", 0)),
        trail=tuple(_float_tuple(point, 3) for point in item.get("trail", ())),
        area=float(item.get("area", 0.0)),
        height=float(item.get("height", 0.0)),
        height_filtered=_optional_float(item.get("height_filtered")),
        height_slope=_optional_float(item.get("height_slope")),
        clipped=bool(item.get("clipped", False)),
        height_reliable=_optional_bool(item.get("height_reliable")),
        active=bool(item.get("active", False)),
        handoff_from_track_id=_optional_int(item.get("handoff_from_track_id")),
        motion=str(item.get("motion", "UNKNOWN")),
        zone=dict(item["zone"]) if item.get("zone") is not None else None,
    )


def _float_tuple(value: Any, length: int) -> tuple[Any, ...]:
    result = tuple(float(item) for item in value)
    if len(result) != length:
        raise ValueError(f"expected {length} numeric values, got {len(result)}")
    return result


def _optional_tuple(value: Any, length: int) -> tuple[Any, ...] | None:
    return None if value is None else _float_tuple(value, length)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
