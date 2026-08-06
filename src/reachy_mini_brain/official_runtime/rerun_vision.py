"""Rerun rendering for versioned offline vision observations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .live_detection import DetectionLayerObservation
from .vision_observation import DetectionObservation, TrackObservation, VisionObservation


_STATE_CODES = {
    "ABSENT": 0.0,
    "PRESENT": 1.0,
    "UNKNOWN": 0.0,
    "FAR": 1.0,
    "NEAR": 2.0,
    "STATIONARY": 1.0,
    "APPROACHING": 2.0,
    "RECEDING": -1.0,
    "OUTSIDE": 1.0,
    "INSIDE": 2.0,
}


class RerunVisionRenderer:
    """Convert observations to stable Rerun entities without running inference."""

    def __init__(
        self,
        *,
        save_path: str | Path | None = None,
        spawn: bool = False,
        grpc_url: str | None = None,
        recording_id: str | None = None,
        jpeg_quality: int = 85,
        mode: str = "replay",
        rr_module: Any | None = None,
    ) -> None:
        if save_path is None and not spawn and grpc_url is None:
            raise ValueError("Rerun renderer requires save_path, grpc_url, or spawn=True")
        if rr_module is None:
            try:
                import rerun as rr_module  # type: ignore[import-not-found,no-redef]
            except ImportError as exc:
                raise RuntimeError("rerun-sdk is required for --save-rrd or --spawn-rerun") from exc
        self.rr = rr_module
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")
        self.jpeg_quality = int(jpeg_quality)
        self.mode = mode
        self._last_states: dict[str, str] = {}
        self._visible_path_ids: set[str] = set()
        self._initialize(
            save_path=Path(save_path) if save_path is not None else None,
            spawn=spawn,
            grpc_url=grpc_url,
            recording_id=recording_id,
        )

    def render(self, observation: VisionObservation, image_bgr: Any | None = None) -> None:
        rr = self.rr
        _set_time(rr, observation.frame_index, observation.frame_ts)
        root = f"{observation.mode}/camera"
        if image_bgr is not None:
            rr.log(root, _image_archetype(rr, image_bgr, jpeg_quality=self.jpeg_quality))

        self._log_detections(root, observation)
        self._log_tracks(root, observation)
        self._log_doorway(root, observation)
        self._log_signals(observation)
        self._log_states(observation)
        self._log_events(observation)

    def render_frame(self, *, frame_index: int, frame_ts: float, image_bgr: Any) -> None:
        """Render one live camera frame independently of detector completion."""

        _set_time(self.rr, frame_index, frame_ts)
        self.rr.log(
            f"{self.mode}/camera",
            _image_archetype(self.rr, image_bgr, jpeg_quality=self.jpeg_quality),
        )

    def render_detection_layer(self, observation: DetectionLayerObservation) -> None:
        """Render one named detector/tracker result on its source frame timestamp."""

        rr = self.rr
        _set_time(rr, observation.frame_index, observation.frame_ts)
        root = f"{self.mode}"
        pipeline_id = observation.pipeline_id
        color = _pipeline_color(pipeline_id)
        detections = list(observation.detections)
        detection_labels = [
            f"{pipeline_id}: {item.class_name} {item.confidence:.2f}"
            for item in detections
        ]
        _log_boxes(
            rr,
            f"{root}/detectors/{pipeline_id}/detections",
            [item.box for item in detections],
            detection_labels,
            colors=[color] * len(detections),
        )
        tracks = list(observation.tracks)
        track_labels = [f"{pipeline_id}: track {item.track_id}" for item in tracks]
        _log_boxes(
            rr,
            f"{root}/trackers/{pipeline_id}/tracks",
            [item.box for item in tracks],
            track_labels,
            colors=[_track_color(color)] * len(tracks),
        )
        result_age_ms = max(0.0, (observation.completed_ts - observation.frame_ts) * 1000.0)
        metrics_root = f"{root}/diagnostics"
        _log_scalar(
            rr,
            f"{metrics_root}/inference_latency_ms/{pipeline_id}",
            observation.inference_latency_ms,
        )
        _log_scalar(rr, f"{metrics_root}/result_age_ms/{pipeline_id}", result_age_ms)
        _log_scalar(
            rr,
            f"{metrics_root}/scheduler_wait_ms/{pipeline_id}",
            observation.scheduler_wait_ms,
        )
        _log_scalar(rr, f"{metrics_root}/detection_count/{pipeline_id}", float(len(detections)))
        _log_scalar(rr, f"{metrics_root}/track_count/{pipeline_id}", float(len(tracks)))
        _log_scalar(
            rr,
            f"{metrics_root}/submitted_frames/{pipeline_id}",
            float(observation.submitted_frames),
        )
        _log_scalar(
            rr,
            f"{metrics_root}/completed_frames/{pipeline_id}",
            float(observation.completed_frames),
        )
        _log_scalar(
            rr,
            f"{metrics_root}/dropped_frames/{pipeline_id}",
            float(observation.dropped_frames),
        )

    def close(self) -> None:
        flush = getattr(self.rr, "flush", None)
        if callable(flush):
            flush()
            return
        disconnect = getattr(self.rr, "disconnect", None)
        if callable(disconnect):
            disconnect()

    def _initialize(
        self,
        *,
        save_path: Path | None,
        spawn: bool,
        grpc_url: str | None,
        recording_id: str | None,
    ) -> None:
        rr = self.rr
        blueprint = _default_blueprint(self.mode)
        init_kwargs: dict[str, Any] = {
            "spawn": spawn and save_path is None,
            "default_blueprint": blueprint,
        }
        if recording_id is not None:
            init_kwargs["recording_id"] = recording_id
        rr.init(f"reachy-mini-vision-{self.mode}", **init_kwargs)
        if grpc_url is not None:
            set_sinks = getattr(rr, "set_sinks", None)
            grpc_sink = getattr(rr, "GrpcSink", None)
            file_sink = getattr(rr, "FileSink", None)
            if not callable(set_sinks) or not callable(grpc_sink):
                raise RuntimeError("this rerun-sdk version does not support a gRPC sink")
            sinks = [grpc_sink(grpc_url)]
            if save_path is not None:
                if not callable(file_sink):
                    raise RuntimeError("this rerun-sdk version does not support a file sink")
                save_path.parent.mkdir(parents=True, exist_ok=True)
                sinks.append(file_sink(str(save_path)))
            set_sinks(*sinks, default_blueprint=blueprint)
            send_blueprint = getattr(rr, "send_blueprint", None)
            if blueprint is not None and callable(send_blueprint):
                send_blueprint(blueprint)
        elif spawn and save_path is not None:
            spawn_viewer = getattr(rr, "spawn", None)
            set_sinks = getattr(rr, "set_sinks", None)
            grpc_sink = getattr(rr, "GrpcSink", None)
            file_sink = getattr(rr, "FileSink", None)
            if not all(callable(item) for item in (spawn_viewer, set_sinks, grpc_sink, file_sink)):
                raise RuntimeError("this rerun-sdk version cannot combine viewer and file sinks")
            spawn_viewer()
            set_sinks(grpc_sink(), file_sink(str(save_path)))
            send_blueprint = getattr(rr, "send_blueprint", None)
            if blueprint is not None and callable(send_blueprint):
                send_blueprint(blueprint)
        elif save_path is not None:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            rr.save(str(save_path), default_blueprint=blueprint)

    def _log_detections(self, root: str, observation: VisionObservation) -> None:
        rr = self.rr
        entity = f"{root}/detections"
        boxes = [item.box for item in observation.detections]
        labels = [
            _detection_label(item)
            for item in observation.detections
        ]
        colors = [
            [220, 72, 72] if item.possible_duplicate else [230, 92, 50]
            for item in observation.detections
        ]
        _log_boxes(rr, entity, boxes, labels, colors=colors)

    def _log_tracks(self, root: str, observation: VisionObservation) -> None:
        rr = self.rr
        tracks = observation.tracks
        labels = [
            f"{track.logical_track_id} src={track.source_track_id if track.source_track_id is not None else '-'}"
            for track in tracks
        ]
        colors = [[255, 196, 0] if track.active else [42, 171, 116] for track in tracks]
        _log_boxes(rr, f"{root}/tracks", [track.box for track in tracks], labels, colors=colors)
        _log_points(
            rr,
            f"{root}/track_anchors",
            [_pixel_anchor(track, observation) for track in tracks],
            labels=[track.logical_track_id for track in tracks],
            colors=colors,
            radii=[5.0] * len(tracks),
        )

        current_path_ids = {track.logical_track_id for track in tracks}
        for stale_id in self._visible_path_ids - current_path_ids:
            _clear(rr, f"{root}/track_paths/{stale_id}")
            _clear(rr, f"{root}/track_velocity/{stale_id}")
        for track in tracks:
            path = [
                (x * observation.frame_width, y * observation.frame_height)
                for _, x, y in track.trail
            ]
            _log_line_strip(rr, f"{root}/track_paths/{track.logical_track_id}", path, [42, 171, 116])
            if track.velocity is not None:
                anchor = _pixel_anchor(track, observation)
                vector = (
                    track.velocity[0] * observation.frame_width * 0.25,
                    track.velocity[1] * observation.frame_height * 0.25,
                )
                _log_arrow(rr, f"{root}/track_velocity/{track.logical_track_id}", anchor, vector)
            else:
                _clear(rr, f"{root}/track_velocity/{track.logical_track_id}")
        self._visible_path_ids = current_path_ids

    def _log_doorway(self, root: str, observation: VisionObservation) -> None:
        if observation.zone_config is None:
            _clear(self.rr, f"{root}/doorway")
            _clear(self.rr, f"{root}/doorway_anchors")
            return
        polygon = [
            (float(x) * observation.frame_width, float(y) * observation.frame_height)
            for x, y in observation.zone_config.get("polygon", ())
        ]
        if polygon:
            polygon.append(polygon[0])
        _log_line_strip(self.rr, f"{root}/doorway", polygon, [70, 130, 220])
        zone_tracks = [track for track in observation.tracks if track.zone is not None]
        _log_points(
            self.rr,
            f"{root}/doorway_anchors",
            [_pixel_anchor(track, observation) for track in zone_tracks],
            labels=[str(track.zone.get("zone_occupancy", "UNKNOWN")) for track in zone_tracks],
            colors=[_zone_color(track.zone) for track in zone_tracks],
            radii=[7.0] * len(zone_tracks),
        )

    def _log_signals(self, observation: VisionObservation) -> None:
        rr = self.rr
        root = f"{observation.mode}/signals"
        rr.log(
            f"{root}/person_counts/raw_person_detections",
            rr.Scalars(float(len(observation.detections))),
        )
        rr.log(
            f"{root}/person_counts/possible_duplicate_person_detections",
            rr.Scalars(float(sum(1 for item in observation.detections if item.possible_duplicate))),
        )
        rr.log(
            f"{root}/person_counts/byte_track_tracks",
            rr.Scalars(float(observation.scene.get("byte_track_track_count", 0))),
        )
        for detection in observation.detections:
            if detection.confidence is None:
                continue
            confidence_group = "possible_duplicate" if detection.possible_duplicate else "raw"
            rr.log(
                f"{root}/person_detection_confidence/{confidence_group}/detection_{detection.detection_index}",
                rr.Scalars(detection.confidence),
            )
        for track in observation.tracks:
            rr.log(f"{root}/height/raw/{track.logical_track_id}", rr.Scalars(track.height))
            if track.height_filtered is not None:
                rr.log(
                    f"{root}/height/filtered/{track.logical_track_id}",
                    rr.Scalars(track.height_filtered),
                )
            if track.height_slope is not None:
                rr.log(
                    f"{root}/log_height_slope/{track.logical_track_id}",
                    rr.Scalars(track.height_slope),
                )
            if track.velocity is not None:
                speed = (track.velocity[0] ** 2 + track.velocity[1] ** 2) ** 0.5
                rr.log(
                    f"{root}/image_plane_track_speed/{track.logical_track_id}",
                    rr.Scalars(speed),
                )
        parameters = observation.visitor_profile.get("parameters", {})
        _log_optional_scalar(
            rr,
            f"{root}/person_detection_confidence/detector_threshold",
            observation.detector.get("threshold"),
        )
        _log_optional_scalar(
            rr,
            f"{root}/height/threshold_near_enter",
            parameters.get("near_enter_height"),
        )
        _log_optional_scalar(
            rr,
            f"{root}/height/threshold_near_exit",
            parameters.get("near_exit_height"),
        )
        height_signal = parameters.get("height_signal", {})
        if isinstance(height_signal, dict):
            _log_optional_scalar(
                rr,
                f"{root}/log_height_slope/threshold_approaching",
                height_signal.get("approach_slope"),
            )
            _log_optional_scalar(
                rr,
                f"{root}/log_height_slope/threshold_receding",
                height_signal.get("recede_slope"),
            )
        _log_scalar(rr, f"{root}/log_height_slope/stationary_zero", 0.0)

    def _log_states(self, observation: VisionObservation) -> None:
        for dimension in ("presence", "proximity", "motion"):
            for semantics in ("observed", "retained"):
                key = f"{semantics}_{dimension}"
                value = str(observation.scene.get(key, "UNKNOWN"))
                self._log_discrete_state(
                    f"{observation.mode}/states/{dimension}/{semantics}",
                    value,
                )
        for track in observation.tracks:
            if track.zone is None:
                continue
            value = str(track.zone.get("zone_occupancy", "UNKNOWN"))
            self._log_discrete_state(
                f"{observation.mode}/states/doorway_occupancy/{track.logical_track_id}",
                value,
            )
            candidate = track.zone.get("zone_candidate")
            if candidate is not None:
                self._log_discrete_state(
                    f"{observation.mode}/states/doorway_candidate/{track.logical_track_id}",
                    str(candidate),
                )
        if bool(observation.scene.get("handoff")):
            self.rr.log(
                f"{observation.mode}/diagnostics/handoff",
                self.rr.TextLog(
                    f"accepted handoff from={observation.scene.get('handoff_from_track_id')} "
                    f"to={observation.scene.get('active_track_id')}"
                ),
            )

    def _log_discrete_state(self, entity: str, value: str) -> None:
        self.rr.log(entity, self.rr.Scalars(_STATE_CODES.get(value, 0.0)))
        if self._last_states.get(entity) != value:
            self.rr.log(f"{entity}/changes", self.rr.TextLog(value))
            self._last_states[entity] = value

    def _log_events(self, observation: VisionObservation) -> None:
        for event in observation.events:
            kind = str(event.get("kind", "unknown"))
            self.rr.log(
                f"{observation.mode}/decisions/{kind}",
                self.rr.TextLog(f"frame={observation.frame_index} {event}"),
            )


def _set_time(rr: Any, frame_index: int, frame_ts: float) -> None:
    rr.set_time("frame", sequence=frame_index)
    rr.set_time("time", timestamp=frame_ts)


def _image_archetype(rr: Any, image_bgr: Any, *, jpeg_quality: int) -> Any:
    encoded_image = getattr(rr, "EncodedImage", None)
    if callable(encoded_image):
        import cv2

        ok, encoded = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        if not ok:
            raise RuntimeError("failed to JPEG-encode replay frame")
        return encoded_image(contents=encoded.tobytes(), media_type="image/jpeg")
    return rr.Image(image_bgr[:, :, ::-1].copy())


def _default_blueprint(mode: str = "replay") -> Any | None:
    try:
        import rerun.blueprint as rrb  # type: ignore[import-not-found]
    except ImportError:
        return None
    if mode == "live":
        views = [
            rrb.Spatial2DView(
                origin="/live",
                contents=[
                    "/live/camera",
                    "/live/camera/**",
                    "/live/detectors/**",
                    "/live/trackers/**",
                ],
                name="Live detection and tracking",
            ),
            rrb.TimeSeriesView(
                origin="/live/diagnostics/inference_latency_ms",
                contents=["/live/diagnostics/inference_latency_ms/**"],
                name="Detector inference latency",
            ),
            rrb.TimeSeriesView(
                origin="/live/diagnostics/detection_count",
                contents=["/live/diagnostics/detection_count/**"],
                name="Detection counts",
            ),
            rrb.TimeSeriesView(
                origin="/live/diagnostics/dropped_frames",
                contents=["/live/diagnostics/dropped_frames/**"],
                name="Dropped detector frames",
            ),
        ]
        return rrb.Blueprint(
            rrb.Grid(*views, grid_columns=2, name="Live vision diagnosis"),
            rrb.SelectionPanel(expanded=True),
            rrb.TimePanel(expanded=True, timeline="time"),
            auto_views=False,
            auto_layout=False,
        )
    views = [
        rrb.Spatial2DView(
            origin="/replay/camera",
            contents=["/replay/camera", "/replay/camera/**"],
            name="Spatial tracking",
        ),
        rrb.TextLogView(
            origin="/replay",
            contents=[
                "/replay/decisions/**",
                "/replay/diagnostics/**",
                "/replay/states/**/changes",
            ],
            name="Events and diagnostics",
        ),
        rrb.TimeSeriesView(
            origin="/replay/signals/person_counts/raw_person_detections",
            contents=["/replay/signals/person_counts/raw_person_detections"],
            name="Raw person detections",
        ),
        rrb.TimeSeriesView(
            origin="/replay/signals/person_counts/possible_duplicate_person_detections",
            contents=["/replay/signals/person_counts/possible_duplicate_person_detections"],
            name="Possible duplicate person detections",
        ),
        rrb.TimeSeriesView(
            origin="/replay/signals/person_counts/byte_track_tracks",
            contents=["/replay/signals/person_counts/byte_track_tracks"],
            name="ByteTrack track count",
        ),
        rrb.TimeSeriesView(
            origin="/replay/signals/height",
            contents=["/replay/signals/height/**"],
            name="Height",
        ),
        rrb.TimeSeriesView(
            origin="/replay/signals/log_height_slope",
            contents=["/replay/signals/log_height_slope/**"],
            name="Log-height slope",
        ),
        rrb.TimeSeriesView(
            origin="/replay/states/presence/observed",
            contents=["/replay/states/presence/observed"],
            name="Observed presence",
        ),
        rrb.TimeSeriesView(
            origin="/replay/states/presence/retained",
            contents=["/replay/states/presence/retained"],
            name="Retained presence",
        ),
        rrb.TimeSeriesView(
            origin="/replay/states/proximity/observed",
            contents=["/replay/states/proximity/observed"],
            name="Observed proximity",
        ),
        rrb.TimeSeriesView(
            origin="/replay/states/proximity/retained",
            contents=["/replay/states/proximity/retained"],
            name="Retained proximity",
        ),
        rrb.TimeSeriesView(
            origin="/replay/states/motion/observed",
            contents=["/replay/states/motion/observed"],
            name="Observed motion",
        ),
        rrb.TimeSeriesView(
            origin="/replay/states/motion/retained",
            contents=["/replay/states/motion/retained"],
            name="Retained motion",
        ),
        rrb.TimeSeriesView(
            origin="/replay/signals/person_detection_confidence",
            contents=["/replay/signals/person_detection_confidence/**"],
            name="Person detection confidence",
        ),
        rrb.TimeSeriesView(
            origin="/replay/signals/image_plane_track_speed",
            contents=["/replay/signals/image_plane_track_speed/**"],
            name="Image-plane track speed",
        ),
    ]
    return rrb.Blueprint(
        rrb.Grid(*views, grid_columns=3, name="Vision replay diagnosis"),
        rrb.SelectionPanel(expanded=True),
        rrb.TimePanel(expanded=True, timeline="frame"),
        auto_views=False,
        auto_layout=False,
    )


def _pixel_anchor(track: TrackObservation, observation: VisionObservation) -> tuple[float, float]:
    return (
        track.bottom_center[0] * observation.frame_width,
        track.bottom_center[1] * observation.frame_height,
    )


def _detection_label(detection: DetectionObservation) -> str:
    confidence = f" {detection.confidence:.2f}" if detection.confidence is not None else ""
    duplicate = (
        f" duplicate? of det {detection.duplicate_of_detection_index}"
        if detection.possible_duplicate
        else ""
    )
    return f"det {detection.detection_index}{confidence}{duplicate}"


def _log_optional_scalar(rr: Any, entity: str, value: Any) -> None:
    if value is not None:
        _log_scalar(rr, entity, float(value))


def _log_scalar(rr: Any, entity: str, value: float) -> None:
    rr.log(entity, rr.Scalars(value))


def _zone_color(zone: dict[str, Any] | None) -> list[int]:
    value = zone.get("zone_occupancy") if zone is not None else None
    if value == "INSIDE":
        return [70, 130, 220]
    if value == "OUTSIDE":
        return [130, 130, 130]
    return [220, 160, 50]


def _pipeline_color(pipeline_id: str) -> list[int]:
    known = {
        "door_yolo_world": [42, 190, 90],
        "door_grounding_dino": [30, 190, 235],
    }
    if pipeline_id in known:
        return known[pipeline_id]
    digest = sum((index + 1) * ord(char) for index, char in enumerate(pipeline_id))
    palette = ([230, 92, 50], [70, 130, 220], [210, 150, 30], [180, 80, 190])
    return list(palette[digest % len(palette)])


def _track_color(detection_color: list[int]) -> list[int]:
    return [min(255, channel + 45) for channel in detection_color]


def _log_boxes(
    rr: Any,
    entity: str,
    boxes: list[tuple[float, float, float, float]],
    labels: list[str],
    *,
    colors: list[list[int]],
) -> None:
    if not boxes:
        _clear(rr, entity)
        return
    mins = [[x1, y1] for x1, y1, _, _ in boxes]
    sizes = [[x2 - x1, y2 - y1] for x1, y1, x2, y2 in boxes]
    rr.log(entity, rr.Boxes2D(mins=mins, sizes=sizes, labels=labels, colors=colors))


def _log_points(
    rr: Any,
    entity: str,
    points: list[tuple[float, float]],
    *,
    labels: list[str],
    colors: list[list[int]],
    radii: list[float],
) -> None:
    if not points:
        _clear(rr, entity)
        return
    rr.log(entity, rr.Points2D(points, labels=labels, colors=colors, radii=radii))


def _log_line_strip(rr: Any, entity: str, points: list[tuple[float, float]], color: list[int]) -> None:
    if len(points) < 2:
        _clear(rr, entity)
        return
    rr.log(entity, rr.LineStrips2D([points], colors=[color]))


def _log_arrow(rr: Any, entity: str, origin: tuple[float, float], vector: tuple[float, float]) -> None:
    arrows = getattr(rr, "Arrows2D", None)
    if callable(arrows):
        rr.log(entity, arrows(origins=[origin], vectors=[vector], colors=[[255, 196, 0]]))
    else:
        _log_line_strip(rr, entity, [origin, (origin[0] + vector[0], origin[1] + vector[1])], [255, 196, 0])


def _clear(rr: Any, entity: str) -> None:
    clear = getattr(rr, "Clear", None)
    if callable(clear):
        rr.log(entity, clear(recursive=True))
