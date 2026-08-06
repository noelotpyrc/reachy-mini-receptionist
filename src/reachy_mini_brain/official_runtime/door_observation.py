"""Offline door-state and door/person observation primitives."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray


Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class DoorObserverSettings:
    motion_enter_threshold: float = 0.10
    motion_exit_threshold: float = 0.035
    stable_dwell_s: float = 0.8
    geometry_hold_s: float = 1.0
    semantic_stale_s: float = 2.5
    retained_box_alpha: float = 0.45
    pixel_difference_threshold: int = 18
    relative_motion_enabled: bool = False
    relative_motion_gain: float = 8.0
    relative_motion_min_door_inliers: int = 8
    relative_motion_min_background_inliers: int = 12
    relative_motion_min_door_coverage: float = 0.10
    relative_motion_max_fb_error_px: float = 1.5
    relative_motion_ransac_threshold_px: float = 2.0
    relative_motion_person_padding_ratio: float = 0.10

    def __post_init__(self) -> None:
        if not 0.0 <= self.motion_exit_threshold < self.motion_enter_threshold <= 1.0:
            raise ValueError("motion thresholds must satisfy 0 <= exit < enter <= 1")
        if self.stable_dwell_s < 0.0 or self.geometry_hold_s < 0.0:
            raise ValueError("dwell and hold durations must be non-negative")
        if self.semantic_stale_s <= 0.0:
            raise ValueError("semantic_stale_s must be positive")
        if not 0.0 < self.retained_box_alpha <= 1.0:
            raise ValueError("retained_box_alpha must be in (0, 1]")
        if self.pixel_difference_threshold <= 0:
            raise ValueError("pixel_difference_threshold must be positive")
        if self.relative_motion_gain < 0.0:
            raise ValueError("relative_motion_gain must be non-negative")
        if self.relative_motion_min_door_inliers < 3:
            raise ValueError("relative_motion_min_door_inliers must be at least 3")
        if self.relative_motion_min_background_inliers < 3:
            raise ValueError("relative_motion_min_background_inliers must be at least 3")
        if not 0.0 <= self.relative_motion_min_door_coverage <= 1.0:
            raise ValueError("relative_motion_min_door_coverage must be in [0, 1]")
        if self.relative_motion_max_fb_error_px <= 0.0:
            raise ValueError("relative_motion_max_fb_error_px must be positive")
        if self.relative_motion_ransac_threshold_px <= 0.0:
            raise ValueError("relative_motion_ransac_threshold_px must be positive")
        if self.relative_motion_person_padding_ratio < 0.0:
            raise ValueError("relative_motion_person_padding_ratio must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DoorDetectionInput:
    confidence: float
    box: Box


@dataclass(frozen=True)
class PersonBoxInput:
    track_id: str
    box: Box


@dataclass(frozen=True)
class DoorPersonInteraction:
    track_id: str
    overlap_ratio: float
    normalized_distance: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DoorFrameObservation:
    frame_index: int
    frame_ts: float
    frame_width: int
    frame_height: int
    state: str
    motion_score: float
    motion_enter_threshold: float
    motion_exit_threshold: float
    geometry_change_score: float
    relative_door_motion_score: float
    relative_door_displacement: float
    relative_motion_valid: bool
    door_flow_tracked_points: int
    door_flow_inlier_ratio: float
    door_flow_coverage: float
    background_flow_tracked_points: int
    background_flow_inlier_ratio: float
    global_frame_change_score: float
    semantic_updated: bool
    raw_door_detections: tuple[DoorDetectionInput, ...]
    retained_box: Box | None
    retained_box_normalized: Box | None
    people: tuple[PersonBoxInput, ...]
    interactions: tuple[DoorPersonInteraction, ...]
    semantic_completed_ts: float | None = None
    semantic_inference_latency_ms: float | None = None
    semantic_source_age_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _RelativeMotionEvidence:
    score: float
    displacement: float
    valid: bool
    door_tracked_points: int
    door_inlier_ratio: float
    door_coverage: float
    background_tracked_points: int
    background_inlier_ratio: float

    @classmethod
    def empty(cls) -> _RelativeMotionEvidence:
        return cls(
            score=0.0,
            displacement=0.0,
            valid=False,
            door_tracked_points=0,
            door_inlier_ratio=0.0,
            door_coverage=0.0,
            background_tracked_points=0,
            background_inlier_ratio=0.0,
        )


class DoorMotionObserver:
    """Derive a retained door box and STABLE/MOVING/UNKNOWN state."""

    def __init__(self, settings: DoorObserverSettings | None = None) -> None:
        self.settings = settings or DoorObserverSettings()
        self._state = "UNKNOWN"
        self._retained_box: Box | None = None
        self._last_observed_box: Box | None = None
        self._last_semantic_ts: float | None = None
        self._geometry_score = 0.0
        self._geometry_score_ts: float | None = None
        self._previous_gray: NDArray[np.uint8] | None = None
        self._previous_retained_box: Box | None = None
        self._previous_people: tuple[PersonBoxInput, ...] = ()
        self._low_since: float | None = None

    def update(
        self,
        *,
        frame_index: int,
        frame_ts: float,
        frame_bgr: NDArray[np.uint8],
        door_detections: list[DoorDetectionInput] | None,
        people: list[PersonBoxInput],
        semantic_completed_ts: float | None = None,
        semantic_inference_latency_ms: float | None = None,
    ) -> DoorFrameObservation:
        height, width = frame_bgr.shape[:2]
        semantic_updated = door_detections is not None
        raw_detections = tuple(door_detections or ())
        if semantic_updated:
            self._update_semantic(frame_ts, list(raw_detections), width=width, height=height)

        retained_box = self._current_retained_box(frame_ts)
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        frame_change_score = _frame_change_score(
            self._previous_gray,
            gray,
            threshold=self.settings.pixel_difference_threshold,
        )
        relative_motion = (
            self._relative_door_motion(
                gray,
                retained_box=retained_box,
                people=people,
            )
            if self.settings.relative_motion_enabled
            else _RelativeMotionEvidence.empty()
        )
        self._previous_gray = gray
        self._previous_retained_box = retained_box
        self._previous_people = tuple(people)
        geometry_score = self._current_geometry_score(frame_ts)
        motion_score = min(1.0, max(geometry_score, relative_motion.score))
        reliable = retained_box is not None
        self._update_state(frame_ts, motion_score=motion_score, reliable=reliable)

        interactions = tuple(
            _person_interaction(person, retained_box, width=width, height=height)
            for person in people
            if retained_box is not None
        )
        normalized = _normalize_box(retained_box, width=width, height=height) if retained_box else None
        return DoorFrameObservation(
            frame_index=frame_index,
            frame_ts=frame_ts,
            frame_width=width,
            frame_height=height,
            state=self._state,
            motion_score=motion_score,
            motion_enter_threshold=self.settings.motion_enter_threshold,
            motion_exit_threshold=self.settings.motion_exit_threshold,
            geometry_change_score=geometry_score,
            relative_door_motion_score=relative_motion.score,
            relative_door_displacement=relative_motion.displacement,
            relative_motion_valid=relative_motion.valid,
            door_flow_tracked_points=relative_motion.door_tracked_points,
            door_flow_inlier_ratio=relative_motion.door_inlier_ratio,
            door_flow_coverage=relative_motion.door_coverage,
            background_flow_tracked_points=relative_motion.background_tracked_points,
            background_flow_inlier_ratio=relative_motion.background_inlier_ratio,
            global_frame_change_score=frame_change_score,
            semantic_updated=semantic_updated,
            raw_door_detections=raw_detections,
            retained_box=retained_box,
            retained_box_normalized=normalized,
            people=tuple(people),
            interactions=interactions,
            semantic_completed_ts=semantic_completed_ts if semantic_updated else None,
            semantic_inference_latency_ms=(
                semantic_inference_latency_ms if semantic_updated else None
            ),
            semantic_source_age_s=(
                max(0.0, semantic_completed_ts - frame_ts)
                if semantic_updated and semantic_completed_ts is not None
                else None
            ),
        )

    def _update_semantic(
        self,
        frame_ts: float,
        detections: list[DoorDetectionInput],
        *,
        width: int,
        height: int,
    ) -> None:
        valid = [item for item in detections if _valid_box(item.box, width=width, height=height)]
        if not valid:
            return
        observed = _associated_box(valid, self._retained_box)
        if self._last_observed_box is not None:
            self._geometry_score = 1.0 - _box_iou(self._last_observed_box, observed)
            self._geometry_score_ts = frame_ts
        else:
            self._geometry_score = 0.0
            self._geometry_score_ts = frame_ts
        self._last_observed_box = observed
        self._retained_box = (
            observed
            if self._retained_box is None
            else _blend_box(self._retained_box, observed, self.settings.retained_box_alpha)
        )
        self._last_semantic_ts = frame_ts

    def _current_retained_box(self, frame_ts: float) -> Box | None:
        if self._retained_box is None or self._last_semantic_ts is None:
            return None
        if frame_ts - self._last_semantic_ts > self.settings.semantic_stale_s:
            return None
        return self._retained_box

    def _current_geometry_score(self, frame_ts: float) -> float:
        if self._geometry_score_ts is None:
            return 0.0
        if frame_ts - self._geometry_score_ts > self.settings.geometry_hold_s:
            return 0.0
        return self._geometry_score

    def _relative_door_motion(
        self,
        gray: NDArray[np.uint8],
        *,
        retained_box: Box | None,
        people: list[PersonBoxInput],
    ) -> _RelativeMotionEvidence:
        previous = self._previous_gray
        previous_box = self._previous_retained_box
        if (
            previous is None
            or previous.shape != gray.shape
            or previous_box is None
            or retained_box is None
        ):
            return _RelativeMotionEvidence.empty()

        people_to_mask = (*self._previous_people, *people)
        door_mask = _door_feature_mask(
            gray.shape,
            previous_box,
            people_to_mask,
            person_padding_ratio=self.settings.relative_motion_person_padding_ratio,
        )
        background_mask = _background_feature_mask(
            gray.shape,
            previous_box,
            people_to_mask,
            person_padding_ratio=self.settings.relative_motion_person_padding_ratio,
        )
        door_previous, door_current = _tracked_features(
            previous,
            gray,
            door_mask,
            max_fb_error_px=self.settings.relative_motion_max_fb_error_px,
        )
        background_previous, background_current = _tracked_features(
            previous,
            gray,
            background_mask,
            max_fb_error_px=self.settings.relative_motion_max_fb_error_px,
        )
        door_transform, door_inliers = _fit_affine(
            door_previous,
            door_current,
            partial=False,
            ransac_threshold_px=self.settings.relative_motion_ransac_threshold_px,
        )
        background_transform, background_inliers = _fit_affine(
            background_previous,
            background_current,
            partial=True,
            ransac_threshold_px=self.settings.relative_motion_ransac_threshold_px,
        )
        door_tracked = len(door_previous)
        background_tracked = len(background_previous)
        door_inlier_count = int(door_inliers.sum())
        background_inlier_count = int(background_inliers.sum())
        door_coverage = _point_coverage(door_previous[door_inliers], previous_box)
        door_inlier_ratio = door_inlier_count / door_tracked if door_tracked else 0.0
        background_inlier_ratio = (
            background_inlier_count / background_tracked if background_tracked else 0.0
        )
        valid = (
            door_transform is not None
            and background_transform is not None
            and door_inlier_count >= self.settings.relative_motion_min_door_inliers
            and background_inlier_count >= self.settings.relative_motion_min_background_inliers
            and door_coverage >= self.settings.relative_motion_min_door_coverage
        )
        displacement = (
            _relative_transform_displacement(door_transform, background_transform, previous_box)
            if valid
            else 0.0
        )
        return _RelativeMotionEvidence(
            score=min(1.0, displacement * self.settings.relative_motion_gain),
            displacement=displacement,
            valid=valid,
            door_tracked_points=door_tracked,
            door_inlier_ratio=door_inlier_ratio,
            door_coverage=door_coverage,
            background_tracked_points=background_tracked,
            background_inlier_ratio=background_inlier_ratio,
        )

    def _update_state(self, frame_ts: float, *, motion_score: float, reliable: bool) -> None:
        if not reliable:
            self._state = "UNKNOWN"
            self._low_since = None
            return
        if motion_score >= self.settings.motion_enter_threshold:
            self._low_since = None
            self._state = "MOVING"
            return
        if motion_score <= self.settings.motion_exit_threshold:
            if self._low_since is None:
                self._low_since = frame_ts
            if frame_ts - self._low_since >= self.settings.stable_dwell_s:
                self._state = "STABLE"
        else:
            self._low_since = None


def _frame_change_score(
    previous: NDArray[np.uint8] | None,
    current: NDArray[np.uint8],
    *,
    threshold: int,
) -> float:
    if previous is None or previous.shape != current.shape:
        return 0.0
    return float((cv2.absdiff(current, previous) >= threshold).mean())


def _door_feature_mask(
    shape: tuple[int, int],
    door_box: Box,
    people: tuple[PersonBoxInput, ...],
    *,
    person_padding_ratio: float,
) -> NDArray[np.uint8]:
    height, width = shape
    mask = np.zeros(shape, dtype=np.uint8)
    x1, y1, x2, y2 = _integer_box(door_box, width=width, height=height)
    inset = min(6, max(0, (x2 - x1 - 1) // 4), max(0, (y2 - y1 - 1) // 4))
    if x2 - x1 > inset * 2 and y2 - y1 > inset * 2:
        cv2.rectangle(mask, (x1 + inset, y1 + inset), (x2 - inset, y2 - inset), 255, -1)
    _mask_people(mask, people, padding_ratio=person_padding_ratio)
    return mask


def _background_feature_mask(
    shape: tuple[int, int],
    door_box: Box,
    people: tuple[PersonBoxInput, ...],
    *,
    person_padding_ratio: float,
) -> NDArray[np.uint8]:
    height, width = shape
    mask = np.zeros(shape, dtype=np.uint8)
    x1, y1, x2, y2 = _integer_box(door_box, width=width, height=height)
    box_width = x2 - x1
    box_height = y2 - y1
    expand_x = int(round(box_width * 0.75))
    expand_y = int(round(box_height * 0.35))
    cv2.rectangle(
        mask,
        (max(0, x1 - expand_x), max(0, y1 - expand_y)),
        (min(width, x2 + expand_x), min(height, y2 + expand_y)),
        255,
        -1,
    )
    cv2.rectangle(
        mask,
        (max(0, x1 - 8), max(0, y1 - 8)),
        (min(width, x2 + 8), min(height, y2 + 8)),
        0,
        -1,
    )
    _mask_people(mask, people, padding_ratio=person_padding_ratio)
    return mask


def _mask_people(
    mask: NDArray[np.uint8],
    people: tuple[PersonBoxInput, ...],
    *,
    padding_ratio: float,
) -> None:
    height, width = mask.shape
    for person in people:
        x1, y1, x2, y2 = _integer_box(person.box, width=width, height=height)
        pad_x = max(12, int(round((x2 - x1) * padding_ratio)))
        pad_y = max(8, int(round((y2 - y1) * padding_ratio * 0.5)))
        cv2.rectangle(
            mask,
            (max(0, x1 - pad_x), max(0, y1 - pad_y)),
            (min(width, x2 + pad_x), min(height, y2 + pad_y)),
            0,
            -1,
        )


def _tracked_features(
    previous: NDArray[np.uint8],
    current: NDArray[np.uint8],
    mask: NDArray[np.uint8],
    *,
    max_fb_error_px: float,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    empty = np.empty((0, 2), dtype=np.float32)
    points = cv2.goodFeaturesToTrack(
        previous,
        maxCorners=180,
        qualityLevel=0.01,
        minDistance=7,
        mask=mask,
        blockSize=7,
    )
    if points is None or len(points) < 6:
        return empty, empty
    current_points, forward_status, _ = cv2.calcOpticalFlowPyrLK(
        previous,
        current,
        points,
        None,
        winSize=(21, 21),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 30, 0.01),
    )
    if current_points is None or forward_status is None:
        return empty, empty
    backward_points, backward_status, _ = cv2.calcOpticalFlowPyrLK(
        current,
        previous,
        current_points,
        None,
        winSize=(21, 21),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 30, 0.01),
    )
    if backward_points is None or backward_status is None:
        return empty, empty
    fb_error = np.linalg.norm(backward_points - points, axis=2).reshape(-1)
    valid = (
        (forward_status.reshape(-1) > 0)
        & (backward_status.reshape(-1) > 0)
        & np.isfinite(fb_error)
        & (fb_error <= max_fb_error_px)
    )
    return points[:, 0, :][valid], current_points[:, 0, :][valid]


def _fit_affine(
    previous: NDArray[np.float32],
    current: NDArray[np.float32],
    *,
    partial: bool,
    ransac_threshold_px: float,
) -> tuple[NDArray[np.float64] | None, NDArray[np.bool_]]:
    empty_inliers = np.zeros(len(previous), dtype=bool)
    if len(previous) < 6:
        return None, empty_inliers
    estimator = cv2.estimateAffinePartial2D if partial else cv2.estimateAffine2D
    transform, inliers = estimator(
        previous,
        current,
        method=cv2.RANSAC,
        ransacReprojThreshold=ransac_threshold_px,
        maxIters=2000,
        confidence=0.99,
        refineIters=10,
    )
    if transform is None or inliers is None:
        return None, empty_inliers
    return transform, inliers.reshape(-1).astype(bool)


def _point_coverage(points: NDArray[np.float32], box: Box) -> float:
    if len(points) < 3:
        return 0.0
    hull_area = float(cv2.contourArea(cv2.convexHull(points.astype(np.float32))))
    return min(1.0, max(0.0, hull_area / max(_box_area(box), 1.0)))


def _relative_transform_displacement(
    door_transform: NDArray[np.float64],
    background_transform: NDArray[np.float64],
    door_box: Box,
) -> float:
    x1, y1, x2, y2 = door_box
    sample_points = np.asarray(
        [
            (x1, y1),
            (x2, y1),
            (x1, y2),
            (x2, y2),
            ((x1 + x2) * 0.5, (y1 + y2) * 0.5),
        ],
        dtype=np.float32,
    )
    door_points = cv2.transform(sample_points[None, :, :], door_transform)[0]
    background_points = cv2.transform(sample_points[None, :, :], background_transform)[0]
    displacement = np.linalg.norm(door_points - background_points, axis=1)
    return float(np.median(displacement) / max(math.hypot(_box_width(door_box), _box_height(door_box)), 1.0))


def _associated_box(detections: list[DoorDetectionInput], retained_box: Box | None) -> Box:
    if retained_box is None:
        return max(detections, key=lambda item: item.confidence).box
    retained_center = _box_center(retained_box)
    retained_scale = max(_box_width(retained_box), _box_height(retained_box), 1.0)
    associated = [
        item
        for item in detections
        if _box_iou(item.box, retained_box) >= 0.02
        or math.dist(_box_center(item.box), retained_center) <= retained_scale * 0.75
    ]
    if not associated:
        return max(detections, key=lambda item: item.confidence).box

    def association_score(item: DoorDetectionInput) -> float:
        overlap = _box_iou(item.box, retained_box)
        normalized_distance = math.dist(_box_center(item.box), retained_center) / retained_scale
        return item.confidence + 0.20 * overlap - 0.05 * normalized_distance

    return max(associated, key=association_score).box


def _person_interaction(
    person: PersonBoxInput,
    door_box: Box,
    *,
    width: int,
    height: int,
) -> DoorPersonInteraction:
    intersection = _intersection_area(person.box, door_box)
    person_area = max(_box_area(person.box), 1.0)
    feet = ((_box_center(person.box)[0]), person.box[3])
    distance = _point_box_distance(feet, door_box) / max(math.hypot(width, height), 1.0)
    return DoorPersonInteraction(
        track_id=person.track_id,
        overlap_ratio=min(1.0, max(0.0, intersection / person_area)),
        normalized_distance=max(0.0, distance),
    )


def _valid_box(box: Box, *, width: int, height: int) -> bool:
    return (
        all(math.isfinite(value) for value in box)
        and 0.0 <= box[0] < box[2] <= float(width)
        and 0.0 <= box[1] < box[3] <= float(height)
    )


def _normalize_box(box: Box, *, width: int, height: int) -> Box:
    cx, cy = _box_center(box)
    return (cx / width, cy / height, _box_width(box) / width, _box_height(box) / height)


def _integer_box(box: Box, *, width: int, height: int) -> tuple[int, int, int, int]:
    return (
        max(0, min(width, int(math.floor(box[0])))),
        max(0, min(height, int(math.floor(box[1])))),
        max(0, min(width, int(math.ceil(box[2])))),
        max(0, min(height, int(math.ceil(box[3])))),
    )


def _blend_box(previous: Box, current: Box, alpha: float) -> Box:
    blended = (
        (1.0 - alpha) * old + alpha * new
        for old, new in zip(previous, current, strict=True)
    )
    return tuple(blended)  # type: ignore[return-value]


def _box_iou(left: Box, right: Box) -> float:
    intersection = _intersection_area(left, right)
    union = _box_area(left) + _box_area(right) - intersection
    return intersection / union if union > 0.0 else 0.0


def _intersection_area(left: Box, right: Box) -> float:
    width = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    height = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return width * height


def _box_area(box: Box) -> float:
    return max(0.0, _box_width(box)) * max(0.0, _box_height(box))


def _box_width(box: Box) -> float:
    return box[2] - box[0]


def _box_height(box: Box) -> float:
    return box[3] - box[1]


def _box_center(box: Box) -> tuple[float, float]:
    return ((box[0] + box[2]) * 0.5, (box[1] + box[3]) * 0.5)


def _point_box_distance(point: tuple[float, float], box: Box) -> float:
    x, y = point
    dx = max(box[0] - x, 0.0, x - box[2])
    dy = max(box[1] - y, 0.0, y - box[3])
    return math.hypot(dx, dy)
