"""Rerun renderer for the focused offline door review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .door_observation import DoorFrameObservation
from .door_policy import DoorPolicyFrameObservation


_STATE_CODES = {"UNKNOWN": 0.0, "STABLE": 1.0, "MOVING": 2.0}
_CANDIDATE_CODES = {"idle": 0.0, "greet": 1.0, "goodbye": 2.0}
_TRIGGER_CODES = {"none": 0.0, "approach": 1.0, "depart": 2.0}


class DoorReviewRenderer:
    """Render one spatial view and the accepted door-review timelines."""

    def __init__(
        self,
        *,
        save_path: str | Path,
        recording_id: str,
        jpeg_quality: int = 85,
        rr_module: Any | None = None,
    ) -> None:
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")
        if rr_module is None:
            try:
                import rerun as rr_module  # type: ignore[import-not-found,no-redef]
            except ImportError as exc:
                raise RuntimeError("rerun-sdk is required to create a door review") from exc
        self.rr = rr_module
        self.jpeg_quality = int(jpeg_quality)
        self._last_state: str | None = None
        self._active_interaction_track_ids: set[str] = set()
        output = Path(save_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        blueprint = _door_review_blueprint()
        self.rr.init(
            "reachy-mini-door-review",
            recording_id=recording_id,
            spawn=False,
            default_blueprint=blueprint,
        )
        self.rr.save(str(output), default_blueprint=blueprint)
        send_blueprint = getattr(self.rr, "send_blueprint", None)
        if blueprint is not None and callable(send_blueprint):
            send_blueprint(blueprint, make_active=True, make_default=True)
        self.rr.log(
            "door_review/state/door",
            self.rr.SeriesLines(
                colors=[35, 210, 190],
                widths=2.5,
                names="Door state",
                interpolation_mode=self.rr.components.InterpolationMode.StepAfter,
            ),
            static=True,
        )
        for entity, name, color in (
            ("door_review/signals/motion/combined", "Combined motion", [35, 210, 190]),
            ("door_review/signals/motion/geometry", "Geometry score", [255, 159, 28]),
            ("door_review/signals/motion/relative_flow", "Relative motion", [191, 90, 242]),
            ("door_review/signals/motion/enter_threshold", "Enter threshold", [255, 69, 58]),
            ("door_review/signals/motion/exit_threshold", "Exit threshold", [255, 214, 10]),
            ("door_review/signals/person_overlap/max_observed", "Maximum overlap", [10, 132, 255]),
        ):
            self.rr.log(
                entity,
                self.rr.SeriesLines(colors=color, widths=2.0, names=name),
                static=True,
            )

    def render(
        self,
        observation: DoorFrameObservation,
        frame_bgr: Any,
        *,
        policy: DoorPolicyFrameObservation | None = None,
    ) -> None:
        rr = self.rr
        rr.set_time("frame", sequence=observation.frame_index)
        rr.set_time("time", timestamp=observation.frame_ts)
        rr.log("door_review/camera/image", _encoded_image(rr, frame_bgr, self.jpeg_quality))

        if observation.semantic_updated:
            raw = list(observation.raw_door_detections)
            _log_boxes(
                rr,
                "door_review/camera/raw_door_boxes",
                [item.box for item in raw],
                [f"DINO {item.confidence:.2f}" for item in raw],
                color=[30, 190, 235],
            )
        if observation.retained_box is not None:
            _log_boxes(
                rr,
                "door_review/camera/retained_door_box",
                [observation.retained_box],
                [f"door {observation.state}"],
                color=[255, 196, 0],
                radii=3.0,
            )
        else:
            _clear(rr, "door_review/camera/retained_door_box")

        _log_boxes(
            rr,
            "door_review/camera/people",
            [item.box for item in observation.people],
            [item.track_id for item in observation.people],
            color=[42, 171, 116],
        )

        rr.log("door_review/state/door", rr.Scalars(_STATE_CODES[observation.state]))
        if self._last_state != observation.state:
            rr.log("door_review/state/changes", rr.TextLog(observation.state))
            self._last_state = observation.state

        rr.log("door_review/signals/motion/combined", rr.Scalars(observation.motion_score))
        rr.log(
            "door_review/signals/motion/geometry",
            rr.Scalars(observation.geometry_change_score),
        )
        rr.log(
            "door_review/signals/motion/relative_flow",
            rr.Scalars(observation.relative_door_motion_score),
        )
        rr.log(
            "door_review/signals/motion/enter_threshold",
            rr.Scalars(observation.motion_enter_threshold),
        )
        rr.log(
            "door_review/signals/motion/exit_threshold",
            rr.Scalars(observation.motion_exit_threshold),
        )
        rr.log(
            "door_review/signals/flow_quality/valid",
            rr.Scalars(float(observation.relative_motion_valid)),
        )
        rr.log(
            "door_review/signals/flow_quality/door_inlier_ratio",
            rr.Scalars(observation.door_flow_inlier_ratio),
        )
        rr.log(
            "door_review/signals/flow_quality/door_coverage",
            rr.Scalars(observation.door_flow_coverage),
        )
        rr.log(
            "door_review/signals/flow_quality/background_inlier_ratio",
            rr.Scalars(observation.background_flow_inlier_ratio),
        )

        normalized = observation.retained_box_normalized
        if normalized is not None:
            center_x, center_y, width, height = normalized
            rr.log("door_review/signals/box/center_x", rr.Scalars(center_x))
            rr.log("door_review/signals/box/center_y", rr.Scalars(center_y))
            rr.log("door_review/signals/box/width", rr.Scalars(width))
            rr.log("door_review/signals/box/height", rr.Scalars(height))

        current_track_ids = {_entity_id(interaction.track_id) for interaction in observation.interactions}
        rr.log(
            "door_review/signals/person_overlap/max_observed",
            rr.Scalars(
                max(
                    (interaction.overlap_ratio for interaction in observation.interactions),
                    default=0.0,
                )
            ),
        )
        for track_id in self._active_interaction_track_ids - current_track_ids:
            rr.log(
                f"door_review/signals/person_overlap/{track_id}",
                rr.Scalars(float("nan")),
            )
            rr.log(
                f"door_review/signals/person_distance/{track_id}",
                rr.Scalars(float("nan")),
            )
        for interaction in observation.interactions:
            track_id = _entity_id(interaction.track_id)
            rr.log(
                f"door_review/signals/person_overlap/{track_id}",
                rr.Scalars(interaction.overlap_ratio),
            )
            rr.log(
                f"door_review/signals/person_distance/{track_id}",
                rr.Scalars(interaction.normalized_distance),
            )
        self._active_interaction_track_ids = current_track_ids
        if policy is not None:
            self._render_policy(policy, observation)

    def _render_policy(
        self,
        policy: DoorPolicyFrameObservation,
        observation: DoorFrameObservation,
    ) -> None:
        rr = self.rr
        candidate = (
            "greet"
            if policy.greet_candidate_armed
            else "goodbye"
            if policy.goodbye_candidate_armed
            else "idle"
        )
        trigger = policy.events[0]["kind"] if policy.events else "none"
        rr.log(
            "door_review/policy/presence/observed",
            rr.Scalars(float(policy.observed_presence == "PRESENT")),
        )
        rr.log(
            "door_review/policy/presence/retained",
            rr.Scalars(float(policy.retained_presence == "PRESENT")),
        )
        rr.log("door_review/policy/candidate", rr.Scalars(_CANDIDATE_CODES[candidate]))
        rr.log("door_review/policy/trigger", rr.Scalars(_TRIGGER_CODES[trigger]))
        rr.log("door_review/policy/decision_latency_s", rr.Scalars(policy.decision_latency_s))
        rr.log(
            "door_review/policy/interaction/distance_threshold",
            rr.Scalars(policy.interaction_distance_enter),
        )
        rr.log(
            "door_review/policy/interaction/overlap_threshold",
            rr.Scalars(policy.interaction_overlap_enter),
        )
        if observation.semantic_inference_latency_ms is not None:
            rr.log(
                "door_review/policy/dino/inference_latency_ms",
                rr.Scalars(observation.semantic_inference_latency_ms),
            )
        if observation.semantic_source_age_s is not None:
            rr.log(
                "door_review/policy/dino/source_age_s",
                rr.Scalars(observation.semantic_source_age_s),
            )
        if policy.decision != "none":
            rr.log(
                "door_review/policy/decisions",
                rr.TextLog(f"{policy.decision}: {policy.reason}"),
            )

    def close(self) -> None:
        flush = getattr(self.rr, "flush", None)
        if callable(flush):
            flush()


def _door_review_blueprint() -> Any | None:
    try:
        import rerun.blueprint as rrb  # type: ignore[import-not-found]
    except ImportError:
        return None
    linked_x_axis = rrb.TimeAxis(link=rrb.components.LinkAxis.LinkToGlobal)
    score_y_axis = rrb.ScalarAxis(range=(-0.02, 1.02))
    views = [
        rrb.Spatial2DView(
            origin="/door_review/camera",
            contents=["/door_review/camera/**"],
            name="Door and person tracking",
        ),
        rrb.TimeSeriesView(
            origin="/door_review/state/door",
            contents=["/door_review/state/door"],
            name="Door state",
            axis_x=linked_x_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/signals/motion",
            contents=[
                "/door_review/signals/motion/combined",
                "/door_review/signals/motion/enter_threshold",
                "/door_review/signals/motion/exit_threshold",
            ],
            name="Combined door motion",
            axis_x=linked_x_axis,
            axis_y=score_y_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/signals/motion/geometry",
            contents=["/door_review/signals/motion/geometry"],
            name="Door geometry score",
            axis_x=linked_x_axis,
            axis_y=score_y_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/signals/motion/relative_flow",
            contents=["/door_review/signals/motion/relative_flow"],
            name="Relative door-leaf motion",
            axis_x=linked_x_axis,
            axis_y=score_y_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/signals/flow_quality",
            contents=["/door_review/signals/flow_quality/**"],
            name="Relative-flow quality",
            axis_x=linked_x_axis,
            axis_y=score_y_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/signals/box",
            contents=["/door_review/signals/box/**"],
            name="Door box geometry",
            axis_x=linked_x_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/signals/person_overlap",
            contents=[
                "/door_review/signals/person_overlap/**",
                "/door_review/policy/interaction/overlap_threshold",
            ],
            name="Person-door overlap",
            axis_x=linked_x_axis,
            axis_y=score_y_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/signals/person_distance",
            contents=[
                "/door_review/signals/person_distance/**",
                "/door_review/policy/interaction/distance_threshold",
            ],
            name="Person-door distance",
            axis_x=linked_x_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/policy/presence",
            contents=["/door_review/policy/presence/**"],
            name="Observed and retained presence",
            axis_x=linked_x_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/policy/candidate",
            contents=["/door_review/policy/candidate"],
            name="Policy arm code (0 idle, 1 arrival, 2 departure; not counts)",
            axis_x=linked_x_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/policy/trigger",
            contents=["/door_review/policy/trigger"],
            name="Policy event code (0 none, 1 approach, 2 depart; not counts)",
            axis_x=linked_x_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/policy/dino",
            contents=["/door_review/policy/dino/**"],
            name="DINO latency",
            axis_x=linked_x_axis,
        ),
        rrb.TimeSeriesView(
            origin="/door_review/policy/decision_latency_s",
            contents=["/door_review/policy/decision_latency_s"],
            name="Source-to-decision latency",
            axis_x=linked_x_axis,
        ),
    ]
    return rrb.Blueprint(
        rrb.Grid(*views, grid_columns=2, name="Door movement review"),
        rrb.SelectionPanel(expanded=True),
        rrb.TimePanel(expanded=True, timeline="frame"),
        auto_views=False,
        auto_layout=False,
    )


def _encoded_image(rr: Any, frame_bgr: Any, quality: int) -> Any:
    import cv2

    ok, encoded = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("failed to JPEG-encode door-review frame")
    return rr.EncodedImage(contents=encoded.tobytes(), media_type="image/jpeg")


def _log_boxes(
    rr: Any,
    entity: str,
    boxes: list[tuple[float, float, float, float]],
    labels: list[str],
    *,
    color: list[int],
    radii: float = 1.5,
) -> None:
    if not boxes:
        _clear(rr, entity)
        return
    rr.log(
        entity,
        rr.Boxes2D(
            mins=[[box[0], box[1]] for box in boxes],
            sizes=[[box[2] - box[0], box[3] - box[1]] for box in boxes],
            labels=labels,
            colors=[color] * len(boxes),
            radii=radii,
        ),
    )


def _clear(rr: Any, entity: str) -> None:
    clear = getattr(rr, "Clear", None)
    if callable(clear):
        rr.log(entity, clear(recursive=True))


def _entity_id(value: str) -> str:
    return "".join(character if character.isalnum() or character in "_-" else "_" for character in value)
