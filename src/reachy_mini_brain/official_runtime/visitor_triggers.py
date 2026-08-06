"""Pure visitor scale classification and greet/goodbye trigger logic."""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Presence(str, Enum):
    ABSENT = "ABSENT"
    PRESENT = "PRESENT"


class Proximity(str, Enum):
    UNKNOWN = "UNKNOWN"
    FAR = "FAR"
    NEAR = "NEAR"


class Motion(str, Enum):
    UNKNOWN = "UNKNOWN"
    APPROACHING = "APPROACHING"
    STATIONARY = "STATIONARY"
    RECEDING = "RECEDING"


@dataclass(frozen=True)
class TrackBox:
    """Normalized geometry for one ByteTrack person box."""

    track_id: int
    area: float
    cx: float
    cy: float
    height: float
    clipped: bool
    box: tuple[int, int, int, int]
    tracking_source: str = "byte_track"
    source_track_id: int | None = None


@dataclass(frozen=True)
class HeightSignalConfig:
    median_window: int = 3
    ema_alpha: float = 0.5
    slope_window_s: float = 1.0
    min_slope_span_s: float = 0.5
    motion_persist_s: float = 0.2
    approach_slope: float = 0.20
    recede_slope: float = -0.20
    reset_gap_s: float = 1.5
    max_samples: int = 30


@dataclass(frozen=True)
class HeightSignalSnapshot:
    raw_height: float | None
    median_height: float | None
    filtered_height: float | None
    log_slope: float | None
    motion: Motion
    sample_valid: bool
    last_valid_ts: float | None
    last_approaching_ts: float | None
    last_receding_ts: float | None

    def has_recent(self, motion: Motion, ts: float, max_age_s: float) -> bool:
        evidence_ts = {
            Motion.APPROACHING: self.last_approaching_ts,
            Motion.RECEDING: self.last_receding_ts,
        }.get(motion)
        return evidence_ts is not None and 0.0 <= ts - evidence_ts <= max_age_s


class HeightMotionSignal:
    """Filter timestamped height and classify its relative trend."""

    def __init__(self, config: HeightSignalConfig | None = None) -> None:
        self.config = config or HeightSignalConfig()
        if self.config.median_window < 1:
            raise ValueError("median_window must be at least 1")
        if self.config.max_samples < 3:
            raise ValueError("max_samples must be at least 3")
        if not 0.0 < self.config.ema_alpha <= 1.0:
            raise ValueError("ema_alpha must be in (0, 1]")
        self._raw: deque[float] = deque(maxlen=self.config.median_window)
        self._trend: deque[tuple[float, float]] = deque(maxlen=self.config.max_samples)
        self._ema: float | None = None
        self._median: float | None = None
        self._slope: float | None = None
        self._motion = Motion.UNKNOWN
        self._motion_candidate: Motion | None = None
        self._motion_candidate_since: float | None = None
        self._last_valid_ts: float | None = None
        self._last_approaching_ts: float | None = None
        self._last_receding_ts: float | None = None

    def update(self, ts: float, raw_height: float, *, valid: bool) -> HeightSignalSnapshot:
        if not valid or not math.isfinite(raw_height) or raw_height <= 0.0:
            return self.snapshot(raw_height=raw_height, sample_valid=False)
        if self._last_valid_ts is not None and ts <= self._last_valid_ts:
            return self.snapshot(raw_height=raw_height, sample_valid=False)
        if self._last_valid_ts is not None and ts - self._last_valid_ts > self.config.reset_gap_s:
            self._reset_series()

        self._last_valid_ts = ts
        self._raw.append(raw_height)
        self._median = float(statistics.median(self._raw))
        if self._ema is None:
            self._ema = self._median
        else:
            alpha = self.config.ema_alpha
            self._ema = alpha * self._median + (1.0 - alpha) * self._ema
        self._trend.append((ts, self._ema))
        cutoff = ts - self.config.slope_window_s
        while len(self._trend) > 2 and self._trend[1][0] < cutoff:
            self._trend.popleft()

        self._slope = self._fit_log_slope()
        raw_motion = self._classify_slope(self._slope)
        self._update_stable_motion(raw_motion, ts)
        if self._motion is Motion.APPROACHING:
            self._last_approaching_ts = ts
        elif self._motion is Motion.RECEDING:
            self._last_receding_ts = ts
        return self.snapshot(raw_height=raw_height, sample_valid=True)

    def snapshot(
        self,
        *,
        raw_height: float | None = None,
        sample_valid: bool = False,
    ) -> HeightSignalSnapshot:
        return HeightSignalSnapshot(
            raw_height=raw_height,
            median_height=self._median,
            filtered_height=self._ema,
            log_slope=self._slope,
            motion=self._motion,
            sample_valid=sample_valid,
            last_valid_ts=self._last_valid_ts,
            last_approaching_ts=self._last_approaching_ts,
            last_receding_ts=self._last_receding_ts,
        )

    def _reset_series(self) -> None:
        self._raw.clear()
        self._trend.clear()
        self._ema = None
        self._median = None
        self._slope = None
        self._motion = Motion.UNKNOWN
        self._motion_candidate = None
        self._motion_candidate_since = None
        self._last_approaching_ts = None
        self._last_receding_ts = None

    def _fit_log_slope(self) -> float | None:
        if len(self._trend) < 3:
            return None
        span = self._trend[-1][0] - self._trend[0][0]
        if span < self.config.min_slope_span_s:
            return None
        t0 = self._trend[0][0]
        points = [(ts - t0, math.log(max(height, 1e-6))) for ts, height in self._trend]
        mean_t = sum(ts for ts, _ in points) / len(points)
        mean_h = sum(height for _, height in points) / len(points)
        denominator = sum((ts - mean_t) ** 2 for ts, _ in points)
        if denominator <= 0.0:
            return None
        numerator = sum((ts - mean_t) * (height - mean_h) for ts, height in points)
        return numerator / denominator

    def _classify_slope(self, slope: float | None) -> Motion:
        if slope is None:
            return Motion.UNKNOWN
        if slope >= self.config.approach_slope:
            return Motion.APPROACHING
        if slope <= self.config.recede_slope:
            return Motion.RECEDING
        return Motion.STATIONARY

    def _update_stable_motion(self, candidate: Motion, ts: float) -> None:
        if candidate is Motion.UNKNOWN:
            return
        if candidate is self._motion:
            self._motion_candidate = None
            self._motion_candidate_since = None
            return
        if candidate is not self._motion_candidate:
            self._motion_candidate = candidate
            self._motion_candidate_since = ts
            if self.config.motion_persist_s > 0.0:
                return
        assert self._motion_candidate_since is not None
        if ts - self._motion_candidate_since >= self.config.motion_persist_s:
            self._motion = candidate
            self._motion_candidate = None
            self._motion_candidate_since = None


@dataclass(frozen=True)
class PresenceSnapshot:
    previous: Presence
    current: Presence
    changed: bool


class PresenceClassifier:
    def __init__(self, *, confirm_s: float = 0.0, absent_reset_s: float = 8.0) -> None:
        self.confirm_s = confirm_s
        self.absent_reset_s = absent_reset_s
        self.current = Presence.ABSENT
        self._present_since: float | None = None
        self._absent_since: float | None = None

    def update(self, ts: float, detected: bool) -> PresenceSnapshot:
        previous = self.current
        if detected:
            self._absent_since = None
            if self.current is Presence.ABSENT:
                if self._present_since is None:
                    self._present_since = ts
                if ts - self._present_since >= self.confirm_s:
                    self.current = Presence.PRESENT
            else:
                self._present_since = None
        else:
            self._present_since = None
            if self.current is Presence.PRESENT:
                if self._absent_since is None:
                    self._absent_since = ts
                if ts - self._absent_since >= self.absent_reset_s:
                    self.current = Presence.ABSENT
                    self._absent_since = None
        return PresenceSnapshot(previous=previous, current=self.current, changed=previous is not self.current)


@dataclass(frozen=True)
class ProximitySnapshot:
    previous: Proximity
    current: Proximity
    changed: bool


class ProximityClassifier:
    """Classify stable proximity with an UNKNOWN state and threshold hysteresis."""

    def __init__(
        self,
        *,
        near_enter_height: float = 0.55,
        near_exit_height: float = 0.45,
        persist_s: float = 0.4,
    ) -> None:
        if near_exit_height >= near_enter_height:
            raise ValueError("near_exit_height must be lower than near_enter_height")
        self.near_enter_height = near_enter_height
        self.near_exit_height = near_exit_height
        self.persist_s = persist_s
        self.current = Proximity.UNKNOWN
        self._candidate: Proximity | None = None
        self._candidate_since: float | None = None

    def update(
        self,
        ts: float,
        *,
        filtered_height: float | None,
        valid: bool,
        strong_near_clipping: bool = False,
    ) -> ProximitySnapshot:
        previous = self.current
        proposed: Proximity | None = None
        if valid and filtered_height is not None:
            if self.current is Proximity.UNKNOWN:
                if filtered_height >= self.near_enter_height:
                    proposed = Proximity.NEAR
                elif filtered_height <= self.near_exit_height:
                    proposed = Proximity.FAR
            elif self.current is Proximity.FAR and filtered_height >= self.near_enter_height:
                proposed = Proximity.NEAR
            elif self.current is Proximity.NEAR and filtered_height <= self.near_exit_height:
                proposed = Proximity.FAR
        elif self.current is Proximity.UNKNOWN and strong_near_clipping:
            proposed = Proximity.NEAR

        if proposed is None or proposed is self.current:
            self._candidate = None
            self._candidate_since = None
        elif proposed is not self._candidate:
            self._candidate = proposed
            self._candidate_since = ts
            if self.persist_s <= 0.0:
                self.current = proposed
                self._candidate = None
                self._candidate_since = None
        else:
            assert self._candidate_since is not None
            if ts - self._candidate_since >= self.persist_s:
                self.current = proposed
                self._candidate = None
                self._candidate_since = None
        return ProximitySnapshot(previous=previous, current=self.current, changed=previous is not self.current)

    def snapshot(self) -> ProximitySnapshot:
        return ProximitySnapshot(previous=self.current, current=self.current, changed=False)

    def reset(self) -> None:
        self.current = Proximity.UNKNOWN
        self._candidate = None
        self._candidate_since = None


@dataclass(frozen=True)
class TriggerDecision:
    kind: str
    reason: str


class VisitorTriggerEvaluator:
    """Combine independent classifications without introducing lifecycle states."""

    def __init__(
        self,
        *,
        motion_evidence_window_s: float = 1.0,
        goodbye_confirm_s: float = 0.6,
        goodbye_candidate_timeout_s: float = 2.0,
        goodbye_additional_shrink: float = 0.03,
    ) -> None:
        self.motion_evidence_window_s = motion_evidence_window_s
        self.goodbye_confirm_s = goodbye_confirm_s
        self.goodbye_candidate_timeout_s = goodbye_candidate_timeout_s
        if not 0.0 <= goodbye_additional_shrink < 1.0:
            raise ValueError("goodbye_additional_shrink must be in [0, 1)")
        self.goodbye_additional_shrink = goodbye_additional_shrink
        self.reset()

    def reset(self) -> None:
        self.greet_fired = False
        self.goodbye_fired = False
        self.goodbye_pending_since: float | None = None
        self.goodbye_pending_height: float | None = None
        self._near_crossing_ts: float | None = None
        self._far_crossing_ts: float | None = None
        self.last_decision: str | None = None

    def update(
        self,
        ts: float,
        *,
        presence: Presence,
        proximity: ProximitySnapshot,
        motion: HeightSignalSnapshot,
        target_reliable: bool,
    ) -> list[TriggerDecision]:
        self.last_decision = None
        if presence is Presence.ABSENT:
            return []

        if proximity.changed:
            if proximity.previous is Proximity.FAR and proximity.current is Proximity.NEAR:
                self._near_crossing_ts = ts
            elif proximity.previous is Proximity.NEAR and proximity.current is Proximity.FAR:
                self._far_crossing_ts = ts

        decisions: list[TriggerDecision] = []
        if (
            not self.greet_fired
            and proximity.current is Proximity.NEAR
            and self._is_recent(self._near_crossing_ts, ts)
            and motion.has_recent(Motion.APPROACHING, ts, self.motion_evidence_window_s)
        ):
            self.greet_fired = True
            self._near_crossing_ts = None
            self.last_decision = "greet_confirmed"
            decisions.append(TriggerDecision(kind="approach", reason="far_to_near_while_approaching"))

        if (
            not self.goodbye_fired
            and self.goodbye_pending_since is None
            and proximity.current is Proximity.FAR
            and self._is_recent(self._far_crossing_ts, ts)
            and motion.has_recent(Motion.RECEDING, ts, self.motion_evidence_window_s)
        ):
            self.goodbye_pending_since = ts
            self.goodbye_pending_height = motion.filtered_height
            self._far_crossing_ts = None
            self.last_decision = "goodbye_pending"

        pending_since = self.goodbye_pending_since
        if pending_since is not None:
            if target_reliable and motion.sample_valid:
                if proximity.current is Proximity.NEAR or motion.motion in {
                    Motion.STATIONARY,
                    Motion.APPROACHING,
                }:
                    self.goodbye_pending_since = None
                    self.goodbye_pending_height = None
                    self.last_decision = "goodbye_cancelled"
                elif (
                    proximity.current is Proximity.FAR
                    and motion.motion is Motion.RECEDING
                    and ts - pending_since >= self.goodbye_confirm_s
                    and self._continued_shrink(motion.filtered_height)
                ):
                    self.goodbye_pending_since = None
                    self.goodbye_pending_height = None
                    self.goodbye_fired = True
                    self.last_decision = "goodbye_confirmed"
                    decisions.append(TriggerDecision(kind="depart", reason="far_and_still_receding"))
            if (
                self.goodbye_pending_since is not None
                and ts - pending_since >= self.goodbye_candidate_timeout_s
            ):
                self.goodbye_pending_since = None
                self.goodbye_pending_height = None
                self.last_decision = "goodbye_expired"
        return decisions

    def _is_recent(self, event_ts: float | None, ts: float) -> bool:
        return event_ts is not None and 0.0 <= ts - event_ts <= self.motion_evidence_window_s

    def _continued_shrink(self, current_height: float | None) -> bool:
        if current_height is None or self.goodbye_pending_height is None:
            return False
        return current_height <= self.goodbye_pending_height * (1.0 - self.goodbye_additional_shrink)


@dataclass(frozen=True)
class VisitorTriggerConfig:
    """Untuned defaults; captured evaluation must set production values."""

    present_area_frac: float = 0.03
    present_confirm_s: float = 0.0
    absent_reset_s: float = 8.0
    near_enter_height: float = 0.55
    near_exit_height: float = 0.45
    proximity_persist_s: float = 0.4
    clipped_near_height: float = 0.85
    handoff_grace_s: float = 1.0
    handoff_center_distance: float = 0.25
    handoff_height_ratio: float = 2.0
    motion_evidence_window_s: float = 1.0
    goodbye_confirm_s: float = 0.6
    goodbye_candidate_timeout_s: float = 2.0
    goodbye_additional_shrink: float = 0.03
    height_signal: HeightSignalConfig = field(default_factory=HeightSignalConfig)


class VisitorTriggerEngine:
    """Own per-track signals, conservative target continuity, and visit trigger latches."""

    def __init__(self, config: VisitorTriggerConfig | None = None) -> None:
        self.config = config or VisitorTriggerConfig()
        self._signals: dict[int, HeightMotionSignal] = {}
        self._snapshots: dict[int, HeightSignalSnapshot] = {}
        self._presence = PresenceClassifier(
            confirm_s=self.config.present_confirm_s,
            absent_reset_s=self.config.absent_reset_s,
        )
        self._proximity = ProximityClassifier(
            near_enter_height=self.config.near_enter_height,
            near_exit_height=self.config.near_exit_height,
            persist_s=self.config.proximity_persist_s,
        )
        self._triggers = VisitorTriggerEvaluator(
            motion_evidence_window_s=self.config.motion_evidence_window_s,
            goodbye_confirm_s=self.config.goodbye_confirm_s,
            goodbye_candidate_timeout_s=self.config.goodbye_candidate_timeout_s,
            goodbye_additional_shrink=self.config.goodbye_additional_shrink,
        )
        self._active_track_id: int | None = None
        self._last_active_box: TrackBox | None = None
        self._last_active_ts: float | None = None
        self._handoff = False
        self._handoff_from_track_id: int | None = None
        self._last_decision: str | None = None
        self._presence_change: str | None = None
        self._proximity_change: str | None = None
        self._motion_change: str | None = None
        self._last_active_motion = Motion.UNKNOWN
        self._scene_person_count = 0
        self._target_visible = False

    @property
    def active_track_id(self) -> int | None:
        return self._active_track_id

    @property
    def debug_state(self) -> dict[str, Any]:
        active = self._snapshots.get(self._active_track_id) if self._active_track_id is not None else None
        retained_motion = active.motion if active is not None else Motion.UNKNOWN
        observed_motion = retained_motion if self._target_visible else Motion.UNKNOWN
        retained_presence = self._presence.current
        observed_presence = Presence.PRESENT if self._target_visible else Presence.ABSENT
        retained_proximity = self._proximity.current
        observed_proximity = retained_proximity if self._target_visible else Proximity.UNKNOWN
        return {
            "active_track_id": self._active_track_id,
            "target_visible": self._target_visible,
            "handoff": self._handoff,
            "handoff_from_track_id": self._handoff_from_track_id,
            "presence": retained_presence.value,
            "visit_presence": retained_presence.value,
            "observed_presence": observed_presence.value,
            "retained_presence": retained_presence.value,
            "proximity": retained_proximity.value,
            "observed_proximity": observed_proximity.value,
            "retained_proximity": retained_proximity.value,
            "motion": observed_motion.value,
            "observed_motion": observed_motion.value,
            "retained_motion": retained_motion.value,
            "height_filtered": _rounded(active.filtered_height if active is not None else None),
            "log_height_slope": _rounded(active.log_slope if active is not None else None),
            "greet": self._triggers.greet_fired,
            "depart": self._triggers.goodbye_fired,
            "goodbye_pending": self._triggers.goodbye_pending_since is not None,
            "trigger_decision": self._last_decision,
            "presence_change": self._presence_change,
            "proximity_change": self._proximity_change,
            "motion_change": self._motion_change,
            "scene_person_count": self._scene_person_count,
        }

    def update(
        self,
        ts: float,
        boxes: list[TrackBox],
        *,
        scene_person_count: int | None = None,
    ) -> list[dict[str, Any]]:
        self._handoff = False
        self._handoff_from_track_id = None
        self._presence_change = None
        self._proximity_change = None
        self._motion_change = None
        self._scene_person_count = len(boxes) if scene_person_count is None else scene_person_count
        by_id = {box.track_id: box for box in boxes}
        target = self._select_target(ts, boxes, by_id, self._scene_person_count)
        self._target_visible = target is not None
        if target is not None and self._handoff:
            self._transfer_signal_history(target.track_id)
        for box in boxes:
            signal = self._signals.setdefault(box.track_id, HeightMotionSignal(self.config.height_signal))
            self._snapshots[box.track_id] = signal.update(ts, box.height, valid=not box.clipped)

        presence = self._presence.update(ts, target is not None)
        if presence.changed:
            self._presence_change = f"{presence.previous.value}->{presence.current.value}"
        if presence.current is Presence.ABSENT:
            self._proximity.reset()
            self._triggers.reset()
            self._last_decision = "visit_reset" if presence.changed else None
            if presence.changed:
                self._signals.clear()
                self._snapshots.clear()
                self._active_track_id = None
                self._last_active_box = None
                self._last_active_ts = None
                self._last_active_motion = Motion.UNKNOWN
                self._handoff_from_track_id = None
            return []

        if target is None:
            proximity = self._proximity.snapshot()
            motion = HeightSignalSnapshot(
                raw_height=None,
                median_height=None,
                filtered_height=None,
                log_slope=None,
                motion=Motion.UNKNOWN,
                sample_valid=False,
                last_valid_ts=None,
                last_approaching_ts=None,
                last_receding_ts=None,
            )
            self._triggers.update(
                ts,
                presence=presence.current,
                proximity=proximity,
                motion=motion,
                target_reliable=False,
            )
            self._last_decision = self._triggers.last_decision
            return []

        signal = self._snapshots[target.track_id]
        if signal.motion is not self._last_active_motion:
            self._motion_change = f"{self._last_active_motion.value}->{signal.motion.value}"
            self._last_active_motion = signal.motion
        proximity = self._proximity.update(
            ts,
            filtered_height=signal.filtered_height,
            valid=signal.sample_valid,
            strong_near_clipping=target.clipped and target.height >= self.config.clipped_near_height,
        )
        if proximity.changed:
            self._proximity_change = f"{proximity.previous.value}->{proximity.current.value}"
        decisions = self._triggers.update(
            ts,
            presence=presence.current,
            proximity=proximity,
            motion=signal,
            target_reliable=True,
        )
        self._last_decision = self._triggers.last_decision
        return [self._event(decision, target, signal) for decision in decisions]

    def track_debug(self, track_id: int) -> dict[str, Any]:
        signal = self._snapshots.get(track_id)
        return {
            "active": track_id == self._active_track_id,
            "height_filtered": _rounded(signal.filtered_height if signal is not None else None),
            "log_height_slope": _rounded(signal.log_slope if signal is not None else None),
            "motion": signal.motion.value if signal is not None else Motion.UNKNOWN.value,
            "presence": self._presence.current.value,
            "proximity": (
                self._proximity.current.value
                if track_id == self._active_track_id
                else Proximity.UNKNOWN.value
            ),
            "handoff": self._handoff and track_id == self._active_track_id,
            "goodbye_pending": self._triggers.goodbye_pending_since is not None,
            "trigger_decision": self._last_decision if track_id == self._active_track_id else None,
            "presence_change": self._presence_change if track_id == self._active_track_id else None,
            "proximity_change": self._proximity_change if track_id == self._active_track_id else None,
            "motion_change": self._motion_change if track_id == self._active_track_id else None,
            "near_enter_height": self.config.near_enter_height,
            "near_exit_height": self.config.near_exit_height,
            "approach_slope": self.config.height_signal.approach_slope,
            "recede_slope": self.config.height_signal.recede_slope,
        }

    def _select_target(
        self,
        ts: float,
        boxes: list[TrackBox],
        by_id: dict[int, TrackBox],
        scene_person_count: int,
    ) -> TrackBox | None:
        credible = [box for box in boxes if box.area >= self.config.present_area_frac]
        if self._active_track_id is not None and self._active_track_id in by_id:
            target = by_id[self._active_track_id]
            if target.area >= self.config.present_area_frac:
                self._remember_target(target, ts)
                return target

        # A reception visit is scene-level state, not a ByteTrack identity. If exactly
        # one person remains visible, continue the active visit across tracker ID churn.
        if self._active_track_id is not None and scene_person_count == 1 and len(credible) == 1:
            target = credible[0]
            previous_track_id = self._active_track_id
            self._handoff = target.track_id != previous_track_id
            if self._handoff:
                self._handoff_from_track_id = previous_track_id
            self._active_track_id = target.track_id
            self._remember_target(target, ts)
            return target

        if self._last_active_box is not None and self._last_active_ts is not None:
            if ts - self._last_active_ts <= self.config.handoff_grace_s:
                compatible = [box for box in credible if self._compatible_handoff(self._last_active_box, box)]
                if compatible:
                    target = min(compatible, key=lambda box: self._handoff_score(self._last_active_box, box))
                    previous_track_id = self._active_track_id
                    self._handoff = previous_track_id is not None and target.track_id != previous_track_id
                    if self._handoff:
                        self._handoff_from_track_id = previous_track_id
                    self._active_track_id = target.track_id
                    self._remember_target(target, ts)
                    return target

        if self._active_track_id is None and self._presence.current is Presence.ABSENT and credible:
            target = max(credible, key=lambda box: box.area)
            self._active_track_id = target.track_id
            self._remember_target(target, ts)
            return target
        return None

    def _transfer_signal_history(self, target_track_id: int) -> None:
        previous_track_id = self._handoff_from_track_id
        if previous_track_id is None or previous_track_id == target_track_id:
            return
        previous_signal = self._signals.pop(previous_track_id, None)
        self._snapshots.pop(previous_track_id, None)
        if previous_signal is None:
            return
        self._signals[target_track_id] = previous_signal
        self._snapshots.pop(target_track_id, None)

    def _compatible_handoff(self, previous: TrackBox, current: TrackBox) -> bool:
        center_distance = math.hypot(current.cx - previous.cx, current.cy - previous.cy)
        if center_distance > self.config.handoff_center_distance:
            return False
        low = min(previous.height, current.height)
        high = max(previous.height, current.height)
        return low > 0.0 and high / low <= self.config.handoff_height_ratio

    @staticmethod
    def _handoff_score(previous: TrackBox, current: TrackBox) -> float:
        center = math.hypot(current.cx - previous.cx, current.cy - previous.cy)
        scale = abs(math.log(max(current.height, 1e-6) / max(previous.height, 1e-6)))
        return center + scale

    def _remember_target(self, target: TrackBox, ts: float) -> None:
        self._last_active_box = target
        self._last_active_ts = ts

    def _event(
        self,
        decision: TriggerDecision,
        target: TrackBox,
        signal: HeightSignalSnapshot,
    ) -> dict[str, Any]:
        return {
            "kind": decision.kind,
            "id": target.track_id,
            "area": round(target.area, 3),
            "cx": round(target.cx, 2),
            "cy": round(target.cy, 2),
            "height": round(target.height, 3),
            "height_filtered": _rounded(signal.filtered_height),
            "log_height_slope": _rounded(signal.log_slope),
            "presence": self._presence.current.value,
            "proximity": self._proximity.current.value,
            "motion": signal.motion.value,
            "reason": decision.reason,
            "tracking_source": target.tracking_source,
            "source_track_id": target.source_track_id,
        }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
