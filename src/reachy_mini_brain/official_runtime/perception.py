"""Reception perception pipeline: person approach/departure plus wave trigger."""

from __future__ import annotations

import json
import time
import logging
import warnings
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .events import EventSink, RuntimeEvent
from .visitor_triggers import HeightSignalConfig, TrackBox, VisitorTriggerConfig, VisitorTriggerEngine
from .visitor_trigger_profiles import (
    DEFAULT_VISITOR_TRIGGER_PROFILE,
    VisitorTriggerProfile,
    resolve_visitor_trigger_profile,
)


logger = logging.getLogger(__name__)

_GESTURE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/1/gesture_recognizer.task"
)
_GESTURE_MODEL_PATH = Path.home() / ".cache" / "reachy_mini" / "gesture_recognizer.task"


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
        dom_area, dom = 0.0, None
        for i in range(len(tracked)):
            if tracked.tracker_id is None:
                continue
            tid = int(tracked.tracker_id[i])
            x1, y1, x2, y2 = tracked.xyxy[i]
            area = ((x2 - x1) * (y2 - y1)) / frame_area
            cx = ((x1 + x2) / 2) / self.W
            cy = ((y1 + y2) / 2) / self.H
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
    if profile.implementation == "visitor_height_v1":
        return ApproachTracker(frame_wh, smooth=smooth, trigger_config=profile.trigger_config)
    raise ValueError(f"unsupported visitor trigger implementation: {profile.implementation}")


class GestureDetector:
    """MediaPipe gesture recognizer for wave/open-palm events."""

    def __init__(self, gestures: tuple[str, ...] = ("Open_Palm",), threshold: float = 0.5) -> None:
        import mediapipe as mp

        self._mp = mp
        self.gestures = tuple(gestures)
        self._gesture_set = set(gestures)
        self.threshold = threshold
        self.model_path = _ensure_gesture_model()
        opts = mp.tasks.vision.GestureRecognizerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=self.model_path),
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
        )
        self._recognizer = mp.tasks.vision.GestureRecognizer.create_from_options(opts)

    def detect_candidate(self, frame_bgr: NDArray[np.uint8]) -> tuple[str, float] | None:
        import cv2

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._recognizer.recognize(img)
        if not result.gestures:
            return None
        top = result.gestures[0][0]
        return top.category_name, float(top.score)

    def detect(self, frame_bgr: NDArray[np.uint8]) -> tuple[str, float] | None:
        candidate = self.detect_candidate(frame_bgr)
        if candidate is None:
            return None
        name, score = candidate
        if name in self._gesture_set and score >= self.threshold:
            return name, score
        return None


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
        event_sink: EventSink | None = None,
        clock: Callable[[], float] = time.time,
        visitor_trigger_profile: str = DEFAULT_VISITOR_TRIGGER_PROFILE,
    ) -> None:
        self.visitor_trigger_profile = resolve_visitor_trigger_profile(visitor_trigger_profile)
        self._detector = detector if detector is not None else PersonDetector(threshold=threshold)
        self._smooth = smooth
        self._tracker_factory = tracker_factory
        self._approach: Any | None = None
        self._gestures = gestures
        self._gesture_detector: Any | None = gesture_detector
        self._gesture_detector_ready_emitted = False
        self._gesture_cooldown = gesture_cooldown
        self._last_wave = 0.0
        self._clock = clock
        self._event_sink = event_sink
        self._events_path = Path(events_path) if events_path else None
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
            model_path=str(_GESTURE_MODEL_PATH),
        )
        started = time.perf_counter()
        try:
            self._gesture_detector = GestureDetector()
        except Exception as exc:  # noqa: BLE001
            load_ms = round((time.perf_counter() - started) * 1000.0, 1)
            self._emit(
                "vision.gesture_detector_failed",
                gestures=["Open_Palm"],
                threshold=0.5,
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
    ) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
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
        persons = self._detector.detect(frame, bgr=bgr)
        events = self._approach.update(persons, ts=float(ts if ts is not None else self._clock()))
        if self._gestures:
            wave = self._detect_wave(frame)
            if wave is not None:
                events.append(wave)
        self._write_events(events)
        return events, len(persons), self._approach.frame_debug

    @property
    def debug_state(self) -> dict[str, Any]:
        return self._approach.debug_state if self._approach is not None else {}

    def _detect_wave(self, frame: NDArray[np.uint8]) -> dict[str, Any] | None:
        detector = self._gesture_detector
        if detector is None:
            metadata = self.ensure_gesture_detector()
            detector = self._gesture_detector
            if detector is None:
                return None
        hit = self._detect_gesture_candidate(detector, frame)
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
            )
            return None
        self._emit(
            "vision.gesture_candidate",
            gesture=name,
            score=round(score, 3),
            threshold=threshold,
            accepted=True,
        )
        now = self._clock()
        if now - self._last_wave < self._gesture_cooldown:
            remaining = self._gesture_cooldown - (now - self._last_wave)
            self._emit(
                "vision.gesture_suppressed",
                gesture=name,
                score=round(score, 3),
                reason="cooldown",
                cooldown_s=self._gesture_cooldown,
                remaining_s=round(max(0.0, remaining), 3),
            )
            return None
        self._last_wave = now
        event = {"kind": "wave", "gesture": name, "score": round(score, 2)}
        self._emit("vision.gesture_emitted", **event)
        return event

    @staticmethod
    def _detect_gesture_candidate(detector: Any, frame: NDArray[np.uint8]) -> tuple[str, float] | None:
        detect_candidate = getattr(detector, "detect_candidate", None)
        if callable(detect_candidate):
            hit = detect_candidate(frame)
        else:
            hit = detector.detect(frame)
        if hit is None:
            return None
        name, score = hit
        return str(name), float(score)

    @staticmethod
    def _gesture_metadata(detector: Any) -> dict[str, Any]:
        gestures = tuple(getattr(detector, "gestures", ("Open_Palm",)))
        threshold = float(getattr(detector, "threshold", 0.5))
        model_path = str(getattr(detector, "model_path", _GESTURE_MODEL_PATH))
        return {
            "gestures": list(gestures),
            "threshold": threshold,
            "model_path": model_path,
        }

    def _emit(self, event_kind: str, **data: Any) -> None:
        if self._event_sink is None:
            return
        self._event_sink.emit(RuntimeEvent(kind=event_kind, source="official_runtime.perception", data=data))

    def _write_events(self, events: list[dict[str, Any]]) -> None:
        if self._events_path is None:
            return
        with self._events_path.open("a", encoding="utf-8") as f:
            for event in events:
                rec = {
                    "type": event["kind"],
                    "ts": round(self._clock(), 3),
                    **{k: v for k, v in event.items() if k != "kind"},
                }
                f.write(json.dumps(rec, sort_keys=True) + "\n")


def _ensure_gesture_model() -> str:
    if not _GESTURE_MODEL_PATH.exists():
        _GESTURE_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        logger.info("downloading MediaPipe gesture model to %s", _GESTURE_MODEL_PATH)
        urllib.request.urlretrieve(_GESTURE_MODEL_URL, _GESTURE_MODEL_PATH)
    return str(_GESTURE_MODEL_PATH)
