"""Door-ordered greet and goodbye policy decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .door_observation import DoorFrameObservation, DoorPersonInteraction


DOOR_ORDERED_TRIGGER_CONTRACT = "door_ordered_v1"
TEMPORAL_GREET_DIRECT_GOODBYE_CONTRACT = "temporal_greet_direct_goodbye_v2"
PRESENCE_OVERLAP_DIRECT_GOODBYE_CONTRACT = "presence_overlap_direct_goodbye_v3"
DOOR_TRIGGER_CONTRACTS = (
    DOOR_ORDERED_TRIGGER_CONTRACT,
    TEMPORAL_GREET_DIRECT_GOODBYE_CONTRACT,
    PRESENCE_OVERLAP_DIRECT_GOODBYE_CONTRACT,
)


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
    trigger_contract: str = DOOR_ORDERED_TRIGGER_CONTRACT
    greet_door_lookback_s: float = 2.0
    greet_door_lookahead_s: float = 2.5
    greet_presence_overlap_window_s: float = 1.5
    greet_overlap_consecutive_observations: int = 4
    greet_presence_rearm_observations: int = 2

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
        if self.trigger_contract not in DOOR_TRIGGER_CONTRACTS:
            choices = ", ".join(DOOR_TRIGGER_CONTRACTS)
            raise ValueError(f"unknown door trigger contract {self.trigger_contract!r}; choose one of: {choices}")
        if self.greet_door_lookback_s < 0.0 or self.greet_door_lookahead_s <= 0.0:
            raise ValueError("greet door lookback must be non-negative and lookahead must be positive")
        if self.greet_presence_overlap_window_s <= 0.0:
            raise ValueError("greet presence-overlap window must be positive")
        if self.greet_overlap_consecutive_observations < 1:
            raise ValueError("greet consecutive overlap observations must be positive")
        if self.greet_presence_rearm_observations < 1:
            raise ValueError("greet presence rearm observations must be positive")

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
    trigger_contract: str
    eligible_person_appearance: bool
    last_door_moving_ts: float | None
    greet_candidate_track_ids: tuple[str, ...]
    greet_overlap_streak: int
    greet_overlap_required: int
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


@dataclass
class _PresenceOverlapCandidate:
    since: float
    overlap_streak: int = 0


class DoorPolicyTriggerEngine:
    """Require door/person evidence in opposite temporal order for greet and goodbye."""

    def __init__(self, settings: DoorPolicySettings | None = None) -> None:
        self.settings = settings or DoorPolicySettings()
        self._previous_door_state = "UNKNOWN"
        self._last_person_ts: float | None = None
        self._interaction_inside: dict[str, bool] = {}
        self._distance_inside: dict[str, bool] = {}
        self._last_interaction_distance: dict[str, float] = {}
        self._last_door_moving_ts: float | None = None
        self._presence_active_track_ids: set[str] = set()
        self._presence_missing_observations: dict[str, int] = {}
        self._presence_overlap_candidates: dict[str, _PresenceOverlapCandidate] = {}
        self._frame_greet_overlap_streak = 0
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
        self._frame_greet_overlap_streak = 0

        self._expire_greet_candidate(frame_ts)

        if observed_present:
            self._last_person_ts = frame_ts

        eligible, ineligible_reasons = self._partition_interactions(observation.interactions)
        inside_ids, crossing_ids = self._update_interactions(eligible)
        eligible_person_appearance = bool(eligible) and not retained_before
        if ineligible_reasons:
            self._greet_candidate_since = None
            if not inside_ids:
                self._goodbye_candidate_since = None
                self._goodbye_candidate_last_supported_ts = None
        self._update_goodbye_support(frame_ts, supported=bool(inside_ids))

        if self.settings.trigger_contract == PRESENCE_OVERLAP_DIRECT_GOODBYE_CONTRACT:
            crossing_ids = self._update_distance_crossings(eligible)
            decision, reason, eligible_person_appearance = (
                self._update_presence_overlap_direct_goodbye(
                    observation,
                    resolved_decision_ts,
                    eligible=eligible,
                    ineligible_reasons=ineligible_reasons,
                    distance_crossing_ids=crossing_ids,
                    events=events,
                )
            )
        elif self.settings.trigger_contract == TEMPORAL_GREET_DIRECT_GOODBYE_CONTRACT:
            crossing_ids = self._update_distance_crossings(eligible)
            if observation.state == "MOVING":
                self._last_door_moving_ts = frame_ts
            decision, reason = self._update_temporal_greet_direct_goodbye(
                observation,
                resolved_decision_ts,
                eligible_person_appearance=eligible_person_appearance,
                eligible_track_ids=tuple(item.track_id for item in eligible),
                distance_crossing_ids=crossing_ids,
                events=events,
            )
        else:
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
            trigger_contract=self.settings.trigger_contract,
            eligible_person_appearance=eligible_person_appearance,
            last_door_moving_ts=self._last_door_moving_ts,
            greet_candidate_track_ids=tuple(sorted(self._presence_overlap_candidates)),
            greet_overlap_streak=max(
                [
                    self._frame_greet_overlap_streak,
                    *(
                    candidate.overlap_streak
                    for candidate in self._presence_overlap_candidates.values()
                    ),
                ]
            ),
            greet_overlap_required=self.settings.greet_overlap_consecutive_observations,
            greet_candidate_armed=(
                self._greet_candidate_since is not None
                or bool(self._presence_overlap_candidates)
            ),
            goodbye_candidate_armed=self._goodbye_candidate_since is not None,
            greet_candidate_since=self._greet_candidate_since,
            goodbye_candidate_since=self._goodbye_candidate_since,
            goodbye_candidate_last_supported_ts=self._goodbye_candidate_last_supported_ts,
            decision=decision,
            reason=reason,
            events=tuple(events),
        )

    def _update_presence_overlap_direct_goodbye(
        self,
        observation: DoorFrameObservation,
        decision_ts: float,
        *,
        eligible: tuple[DoorPersonInteraction, ...],
        ineligible_reasons: dict[str, str],
        distance_crossing_ids: list[str],
        events: list[dict[str, Any]],
    ) -> tuple[str, str, bool]:
        frame_ts = observation.frame_ts
        observed_ids = {person.track_id for person in observation.people}
        new_presence_ids = self._update_presence_episodes(observed_ids)
        eligible_by_id = {interaction.track_id: interaction for interaction in eligible}
        eligible_person_appearance = False

        for track_id in new_presence_ids:
            if track_id not in eligible_by_id:
                continue
            self._presence_overlap_candidates[track_id] = _PresenceOverlapCandidate(
                since=frame_ts
            )
            eligible_person_appearance = True

        decision = "greet_armed" if eligible_person_appearance else "none"
        reason = "eligible_person_appearance" if eligible_person_appearance else "no_transition"
        for track_id, candidate in tuple(self._presence_overlap_candidates.items()):
            if frame_ts - candidate.since > self.settings.greet_presence_overlap_window_s:
                self._presence_overlap_candidates.pop(track_id, None)
                continue
            interaction = eligible_by_id.get(track_id)
            if track_id in ineligible_reasons:
                self._presence_overlap_candidates.pop(track_id, None)
                continue
            if interaction is None:
                candidate.overlap_streak = 0
                continue
            if interaction.overlap_ratio >= self.settings.interaction_overlap_enter:
                candidate.overlap_streak += 1
            else:
                candidate.overlap_streak = 0
            self._frame_greet_overlap_streak = max(
                self._frame_greet_overlap_streak,
                candidate.overlap_streak,
            )
            if (
                candidate.overlap_streak
                < self.settings.greet_overlap_consecutive_observations
            ):
                continue
            event = self._event("approach", observation, decision_ts, track_id=track_id)
            event["overlap_streak"] = candidate.overlap_streak
            event["overlap_required"] = self.settings.greet_overlap_consecutive_observations
            events.append(event)
            self._clear_candidates()
            return (
                "greet_triggered",
                "person_appearance_with_consecutive_overlap",
                eligible_person_appearance,
            )

        self._sync_presence_greet_candidate_since()
        if distance_crossing_ids and not events:
            track_id = distance_crossing_ids[0]
            events.append(self._event("depart", observation, decision_ts, track_id=track_id))
            self._clear_candidates()
            return "goodbye_triggered", "person_distance_crossing", eligible_person_appearance
        return decision, reason, eligible_person_appearance

    def _update_presence_episodes(self, observed_ids: set[str]) -> tuple[str, ...]:
        for track_id in tuple(self._presence_active_track_ids):
            if track_id in observed_ids:
                self._presence_missing_observations[track_id] = 0
                continue
            missing = self._presence_missing_observations.get(track_id, 0) + 1
            self._presence_missing_observations[track_id] = missing
            candidate = self._presence_overlap_candidates.get(track_id)
            if candidate is not None:
                candidate.overlap_streak = 0
            if missing < self.settings.greet_presence_rearm_observations:
                continue
            self._presence_active_track_ids.discard(track_id)
            self._presence_missing_observations.pop(track_id, None)
            self._presence_overlap_candidates.pop(track_id, None)
            self._interaction_inside.pop(track_id, None)
            self._distance_inside.pop(track_id, None)
            self._last_interaction_distance.pop(track_id, None)

        new_ids = tuple(sorted(observed_ids - self._presence_active_track_ids))
        for track_id in observed_ids:
            self._presence_active_track_ids.add(track_id)
            self._presence_missing_observations[track_id] = 0
        return new_ids

    def _sync_presence_greet_candidate_since(self) -> None:
        self._greet_candidate_since = min(
            (candidate.since for candidate in self._presence_overlap_candidates.values()),
            default=None,
        )

    def _update_temporal_greet_direct_goodbye(
        self,
        observation: DoorFrameObservation,
        decision_ts: float,
        *,
        eligible_person_appearance: bool,
        eligible_track_ids: tuple[str, ...],
        distance_crossing_ids: list[str],
        events: list[dict[str, Any]],
    ) -> tuple[str, str]:
        frame_ts = observation.frame_ts
        decision = "none"
        reason = "no_transition"

        if eligible_person_appearance:
            self._greet_candidate_since = frame_ts
            decision = "greet_armed"
            reason = "eligible_person_appearance"

        recent_door_movement = (
            self._last_door_moving_ts is not None
            and self._greet_candidate_since is not None
            and self._last_door_moving_ts >= (
                self._greet_candidate_since - self.settings.greet_door_lookback_s
            )
            and self._last_door_moving_ts <= (
                self._greet_candidate_since + self.settings.greet_door_lookahead_s
            )
        )
        if self._greet_candidate_since is not None and recent_door_movement and eligible_track_ids:
            track_id = eligible_track_ids[0]
            events.append(self._event("approach", observation, decision_ts, track_id=track_id))
            self._clear_candidates()
            return "greet_triggered", "person_appearance_near_door_movement"

        if distance_crossing_ids and not events:
            track_id = distance_crossing_ids[0]
            events.append(self._event("depart", observation, decision_ts, track_id=track_id))
            self._clear_candidates()
            return "goodbye_triggered", "person_distance_crossing"

        return decision, reason

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
        self._presence_overlap_candidates.clear()
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
            self._distance_inside.pop(interaction.track_id, None)
            self._last_interaction_distance.pop(interaction.track_id, None)
        return tuple(eligible), ineligible

    def _update_distance_crossings(
        self,
        interactions: tuple[DoorPersonInteraction, ...],
    ) -> list[str]:
        crossing_ids: list[str] = []
        for interaction in interactions:
            track_id = interaction.track_id
            previous_inside = self._distance_inside.get(track_id)
            previous_distance = self._last_interaction_distance.get(track_id)
            if previous_inside is True:
                inside = interaction.normalized_distance <= self.settings.interaction_distance_exit
            elif previous_inside is False:
                inside = interaction.normalized_distance <= self.settings.interaction_distance_enter
            elif interaction.normalized_distance > self.settings.interaction_distance_exit:
                inside = False
            elif interaction.normalized_distance <= self.settings.interaction_distance_enter:
                inside = True
            else:
                self._last_interaction_distance[track_id] = interaction.normalized_distance
                continue
            if (
                previous_inside is False
                and inside
                and previous_distance is not None
                and interaction.normalized_distance < previous_distance
            ):
                crossing_ids.append(track_id)
            self._distance_inside[track_id] = inside
            self._last_interaction_distance[track_id] = interaction.normalized_distance
        return crossing_ids

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

    def _event(
        self,
        kind: str,
        observation: DoorFrameObservation,
        decision_ts: float,
        *,
        track_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "source": (
                "door_policy_v2"
                if self.settings.trigger_contract != DOOR_ORDERED_TRIGGER_CONTRACT
                else "door_policy_v1"
            ),
            "trigger_contract": self.settings.trigger_contract,
            "frame_index": observation.frame_index,
            "frame_ts": observation.frame_ts,
            "decision_ts": decision_ts,
            "decision_latency_s": decision_ts - observation.frame_ts,
            "door_state": observation.state,
            "track_id": track_id,
        }
