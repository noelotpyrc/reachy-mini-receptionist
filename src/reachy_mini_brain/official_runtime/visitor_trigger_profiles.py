"""Versioned visitor-trigger configurations for live and offline replay."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from .door_observation import DoorObserverSettings
from .door_policy import DoorPolicySettings
from .visitor_triggers import HeightSignalConfig, VisitorTriggerConfig


DEFAULT_VISITOR_TRIGGER_PROFILE = "legacy"
VISITOR_V1_20260802 = "visitor-v1-20260802"
DOOR_V1_20260805 = "door-v1-20260805"
DOOR_V2_20260809 = "door-v2-20260809"
VISITOR_TRIGGER_PROFILE_NAMES = (
    DEFAULT_VISITOR_TRIGGER_PROFILE,
    VISITOR_V1_20260802,
    DOOR_V1_20260805,
    DOOR_V2_20260809,
)


_LEGACY_PARAMETERS: dict[str, Any] = {
    "growth_factor": 1.3,
    "greet_floor": 0.10,
    "min_area_frac": 0.06,
    "depart_factor": 0.6,
    "present_frac": 0.03,
    "reset_absent": 40,
    "history": 30,
}

_VISITOR_V1_CONFIG = VisitorTriggerConfig(
    near_enter_height=0.71,
    near_exit_height=0.69,
    proximity_persist_s=0.0,
    goodbye_confirm_s=0.2,
    goodbye_additional_shrink=0.01,
    height_signal=HeightSignalConfig(
        median_window=3,
        ema_alpha=1.0,
        slope_window_s=1.0,
        min_slope_span_s=0.5,
        motion_persist_s=0.0,
        approach_slope=0.04,
        recede_slope=-0.05,
        reset_gap_s=1.5,
        max_samples=30,
    ),
)


@dataclass(frozen=True)
class VisitorTriggerProfile:
    name: str
    implementation: str
    parameters: dict[str, Any]
    trigger_config: VisitorTriggerConfig | None = None

    def metadata(self, *, smooth: int = 0) -> dict[str, Any]:
        """Return the complete JSON-serializable runtime configuration."""

        return {
            "name": self.name,
            "implementation": self.implementation,
            "parameters": deepcopy(self.parameters),
            "tracker_smoothing_window": smooth,
        }


_PROFILES = {
    DEFAULT_VISITOR_TRIGGER_PROFILE: VisitorTriggerProfile(
        name=DEFAULT_VISITOR_TRIGGER_PROFILE,
        implementation="legacy_area_v1",
        parameters=dict(_LEGACY_PARAMETERS),
    ),
    VISITOR_V1_20260802: VisitorTriggerProfile(
        name=VISITOR_V1_20260802,
        implementation="visitor_height_v1",
        parameters=asdict(_VISITOR_V1_CONFIG),
        trigger_config=_VISITOR_V1_CONFIG,
    ),
    DOOR_V1_20260805: VisitorTriggerProfile(
        name=DOOR_V1_20260805,
        implementation="door_policy_v1",
        parameters={
            "person_observation": asdict(_VISITOR_V1_CONFIG),
            "door_observer": DoorObserverSettings(
                nested_candidate_guard_enabled=False,
                close_person_guard_enabled=False,
            ).to_dict(),
            "door_policy": DoorPolicySettings(
                interaction_eligibility_enabled=False,
            ).to_dict(),
        },
        trigger_config=_VISITOR_V1_CONFIG,
    ),
    DOOR_V2_20260809: VisitorTriggerProfile(
        name=DOOR_V2_20260809,
        implementation="door_policy_v1",
        parameters={
            "person_observation": asdict(_VISITOR_V1_CONFIG),
            "door_observer": DoorObserverSettings().to_dict(),
            "door_policy": DoorPolicySettings().to_dict(),
        },
        trigger_config=_VISITOR_V1_CONFIG,
    ),
}


def resolve_visitor_trigger_profile(name: str) -> VisitorTriggerProfile:
    try:
        return _PROFILES[name]
    except KeyError as exc:
        choices = ", ".join(VISITOR_TRIGGER_PROFILE_NAMES)
        raise ValueError(f"unknown visitor trigger profile {name!r}; choose one of: {choices}") from exc
