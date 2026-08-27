"""Reception perception pipeline: person approach/departure plus wave trigger."""

from __future__ import annotations

import json
import time
import logging
import warnings
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .events import EventSink, RuntimeEvent
from .inference_scheduler import inference_guard
from .visitor_triggers import HeightSignalConfig, TrackBox, VisitorTriggerConfig, VisitorTriggerEngine
from .visitor_trigger_profiles import (
    DEFAULT_VISITOR_TRIGGER_PROFILE,
    VisitorTriggerProfile,
    resolve_visitor_trigger_profile,
)
from .vision_observation import (
    LogicalTrackResolver,
    TrackMovementHistory,
    TrackObservation,
    VisionObservation,
    detection_observations,
)
from .wave_detection import HandMotionWaveDetector


logger = logging.getLogger(__name__)

_GESTURE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/1/gesture_recognizer.task"
)
_GESTURE_MODEL_PATH = Path.home() / ".cache" / "reachy_mini" / "gesture_recognizer.task"
GESTURE_RUNNING_MODES = ("image", "video")
WAVE_DETECTION_MODES = ("open_palm", "hand_motion")


@dataclass(frozen=True)
class GestureFrameObservation:
    """Gesture classification and hand geometry from one MediaPipe inference."""

    candidate: tuple[str, float] | None
    hand_center_x: float | None
    hand_center_y: float | None
    hand_count: int


class PersonDetector:
    """RF-DETR Nano wrapped for person-only detection."""

    PERSON_CLASS_ID = 1

    def __init__(self, threshold: float = 0.5, optimize: bool = True) -> None:
        from rfdetr import RFDETRNano

        self.threshold = threshold
        self._model = RFDETRNano()
        if optimize:
            try:
                self._model.optimize_for_inference()
            except Exception:
                logger.debug("RF-DETR optimize_for_inference failed", exc_info=True)

    def detect(self, image: Any, *, bgr: bool = False) -> Any:
        if isinstance(image, np.ndarray) and bgr:
            image = np.ascontiguousarray(image[:, :, ::-1])
        with inference_guard("mps"):
            detections = self._model.predict(image, threshold=self.threshold)
        return detections[detections.class_id == self.PERSON_CLASS_ID]


class ApproachTracker:
    """Adapt ByteTrack person boxes to visitor greet/goodbye trigger rules."""

    def __init__(
        self,
        frame_wh: tuple[int, int],
        *,
        growth_factor: float = 1.3,
        greet_floor: float = 0.10,
        min_area_frac: float = 0.06,
        depart_factor: float = 0.6,
        present_frac: float = 0.03,
        reset_absent: int = 40,
        history: int = 30,
        smooth: int = 0,
        trigger_config: VisitorTriggerConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        import supervision as sv

        self.W, self.H = frame_wh
        # Retain the legacy constructor attributes for callers while evaluation selects
        # replacement height thresholds independently from the old area heuristic.
        self.growth_factor = growth_factor
        self.greet_floor = greet_floor
        self.min_area_frac = min_area_frac
        self.depart_factor = depart_factor
        self.present_frac = present_frac
        self.reset_absent = reset_absent
        self.history = history
        self._clock = clock
        self.frame_debug: list[dict[str, Any]] = []
        self.last_track_boxes: list[TrackBox] = []
        self._last_dom_area = 0.0
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._tracker = sv.ByteTrack()
        self._smoother = sv.DetectionsSmoother(length=smooth) if smooth > 0 else None
        if trigger_config is None:
            trigger_config = VisitorTriggerConfig(
                present_area_frac=present_frac,
                absent_reset_s=reset_absent * 0.2,
                height_signal=HeightSignalConfig(max_samples=history),
            )
        self._engine = VisitorTriggerEngine(trigger_config)

    @property
    def debug_state(self) -> dict[str, Any]:
        return {"dom_area": self._last_dom_area, **self._engine.debug_state}

    def update(self, persons: Any, *, ts: float | None = None) -> list[dict[str, Any]]:
        tracked = self._tracker.update_with_detections(persons)
        if self._smoother is not None:
            tracked = self._smoother.update_with_detections(tracked)

        boxes: list[TrackBox] = []
        for i in range(len(tracked)):
            if tracked.tracker_id is None:
                continue
            tid = int(tracked.tracker_id[i])
            boxes.append(
                self._track_box(
                    tracked.xyxy[i],
                    track_id=tid,
                    source_track_id=tid,
                    tracking_source="byte_track",
                )
            )

        scene_person_count = len(persons)
        if not boxes and scene_person_count == 1 and self._engine.active_track_id is not None:
            boxes.append(
                self._track_box(
                    persons.xyxy[0],
                    track_id=self._engine.active_track_id,
                    source_track_id=None,
                    tracking_source="raw_detection_fallback",
                )
            )
        frame_ts = float(ts if ts is not None else self._clock())
        events = self._engine.update(
            frame_ts,
            boxes,
            scene_person_count=scene_person_count,
        )
        self.last_track_boxes = list(boxes)
        self._last_dom_area = max((box.area for box in boxes), default=0.0)
        self.frame_debug = [
            {
                "id": box.track_id,
                "source_track_id": box.source_track_id,
                "tracking_source": box.tracking_source,
                "area": round(box.area, 3),
                "height": round(box.height, 3),
                "clipped": box.clipped,
                "cx": round(box.cx, 2),
                "cy": round(box.cy, 2),
                "box": list(box.box),
                **self._engine.track_debug(box.track_id),
            }
            for box in boxes
        ]
        return events

    def _track_box(
        self,
        xyxy: Any,
        *,
        track_id: int,
        source_track_id: int | None,
        tracking_source: str,
    ) -> TrackBox:
        x1, y1, x2, y2 = xyxy
        frame_area = float(self.W * self.H)
        area = max(0.0, ((x2 - x1) * (y2 - y1)) / frame_area)
        cx = ((x1 + x2) / 2) / self.W
        cy = ((y1 + y2) / 2) / self.H
        visible_y1 = max(0.0, float(y1))
        visible_y2 = min(float(self.H), float(y2))
        height = max(0.0, visible_y2 - visible_y1) / self.H
        clip_margin = max(2.0, self.H * 0.01)
        return TrackBox(
            track_id=track_id,
            area=float(area),
            cx=float(cx),
            cy=float(cy),
            height=float(height),
            clipped=bool(y1 <= clip_margin or y2 >= self.H - clip_margin),
            box=(int(x1), int(y1), int(x2), int(y2)),
            tracking_source=tracking_source,
            source_track_id=source_track_id,
        )


class LegacyApproachTracker:
    """Original dominant-area heuristic retained as the rollback profile."""

    def __init__(
        self,
        frame_wh: tuple[int, int],
        *,
        growth_factor: float = 1.3,
        greet_floor: float = 0.10,
        min_area_frac: float = 0.06,
        depart_factor: float = 0.6,
        present_frac: float = 0.03,
        reset_absent: int = 40,
        history: int = 30,
        smooth: int = 0,
    ) -> None:
        import supervision as sv

        self.W, self.H = frame_wh
        self.growth_factor = growth_factor
        self.greet_floor = greet_floor
        self.min_area_frac = min_area_frac
        self.depart_factor = depart_factor
        self.present_frac = present_frac
        self.reset_absent = reset_absent
        self.history = history
        self.frame_debug: list[dict[str, Any]] = []
        self.last_track_boxes: list[TrackBox] = []
        self._fc = 0
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._tracker = sv.ByteTrack()
        self._smoother = sv.DetectionsSmoother(length=smooth) if smooth > 0 else None
        self._reset_visit()

    @property
    def debug_state(self) -> dict[str, Any]:
        return {
            "dom_area": self._last_dom_area,
            "absent": self._absent,
            "peak": self._visit_peak,
            "greet": self._greet_fired,
            "depart": self._depart_fired,
        }

    def update(self, persons: Any, *, ts: float | None = None) -> list[dict[str, Any]]:
        del ts
        tracked = self._tracker.update_with_detections(persons)
        if self._smoother is not None:
            tracked = self._smoother.update_with_detections(tracked)

        frame_area = float(self.W * self.H)
        frame_debug: list[dict[str, Any]] = []
        track_boxes: list[TrackBox] = []
        dom_area, dom = 0.0, None
        for i in range(len(tracked)):
            if tracked.tracker_id is None:
                continue
            tid = int(tracked.tracker_id[i])
            x1, y1, x2, y2 = tracked.xyxy[i]
            area = ((x2 - x1) * (y2 - y1)) / frame_area
            cx = ((x1 + x2) / 2) / self.W
            cy = ((y1 + y2) / 2) / self.H
            visible_y1 = max(0.0, float(y1))
            visible_y2 = min(float(self.H), float(y2))
            height = max(0.0, visible_y2 - visible_y1) / self.H
            clip_margin = max(2.0, self.H * 0.01)
            track_boxes.append(
                TrackBox(
                    track_id=tid,
                    area=float(area),
                    cx=float(cx),
                    cy=float(cy),
                    height=float(height),
                    clipped=bool(y1 <= clip_margin or y2 >= self.H - clip_margin),
                    box=(int(x1), int(y1), int(x2), int(y2)),
                    tracking_source="byte_track",
                    source_track_id=tid,
                )
            )
            if area > dom_area:
                dom_area, dom = area, (tid, area, cx, cy)
            frame_debug.append(
                {
                    "id": int(tid),
                    "area": float(round(area, 3)),
                    "cx": float(round(cx, 2)),
                    "cy": float(round(cy, 2)),
                    "box": [int(x1), int(y1), int(x2), int(y2)],
                }
            )
        self.frame_debug = frame_debug
        self.last_track_boxes = track_boxes
        return self._update_visit(dom_area, dom)

    def _reset_visit(self) -> None:
        self._visit_min = 0.0
        self._visit_peak = 0.0
        self._greet_fired = False
        self._depart_fired = False
        self._absent = 0
        self._dom_hist: list[float] = []
        self._last_dom_area = 0.0

    def _update_visit(
        self,
        dom_area: float,
        dom: tuple[int, float, float, float] | None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        self._fc += 1
        self._last_dom_area = dom_area
        if dom_area >= self.present_frac:
            self._absent = 0
            self._dom_hist.append(dom_area)
            if len(self._dom_hist) > self.history:
                self._dom_hist.pop(0)
            self._visit_peak = max(self._visit_peak, dom_area)
            self._visit_min = dom_area if self._visit_min == 0.0 else min(self._visit_min, dom_area)

            if not self._greet_fired and dom is not None and dom_area >= self.greet_floor:
                grew = self._visit_min > 0 and dom_area / self._visit_min >= self.growth_factor
                rising = len(self._dom_hist) >= 3 and dom_area > self._dom_hist[-3]
                if grew and rising:
                    self._greet_fired = True
                    events.append(self._event("approach", *dom))

            if not self._depart_fired and self._visit_peak >= self.min_area_frac:
                threshold = self._visit_peak * self.depart_factor
                receding = len(self._dom_hist) >= 2 and all(area <= threshold for area in self._dom_hist[-2:])
                if receding and dom is not None:
                    self._depart_fired = True
                    events.append(self._event("depart", *dom))
        else:
            self._absent += 1
            if self._absent >= self.reset_absent:
                logger.info("visit reset after %d absent frames", self._absent)
                self._reset_visit()
        return events

    @staticmethod
    def _event(kind: str, tid: int, area: float, cx: float, cy: float) -> dict[str, Any]:
        return {
            "kind": kind,
            "id": int(tid),
            "area": float(round(area, 3)),
            "cx": float(round(cx, 2)),
            "cy": float(round(cy, 2)),
        }


def build_approach_tracker(
    frame_wh: tuple[int, int],
    *,
    profile: VisitorTriggerProfile,
    smooth: int = 0,
) -> ApproachTracker | LegacyApproachTracker:
    if profile.implementation == "legacy_area_v1":
        return LegacyApproachTracker(frame_wh, smooth=smooth, **profile.parameters)
    if profile.implementation in {"visitor_height_v1", "door_policy_v1"}:
        return ApproachTracker(frame_wh, smooth=smooth, trigger_config=profile.trigger_config)
    raise ValueError(f"unsupported visitor trigger implementation: {profile.implementation}")


class GestureDetector:
    """MediaPipe gesture recognizer for wave/open-palm events."""

    def __init__(
        self,
        gestures: tuple[str, ...] = ("Open_Palm",),
        threshold: float = 0.5,
        running_mode: str = "image",
    ) -> None:
        import mediapipe as mp

        normalized_mode = running_mode.lower()
        if normalized_mode not in GESTURE_RUNNING_MODES:
            choices = ", ".join(GESTURE_RUNNING_MODES)
            raise ValueError(f"unknown gesture running mode {running_mode!r}; choose one of: {choices}")
        self._mp = mp
        self.gestures = tuple(gestures)
        self._gesture_set = set(gestures)
        self.threshold = threshold
        self.running_mode = normalized_mode
        self._video_epoch_s: float | None = None
        self._last_video_timestamp_ms = -1
        self.classifier_score_floor = 0.0
        self.model_path = _ensure_gesture_model()
        mediapipe_mode = (
            mp.tasks.vision.RunningMode.VIDEO
            if normalized_mode == "video"
            else mp.tasks.vision.RunningMode.IMAGE
        )
        classifier_options = mp.tasks.components.processors.ClassifierOptions(
            score_threshold=self.classifier_score_floor,
            category_allowlist=list(self.gestures),
        )
        opts = mp.tasks.vision.GestureRecognizerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=self.model_path),
            running_mode=mediapipe_mode,
            canned_gesture_classifier_options=classifier_options,
        )
        self._recognizer = mp.tasks.vision.GestureRecognizer.create_from_options(opts)

    def observe(
        self,
        frame_bgr: NDArray[np.uint8],
        *,
        timestamp_s: float | None = None,
    ) -> GestureFrameObservation:
        import cv2

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        if self.running_mode == "video":
            if timestamp_s is None:
                raise ValueError("VIDEO gesture recognition requires a source timestamp")
            result = self._recognizer.recognize_for_video(
                img,
                self._video_timestamp_ms(timestamp_s),
            )
        else:
            result = self._recognizer.recognize(img)
        candidate = None
        if result.gestures and result.gestures[0]:
            top = result.gestures[0][0]
            candidate = (str(top.category_name), float(top.score))

        hand_landmarks = list(getattr(result, "hand_landmarks", ()) or ())
        if not hand_landmarks or len(hand_landmarks[0]) <= 9:
            return GestureFrameObservation(
                candidate=candidate,
                hand_center_x=None,
                hand_center_y=None,
                hand_count=len(hand_landmarks),
            )
        wrist = hand_landmarks[0][0]
        middle_mcp = hand_landmarks[0][9]
        return GestureFrameObservation(
            candidate=candidate,
            hand_center_x=(float(wrist.x) + float(middle_mcp.x)) / 2.0,
            hand_center_y=(float(wrist.y) + float(middle_mcp.y)) / 2.0,
            hand_count=len(hand_landmarks),
        )

    def detect_candidate(
        self,
        frame_bgr: NDArray[np.uint8],
        *,
        timestamp_s: float | None = None,
    ) -> tuple[str, float] | None:
        return self.observe(frame_bgr, timestamp_s=timestamp_s).candidate

    def detect(
        self,
        frame_bgr: NDArray[np.uint8],
        *,
        timestamp_s: float | None = None,
    ) -> tuple[str, float] | None:
        candidate = self.detect_candidate(frame_bgr, timestamp_s=timestamp_s)
        if candidate is None:
            return None
        name, score = candidate
        if name in self._gesture_set and score >= self.threshold:
            return name, score
        return None

    def _video_timestamp_ms(self, timestamp_s: float) -> int:
        if self._video_epoch_s is None:
            self._video_epoch_s = timestamp_s
        relative_ms = int(round((timestamp_s - self._video_epoch_s) * 1000.0))
        resolved = max(self._last_video_timestamp_ms + 1, relative_ms, 0)
        self._last_video_timestamp_ms = resolved
        return resolved


class PerceptionPipeline:
    """Run person detection, approach tracking, and optional wave detection per frame."""

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        smooth: int = 0,
        gestures: bool = False,
        gesture_cooldown: float = 3.0,
        events_path: str | Path | None = None,
        detector: Any | None = None,
        tracker_factory: Callable[[tuple[int, int]], Any] | None = None,
        gesture_detector: Any | None = None,
        gesture_running_mode: str = "image",
        wave_detection_mode: str = "open_palm",
        event_sink: EventSink | None = None,
        clock: Callable[[], float] = time.time,
        visitor_trigger_profile: str = DEFAULT_VISITOR_TRIGGER_PROFILE,
        observation_mode: str = "runtime",
        observation_run_id: str | None = None,
        track_trail_window_s: float = 3.0,
        doorway_zone: Any | None = None,
        gesture_only: bool = False,
    ) -> None:
        self.visitor_trigger_profile = resolve_visitor_trigger_profile(visitor_trigger_profile)
        self._gesture_only = bool(gesture_only)
        self._detector = (
            None
            if self._gesture_only
            else detector if detector is not None else PersonDetector(threshold=threshold)
        )
        self._detector_threshold = float(threshold)
        self._smooth = smooth
        self._tracker_factory = tracker_factory
        self._approach: Any | None = None
        self._gestures = gestures
        self._gesture_detector: Any | None = gesture_detector
        normalized_gesture_mode = gesture_running_mode.lower()
        if normalized_gesture_mode not in GESTURE_RUNNING_MODES:
            choices = ", ".join(GESTURE_RUNNING_MODES)
            raise ValueError(
                f"unknown gesture running mode {gesture_running_mode!r}; choose one of: {choices}"
            )
        self._gesture_running_mode = normalized_gesture_mode
        normalized_wave_mode = wave_detection_mode.lower()
        if normalized_wave_mode not in WAVE_DETECTION_MODES:
            choices = ", ".join(WAVE_DETECTION_MODES)
            raise ValueError(
                f"unknown wave detection mode {wave_detection_mode!r}; choose one of: {choices}"
            )
        self._wave_detection_mode = normalized_wave_mode
        self._hand_motion_wave = HandMotionWaveDetector()
        self._gesture_detector_ready_emitted = False
        self._gesture_cooldown = gesture_cooldown
        self._last_wave = 0.0
        self._clock = clock
        self._event_sink = event_sink
        self._events_path = Path(events_path) if events_path else None
        self._observation_mode = observation_mode
        self._observation_run_id = observation_run_id
        self._track_trail_window_s = float(track_trail_window_s)
        self._movement_history = TrackMovementHistory(trail_window_s=track_trail_window_s)
        self._logical_tracks = LogicalTrackResolver()
        self._doorway_zone = doorway_zone
        self._processed_frame_count = 0
        self.last_observation: VisionObservation | None = None
        if self._events_path is not None:
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            self._events_path.touch(exist_ok=True)

    def ensure_gesture_detector(self) -> dict[str, Any] | None:
        """Initialize the gesture detector and emit startup diagnostics."""

        if not self._gestures:
            return None
        if self._gesture_detector is not None:
            metadata = self._gesture_metadata(self._gesture_detector)
            if not self._gesture_detector_ready_emitted:
                self._emit("vision.gesture_detector_ready", load_ms=0.0, **metadata)
                self._gesture_detector_ready_emitted = True
            return metadata
        self._emit(
            "vision.gesture_detector_init_start",
            gestures=["Open_Palm"],
            threshold=0.5,
            running_mode=self._gesture_running_mode,
            wave_detection_mode=self._wave_detection_mode,
            model_path=str(_GESTURE_MODEL_PATH),
        )
        started = time.perf_counter()
        try:
            self._gesture_detector = GestureDetector(running_mode=self._gesture_running_mode)
        except Exception as exc:  # noqa: BLE001
            load_ms = round((time.perf_counter() - started) * 1000.0, 1)
            self._emit(
                "vision.gesture_detector_failed",
                gestures=["Open_Palm"],
                threshold=0.5,
                running_mode=self._gesture_running_mode,
                wave_detection_mode=self._wave_detection_mode,
                model_path=str(_GESTURE_MODEL_PATH),
                load_ms=load_ms,
                error=repr(exc),
            )
            raise
        metadata = self._gesture_metadata(self._gesture_detector)
        metadata["load_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        self._emit("vision.gesture_detector_ready", **metadata)
        self._gesture_detector_ready_emitted = True
        return metadata

    def process(
        self,
        frame: NDArray[np.uint8],
        *,
        bgr: bool = True,
        ts: float | None = None,
        frame_index: int | None = None,
        timestamp_source: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
        if self._gesture_only:
            raise RuntimeError("gesture-only perception pipeline cannot process person observations")
        if self._approach is None:
            h, w = frame.shape[:2]
            if self._tracker_factory is not None:
                self._approach = self._tracker_factory((w, h))
            else:
                self._approach = build_approach_tracker(
                    (w, h),
                    profile=self.visitor_trigger_profile,
                    smooth=self._smooth,
                )
        frame_ts = float(ts if ts is not None else self._clock())
        assert self._detector is not None
        persons = self._detector.detect(frame, bgr=bgr)
        events = self._approach.update(persons, ts=frame_ts)
        if self.visitor_trigger_profile.implementation == "door_policy_v1":
            events = [event for event in events if event.get("kind") not in {"approach", "depart"}]
        observation_index = self._processed_frame_count if frame_index is None else int(frame_index)
        if self._gestures:
            wave = self._detect_wave(
                frame,
                frame_ts=frame_ts,
                frame_index=observation_index,
            )
            if wave is not None:
                events.append(wave)
        self.last_observation = self._build_observation(
            frame,
            persons,
            events,
            frame_ts=frame_ts,
            frame_index=observation_index,
            timestamp_source=timestamp_source or ("provided" if ts is not None else "clock"),
        )
        self._processed_frame_count += 1
        self._write_events(events, ts=frame_ts)
        return events, len(persons), self._approach.frame_debug

    def process_gesture(
        self,
        frame: NDArray[np.uint8],
        *,
        ts: float | None = None,
        frame_index: int | None = None,
    ) -> dict[str, Any] | None:
        """Run only the ordered gesture path for a canonical source frame."""

        if not self._gestures:
            return None
        frame_ts = float(ts if ts is not None else self._clock())
        observation_index = self._processed_frame_count if frame_index is None else int(frame_index)
        event = self._detect_wave(
            frame,
            frame_ts=frame_ts,
            frame_index=observation_index,
        )
        self._processed_frame_count += 1
        if event is not None:
            self._write_events([event], ts=frame_ts)
        return event

    @property
    def debug_state(self) -> dict[str, Any]:
        return self._approach.debug_state if self._approach is not None else {}

    def _build_observation(
        self,
        frame: NDArray[np.uint8],
        persons: Any,
        events: list[dict[str, Any]],
        *,
        frame_ts: float,
        frame_index: int,
        timestamp_source: str,
    ) -> VisionObservation:
        h, w = frame.shape[:2]
        state = dict(self.debug_state)
        detections = detection_observations(persons, (w, h))
        handoff_from = _optional_track_id(state.get("handoff_from_track_id"))
        active_track_id = _optional_track_id(state.get("active_track_id"))
        if bool(state.get("handoff")):
            self._logical_tracks.apply_handoff(handoff_from, active_track_id)
            if self._doorway_zone is not None and handoff_from is not None and active_track_id is not None:
                self._doorway_zone.handoff(handoff_from, active_track_id)

        boxes = _exact_track_boxes(self._approach, frame_wh=(w, h))
        state["raw_person_detection_count"] = len(detections)
        state["possible_duplicate_person_detection_count"] = sum(
            1 for detection in detections if detection.possible_duplicate
        )
        state["tracked_person_count"] = len(boxes)
        state["byte_track_track_count"] = sum(
            1 for box in boxes if box.tracking_source == "byte_track"
        )
        state.setdefault("target_visible", bool(boxes))
        state.setdefault("visit_presence", state.get("presence", "UNKNOWN"))
        state.setdefault(
            "observed_presence",
            "PRESENT" if state["target_visible"] else "ABSENT",
        )
        state.setdefault("retained_presence", state.get("presence", "UNKNOWN"))
        state.setdefault(
            "observed_proximity",
            state.get("proximity", "UNKNOWN") if state["target_visible"] else "UNKNOWN",
        )
        state.setdefault("retained_proximity", state.get("proximity", "UNKNOWN"))
        state.setdefault(
            "observed_motion",
            state.get("motion", "UNKNOWN") if state["target_visible"] else "UNKNOWN",
        )
        state.setdefault("retained_motion", state.get("motion", "UNKNOWN"))
        debug_by_id = {
            int(item["id"]): item
            for item in getattr(self._approach, "frame_debug", ())
            if item.get("id") is not None
        }
        zone_snapshots = self._doorway_zone.update(frame_ts, boxes) if self._doorway_zone is not None else {}
        logical_by_track = {
            box.track_id: self._logical_tracks.resolve(box.track_id)
            for box in boxes
        }
        movement = self._movement_history.update_frame(
            frame_ts,
            {
                logical_by_track[box.track_id]: (box.cx, box.cy + box.height / 2.0)
                for box in boxes
            },
        )
        tracks: list[TrackObservation] = []
        for box in boxes:
            logical_id = logical_by_track[box.track_id]
            sample = movement[logical_id]
            debug = debug_by_id.get(box.track_id, {})
            zone_snapshot = zone_snapshots.get(box.track_id)
            tracks.append(
                TrackObservation(
                    logical_track_id=logical_id,
                    track_id=box.track_id,
                    source_track_id=box.source_track_id,
                    tracking_source=box.tracking_source,
                    box=tuple(float(value) for value in box.box),
                    center=(box.cx, box.cy),
                    bottom_center=(box.cx, box.cy + box.height / 2.0),
                    previous_anchor=sample.previous_anchor,
                    displacement=sample.displacement,
                    velocity=sample.velocity,
                    track_age_s=sample.track_age_s,
                    visible_sample_count=sample.visible_sample_count,
                    trail=sample.trail,
                    area=box.area,
                    height=box.height,
                    height_filtered=_optional_float(debug.get("height_filtered")),
                    height_slope=_optional_float(debug.get("log_height_slope")),
                    clipped=box.clipped,
                    height_reliable=not box.clipped,
                    active=bool(debug.get("active", box.track_id == active_track_id)),
                    handoff_from_track_id=(
                        handoff_from
                        if bool(state.get("handoff")) and box.track_id == active_track_id
                        else None
                    ),
                    motion=str(debug.get("motion", state.get("motion", "UNKNOWN"))),
                    zone=zone_snapshot.to_debug_dict() if zone_snapshot is not None else None,
                )
            )

        observation = VisionObservation(
            frame_index=frame_index,
            frame_ts=frame_ts,
            timestamp_source=timestamp_source,
            frame_width=w,
            frame_height=h,
            mode=self._observation_mode,
            run_id=self._observation_run_id,
            detector={
                "implementation": type(self._detector).__name__,
                "threshold": self._detector_threshold,
                "class_filter": "person",
                "possible_duplicate_containment_threshold": 0.9,
                "possible_duplicate_action": "diagnostic_only",
            },
            visitor_profile=self.visitor_trigger_profile.metadata(smooth=self._smooth),
            movement={
                "anchor": "BOTTOM_CENTER",
                "trail_window_s": self._track_trail_window_s,
                "coordinate_space": "normalized_image",
            },
            zone_config=(
                self._doorway_zone.config.to_dict()
                if self._doorway_zone is not None
                else None
            ),
            detections=detections,
            tracks=tuple(tracks),
            scene=state,
            events=tuple(dict(event) for event in events),
        )
        if state.get("presence_change") == "PRESENT->ABSENT":
            self._movement_history.reset()
            self._logical_tracks.reset()
        return observation

    def _detect_wave(
        self,
        frame: NDArray[np.uint8],
        *,
        frame_ts: float,
        frame_index: int,
    ) -> dict[str, Any] | None:
        detector = self._gesture_detector
        if detector is None:
            self.ensure_gesture_detector()
            detector = self._gesture_detector
            if detector is None:
                return None
        observation = self._observe_gesture_frame(
            detector,
            frame,
            frame_ts=frame_ts,
            running_mode=self._gesture_running_mode,
        )
        if self._wave_detection_mode == "hand_motion":
            return self._detect_hand_motion_wave(
                observation,
                frame_ts=frame_ts,
                frame_index=frame_index,
            )

        hit = observation.candidate
        if hit is None:
            return None
        name, score = hit
        threshold = float(getattr(detector, "threshold", 0.5))
        gestures = tuple(getattr(detector, "gestures", ("Open_Palm",)))
        allowed = name in set(gestures)
        above_threshold = score >= threshold
        if not allowed or not above_threshold:
            reason = "unsupported_gesture" if not allowed else "below_threshold"
            self._emit(
                "vision.gesture_candidate",
                gesture=name,
                score=round(score, 3),
                threshold=threshold,
                accepted=False,
                reason=reason,
                running_mode=self._gesture_running_mode,
                wave_detection_mode=self._wave_detection_mode,
                source_frame_index=frame_index,
                source_frame_ts=frame_ts,
            )
            return None
        self._emit(
            "vision.gesture_candidate",
            gesture=name,
            score=round(score, 3),
            threshold=threshold,
            accepted=True,
            running_mode=self._gesture_running_mode,
            wave_detection_mode=self._wave_detection_mode,
            source_frame_index=frame_index,
            source_frame_ts=frame_ts,
        )
        now = frame_ts
        if now - self._last_wave < self._gesture_cooldown:
            remaining = self._gesture_cooldown - (now - self._last_wave)
            self._emit(
                "vision.gesture_suppressed",
                gesture=name,
                score=round(score, 3),
                reason="cooldown",
                cooldown_s=self._gesture_cooldown,
                remaining_s=round(max(0.0, remaining), 3),
                running_mode=self._gesture_running_mode,
                wave_detection_mode=self._wave_detection_mode,
                source_frame_index=frame_index,
                source_frame_ts=frame_ts,
            )
            return None
        self._last_wave = now
        event = {"kind": "wave", "gesture": name, "score": round(score, 2)}
        self._emit(
            "vision.gesture_emitted",
            **event,
            running_mode=self._gesture_running_mode,
            wave_detection_mode=self._wave_detection_mode,
            source_frame_index=frame_index,
            source_frame_ts=frame_ts,
        )
        return event

    def _detect_hand_motion_wave(
        self,
        observation: GestureFrameObservation,
        *,
        frame_ts: float,
        frame_index: int,
    ) -> dict[str, Any] | None:
        if observation.hand_center_x is None:
            return None
        status = self._hand_motion_wave.update(
            timestamp_s=frame_ts,
            center_x=observation.hand_center_x,
        )
        self._emit(
            "vision.hand_motion_candidate",
            accepted=status.detected,
            reason=status.reason,
            hand_count=observation.hand_count,
            hand_center_x=round(status.center_x, 4),
            hand_center_y=(
                round(observation.hand_center_y, 4)
                if observation.hand_center_y is not None
                else None
            ),
            samples=status.samples,
            direction_changes=status.direction_changes,
            displacement=round(status.displacement, 4),
            min_displacement=self._hand_motion_wave.min_displacement,
            min_direction_changes=self._hand_motion_wave.min_cycles * 2,
            timeout_s=self._hand_motion_wave.timeout_s,
            running_mode=self._gesture_running_mode,
            wave_detection_mode=self._wave_detection_mode,
            source_frame_index=frame_index,
            source_frame_ts=frame_ts,
        )
        if not status.detected:
            return None

        if frame_ts - self._last_wave < self._gesture_cooldown:
            remaining = self._gesture_cooldown - (frame_ts - self._last_wave)
            self._emit(
                "vision.gesture_suppressed",
                gesture="Hand_Motion",
                score=round(status.displacement, 4),
                score_kind="normalized_horizontal_displacement",
                reason="cooldown",
                cooldown_s=self._gesture_cooldown,
                remaining_s=round(max(0.0, remaining), 3),
                running_mode=self._gesture_running_mode,
                wave_detection_mode=self._wave_detection_mode,
                source_frame_index=frame_index,
                source_frame_ts=frame_ts,
            )
            return None

        self._last_wave = frame_ts
        event = {
            "kind": "wave",
            "gesture": "Hand_Motion",
            "score": round(status.displacement, 3),
            "direction_changes": status.direction_changes,
        }
        self._emit(
            "vision.gesture_emitted",
            **event,
            score_kind="normalized_horizontal_displacement",
            running_mode=self._gesture_running_mode,
            wave_detection_mode=self._wave_detection_mode,
            source_frame_index=frame_index,
            source_frame_ts=frame_ts,
        )
        return event

    @staticmethod
    def _observe_gesture_frame(
        detector: Any,
        frame: NDArray[np.uint8],
        *,
        frame_ts: float,
        running_mode: str,
    ) -> GestureFrameObservation:
        observe = getattr(detector, "observe", None)
        if callable(observe):
            observation = (
                observe(frame, timestamp_s=frame_ts)
                if running_mode == "video"
                else observe(frame)
            )
            if isinstance(observation, GestureFrameObservation):
                return observation
            return GestureFrameObservation(
                candidate=getattr(observation, "candidate", None),
                hand_center_x=getattr(observation, "hand_center_x", None),
                hand_center_y=getattr(observation, "hand_center_y", None),
                hand_count=int(getattr(observation, "hand_count", 0)),
            )

        detect_candidate = getattr(detector, "detect_candidate", None)
        if callable(detect_candidate):
            hit = (
                detect_candidate(frame, timestamp_s=frame_ts)
                if running_mode == "video"
                else detect_candidate(frame)
            )
        else:
            hit = (
                detector.detect(frame, timestamp_s=frame_ts)
                if running_mode == "video"
                else detector.detect(frame)
            )
        candidate = None
        if hit is not None:
            name, score = hit
            candidate = (str(name), float(score))
        return GestureFrameObservation(
            candidate=candidate,
            hand_center_x=getattr(detector, "hand_center_x", None),
            hand_center_y=getattr(detector, "hand_center_y", None),
            hand_count=int(getattr(detector, "hand_count", 0)),
        )

    def _gesture_metadata(self, detector: Any) -> dict[str, Any]:
        gestures = tuple(getattr(detector, "gestures", ("Open_Palm",)))
        threshold = float(getattr(detector, "threshold", 0.5))
        model_path = str(getattr(detector, "model_path", _GESTURE_MODEL_PATH))
        return {
            "gestures": list(gestures),
            "threshold": threshold,
            "classifier_score_floor": float(
                getattr(detector, "classifier_score_floor", threshold)
            ),
            "running_mode": str(getattr(detector, "running_mode", self._gesture_running_mode)),
            "wave_detection_mode": self._wave_detection_mode,
            "model_path": model_path,
        }

    def _emit(self, event_kind: str, **data: Any) -> None:
        if self._event_sink is None:
            return
        self._event_sink.emit(RuntimeEvent(kind=event_kind, source="official_runtime.perception", data=data))

    def _write_events(self, events: list[dict[str, Any]], *, ts: float | None = None) -> None:
        if self._events_path is None:
            return
        with self._events_path.open("a", encoding="utf-8") as f:
            for event in events:
                rec = {
                    "type": event["kind"],
                    "ts": round(float(ts if ts is not None else self._clock()), 3),
                    **{k: v for k, v in event.items() if k != "kind"},
                }
                f.write(json.dumps(rec, sort_keys=True) + "\n")


def _exact_track_boxes(approach: Any, *, frame_wh: tuple[int, int]) -> list[TrackBox]:
    exact = getattr(approach, "last_track_boxes", None)
    if exact is not None:
        return list(exact)

    width, height = frame_wh
    boxes: list[TrackBox] = []
    for item in getattr(approach, "frame_debug", ()):
        if item.get("id") is None:
            continue
        x1, y1, x2, y2 = (float(value) for value in item.get("box", (0.0, 0.0, 0.0, 0.0)))
        visible_height = max(0.0, min(float(height), y2) - max(0.0, y1)) / height
        boxes.append(
            TrackBox(
                track_id=int(item["id"]),
                source_track_id=_optional_track_id(item.get("source_track_id")),
                tracking_source=str(item.get("tracking_source", "debug_fallback")),
                area=float(item.get("area", 0.0)),
                cx=float(item.get("cx", ((x1 + x2) / 2.0) / width)),
                cy=float(item.get("cy", ((y1 + y2) / 2.0) / height)),
                height=float(item.get("height", visible_height)),
                clipped=bool(item.get("clipped", False)),
                box=(int(x1), int(y1), int(x2), int(y2)),
            )
        )
    return boxes


def _optional_track_id(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _ensure_gesture_model() -> str:
    if not _GESTURE_MODEL_PATH.exists():
        _GESTURE_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        logger.info("downloading MediaPipe gesture model to %s", _GESTURE_MODEL_PATH)
        urllib.request.urlretrieve(_GESTURE_MODEL_URL, _GESTURE_MODEL_PATH)
    return str(_GESTURE_MODEL_PATH)
