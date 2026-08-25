"""Door-ordered greet and goodbye policy decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .door_observation import DoorFrameObservation, DoorPersonInteraction


@dataclass(frozen=True)
class DoorPolicySettings:
    """Versioned thresholds for the first door-anchored policy profile."""

    interaction_distance_enter: float = 0.06
    interaction_distance_exit: float = 0.08
    interaction_overlap_enter: float = 0.10
    interaction_overlap_exit: float = 0.05
    person_retention_s: float = 0.75
    greet_candidate_timeout_s: float = 4.0
    goodbye_candidate_timeout_s: float = 4.0
    interaction_eligibility_enabled: bool = True
    interaction_person_max_area_ratio: float = 0.40
    interaction_person_boundary_margin_ratio: float = 0.01

    def __post_init__(self) -> None:
        if not 0.0 <= self.interaction_distance_enter < self.interaction_distance_exit:
            raise ValueError("distance thresholds must satisfy 0 <= enter < exit")
        if not 0.0 <= self.interaction_overlap_exit < self.interaction_overlap_enter <= 1.0:
            raise ValueError("overlap thresholds must satisfy 0 <= exit < enter <= 1")
        if self.person_retention_s < 0.0:
            raise ValueError("person_retention_s must be non-negative")
        if self.greet_candidate_timeout_s <= 0.0 or self.goodbye_candidate_timeout_s <= 0.0:
            raise ValueError("candidate timeouts must be positive")
        if not 0.0 < self.interaction_person_max_area_ratio <= 1.0:
            raise ValueError("interaction person maximum area ratio must be in (0, 1]")
        if not 0.0 <= self.interaction_person_boundary_margin_ratio <= 0.5:
            raise ValueError("interaction person boundary margin ratio must be in [0, 0.5]")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DoorPolicyFrameObservation:
    frame_index: int
    frame_ts: float
    decision_ts: float
    decision_latency_s: float
    door_state: str
    door_moving_edge: bool
    observed_presence: str
    retained_presence: str
    interaction_inside_track_ids: tuple[str, ...]
    interaction_crossing_track_ids: tuple[str, ...]
    interaction_ineligible_track_ids: tuple[str, ...]
    interaction_ineligible_reasons: dict[str, str]
    interaction_distance_enter: float
    interaction_overlap_enter: float
    interaction_person_max_area_ratio: float
    interaction_person_boundary_margin_ratio: float
    greet_candidate_armed: bool
    goodbye_candidate_armed: bool
    greet_candidate_since: float | None
    goodbye_candidate_since: float | None
    goodbye_candidate_last_supported_ts: float | None
    decision: str
    reason: str
    events: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DoorPolicyTriggerEngine:
    """Require door/person evidence in opposite temporal order for greet and goodbye."""

    def __init__(self, settings: DoorPolicySettings | None = None) -> None:
        self.settings = settings or DoorPolicySettings()
        self._previous_door_state = "UNKNOWN"
        self._last_person_ts: float | None = None
        self._interaction_inside: dict[str, bool] = {}
        self._greet_candidate_since: float | None = None
        self._goodbye_candidate_since: float | None = None
        self._goodbye_candidate_last_supported_ts: float | None = None

    def update(
        self,
        observation: DoorFrameObservation,
        *,
        decision_ts: float | None = None,
    ) -> DoorPolicyFrameObservation:
        frame_ts = observation.frame_ts
        resolved_decision_ts = frame_ts if decision_ts is None else max(frame_ts, decision_ts)
        observed_present = bool(observation.people)
        retained_before = self._retained_present(frame_ts)
        moving_edge = observation.state == "MOVING" and self._previous_door_state != "MOVING"
        decision = "none"
        reason = "no_transition"
        events: list[dict[str, Any]] = []

        self._expire_greet_candidate(frame_ts)

        if observed_present:
            self._last_person_ts = frame_ts

        eligible, ineligible_reasons = self._partition_interactions(observation.interactions)
        inside_ids, crossing_ids = self._update_interactions(eligible)
        if ineligible_reasons:
            self._greet_candidate_since = None
            if not inside_ids:
                self._goodbye_candidate_since = None
                self._goodbye_candidate_last_supported_ts = None
        self._update_goodbye_support(frame_ts, supported=bool(inside_ids))

        # A goodbye candidate must predate the confirming door movement. A distance
        # crossing and MOVING observation on the same source frame is ambiguous.
        if moving_edge and self._goodbye_candidate_since is not None:
            if self._goodbye_candidate_since < frame_ts:
                events.append(self._event("depart", observation, resolved_decision_ts))
                self._clear_candidates()
                decision = "goodbye_triggered"
                reason = "interaction_then_door_moving"
        elif moving_edge and not retained_before and not observed_present:
            self._greet_candidate_since = frame_ts
            decision = "greet_armed"
            reason = "door_moving_without_retained_person"

        if self._greet_candidate_since is not None and inside_ids and not events:
            if self._greet_candidate_since < frame_ts:
                track_id = inside_ids[0]
                events.append(self._event("approach", observation, resolved_decision_ts, track_id=track_id))
                self._clear_candidates()
                decision = "greet_triggered"
                reason = "door_moving_then_person_interaction"

        if crossing_ids and self._greet_candidate_since is None and not events:
            self._goodbye_candidate_since = frame_ts
            self._goodbye_candidate_last_supported_ts = frame_ts
            decision = "goodbye_armed"
            reason = "person_interaction_crossing"

        retained_present = self._retained_present(frame_ts)
        self._previous_door_state = observation.state
        return DoorPolicyFrameObservation(
            frame_index=observation.frame_index,
            frame_ts=frame_ts,
            decision_ts=resolved_decision_ts,
            decision_latency_s=resolved_decision_ts - frame_ts,
            door_state=observation.state,
            door_moving_edge=moving_edge,
            observed_presence="PRESENT" if observed_present else "ABSENT",
            retained_presence="PRESENT" if retained_present else "ABSENT",
            interaction_inside_track_ids=tuple(inside_ids),
            interaction_crossing_track_ids=tuple(crossing_ids),
            interaction_ineligible_track_ids=tuple(ineligible_reasons),
            interaction_ineligible_reasons=ineligible_reasons,
            interaction_distance_enter=self.settings.interaction_distance_enter,
            interaction_overlap_enter=self.settings.interaction_overlap_enter,
            interaction_person_max_area_ratio=self.settings.interaction_person_max_area_ratio,
            interaction_person_boundary_margin_ratio=(
                self.settings.interaction_person_boundary_margin_ratio
            ),
            greet_candidate_armed=self._greet_candidate_since is not None,
            goodbye_candidate_armed=self._goodbye_candidate_since is not None,
            greet_candidate_since=self._greet_candidate_since,
            goodbye_candidate_since=self._goodbye_candidate_since,
            goodbye_candidate_last_supported_ts=self._goodbye_candidate_last_supported_ts,
            decision=decision,
            reason=reason,
            events=tuple(events),
        )

    def _retained_present(self, frame_ts: float) -> bool:
        return (
            self._last_person_ts is not None
            and frame_ts - self._last_person_ts <= self.settings.person_retention_s
        )

    def _expire_greet_candidate(self, frame_ts: float) -> None:
        if (
            self._greet_candidate_since is not None
            and frame_ts - self._greet_candidate_since > self.settings.greet_candidate_timeout_s
        ):
            self._greet_candidate_since = None

    def _update_goodbye_support(self, frame_ts: float, *, supported: bool) -> None:
        if self._goodbye_candidate_since is None:
            return
        if supported:
            self._goodbye_candidate_last_supported_ts = frame_ts
            return
        if (
            self._goodbye_candidate_last_supported_ts is not None
            and frame_ts - self._goodbye_candidate_last_supported_ts
            > self.settings.goodbye_candidate_timeout_s
        ):
            self._goodbye_candidate_since = None
            self._goodbye_candidate_last_supported_ts = None

    def _clear_candidates(self) -> None:
        self._greet_candidate_since = None
        self._goodbye_candidate_since = None
        self._goodbye_candidate_last_supported_ts = None

    def _update_interactions(
        self,
        interactions: tuple[DoorPersonInteraction, ...],
    ) -> tuple[list[str], list[str]]:
        inside_ids: list[str] = []
        crossing_ids: list[str] = []
        for interaction in interactions:
            track_id = interaction.track_id
            previous = self._interaction_inside.get(track_id)
            inside = self._classify_interaction(interaction, previous=previous)
            self._interaction_inside[track_id] = inside
            if inside:
                inside_ids.append(track_id)
            if previous is False and inside:
                crossing_ids.append(track_id)

        # Keep a missing track's last classification through normal detection gaps.
        # Logical-track expiry is handled by candidate timeout and visit reset upstream.
        return inside_ids, crossing_ids

    def _partition_interactions(
        self,
        interactions: tuple[DoorPersonInteraction, ...],
    ) -> tuple[tuple[DoorPersonInteraction, ...], dict[str, str]]:
        if not self.settings.interaction_eligibility_enabled:
            return interactions, {}
        eligible: list[DoorPersonInteraction] = []
        ineligible: dict[str, str] = {}
        for interaction in interactions:
            reason = self._interaction_ineligibility_reason(interaction)
            if reason is None:
                eligible.append(interaction)
                continue
            ineligible[interaction.track_id] = reason
            self._interaction_inside[interaction.track_id] = False
        return tuple(eligible), ineligible

    def _interaction_ineligibility_reason(
        self,
        interaction: DoorPersonInteraction,
    ) -> str | None:
        if interaction.person_area_ratio > self.settings.interaction_person_max_area_ratio:
            return "person_box_oversized"
        if (
            interaction.person_boundary_clearance_ratio
            <= self.settings.interaction_person_boundary_margin_ratio
        ):
            return "person_box_boundary_clipped"
        return None

    def _classify_interaction(
        self,
        interaction: DoorPersonInteraction,
        *,
        previous: bool | None,
    ) -> bool:
        if previous:
            return (
                interaction.normalized_distance <= self.settings.interaction_distance_exit
                or interaction.overlap_ratio >= self.settings.interaction_overlap_exit
            )
        return (
            interaction.normalized_distance <= self.settings.interaction_distance_enter
            or interaction.overlap_ratio >= self.settings.interaction_overlap_enter
        )

    @staticmethod
    def _event(
        kind: str,
        observation: DoorFrameObservation,
        decision_ts: float,
        *,
        track_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "source": "door_policy_v1",
            "frame_index": observation.frame_index,
            "frame_ts": observation.frame_ts,
            "decision_ts": decision_ts,
            "decision_latency_s": decision_ts - observation.frame_ts,
            "door_state": observation.state,
            "track_id": track_id,
        }
