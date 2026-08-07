"""Configurable asynchronous detection pipelines for live diagnosis."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import numpy as np
from numpy.typing import NDArray

from .inference_scheduler import inference_guard


DETECTION_LAYER_SCHEMA_VERSION = 1
PIPELINE_CONFIG_SCHEMA_VERSION = 1
SUPPORTED_DETECTORS = frozenset({"yolo-world", "grounding-dino"})
SUPPORTED_TRACKERS = frozenset({"none", "bytetrack"})
SUPPORTED_ROLES = frozenset({"diagnosis", "policy"})


@dataclass(frozen=True)
class PipelineSpec:
    id: str
    detector: str
    model: str
    targets: tuple[str, ...]
    threshold: float
    inference_fps: float
    tracker: str = "none"
    role: str = "diagnosis"
    text_threshold: float = 0.15
    device: str = "mps"
    input_size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PipelineConfig:
    path: Path
    sha256: str
    pipelines: tuple[PipelineSpec, ...]
    schema_version: int = PIPELINE_CONFIG_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "path": str(self.path),
            "sha256": self.sha256,
            "pipelines": [item.to_dict() for item in self.pipelines],
        }


@dataclass(frozen=True)
class LayerDetection:
    detection_index: int
    class_id: int | None
    class_name: str
    confidence: float
    box: tuple[float, float, float, float]


@dataclass(frozen=True)
class LayerTrack:
    track_id: int
    source_detection_index: int | None
    class_name: str
    confidence: float | None
    box: tuple[float, float, float, float]


@dataclass(frozen=True)
class DetectionLayerObservation:
    run_id: str
    pipeline_id: str
    frame_index: int
    frame_ts: float
    completed_ts: float
    inference_latency_ms: float
    detector_config: dict[str, Any]
    tracker_config: dict[str, Any]
    scheduler_wait_ms: float = 0.0
    detections: tuple[LayerDetection, ...] = ()
    tracks: tuple[LayerTrack, ...] = ()
    submitted_frames: int = 0
    completed_frames: int = 0
    dropped_frames: int = 0
    schema_version: int = DETECTION_LAYER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FramePacket:
    frame_index: int
    frame_ts: float
    frame_bgr: NDArray[np.uint8]


class LayerDetector(Protocol):
    def detect(self, frame_bgr: NDArray[np.uint8]) -> list[LayerDetection]: ...


class LayerTracker(Protocol):
    def update(self, detections: list[LayerDetection]) -> list[LayerTrack]: ...


def load_pipeline_config(path: str | Path) -> PipelineConfig:
    config_path = Path(path).expanduser().resolve()
    raw = config_path.read_bytes()
    payload = json.loads(raw)
    if int(payload.get("schema_version", 0)) != PIPELINE_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported vision pipeline schema_version: {payload.get('schema_version')!r}"
        )
    rows = payload.get("pipelines")
    if not isinstance(rows, list) or not rows:
        raise ValueError("vision pipeline config requires a non-empty pipelines list")
    pipelines = tuple(_parse_pipeline(row) for row in rows)
    ids = [item.id for item in pipelines]
    if len(ids) != len(set(ids)):
        raise ValueError("vision pipeline ids must be unique")
    return PipelineConfig(
        path=config_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        pipelines=pipelines,
    )


def _parse_pipeline(value: Any) -> PipelineSpec:
    if not isinstance(value, Mapping):
        raise ValueError("each vision pipeline must be an object")
    pipeline_id = str(value.get("id", "")).strip()
    if not pipeline_id or not all(ch.isalnum() or ch in "_-" for ch in pipeline_id):
        raise ValueError(f"invalid vision pipeline id: {pipeline_id!r}")
    detector = str(value.get("detector", ""))
    if detector not in SUPPORTED_DETECTORS:
        raise ValueError(f"unsupported detector {detector!r}; expected {sorted(SUPPORTED_DETECTORS)}")
    tracker = str(value.get("tracker", "none"))
    if tracker not in SUPPORTED_TRACKERS:
        raise ValueError(f"unsupported tracker {tracker!r}; expected {sorted(SUPPORTED_TRACKERS)}")
    role = str(value.get("role", "diagnosis"))
    if role not in SUPPORTED_ROLES:
        raise ValueError(f"unsupported pipeline role {role!r}; expected {sorted(SUPPORTED_ROLES)}")
    targets_value = value.get("targets")
    if not isinstance(targets_value, list) or not targets_value:
        raise ValueError(f"vision pipeline {pipeline_id!r} requires non-empty targets")
    targets = tuple(str(item).strip() for item in targets_value if str(item).strip())
    if not targets:
        raise ValueError(f"vision pipeline {pipeline_id!r} has no valid targets")
    model = str(value.get("model", "")).strip()
    if not model:
        raise ValueError(f"vision pipeline {pipeline_id!r} requires model")
    threshold = float(value.get("threshold", 0.0))
    text_threshold = float(value.get("text_threshold", 0.15))
    inference_fps = float(value.get("inference_fps", 0.0))
    input_size_value = value.get("input_size")
    input_size = int(input_size_value) if input_size_value is not None else None
    if not 0.0 <= threshold <= 1.0 or not 0.0 <= text_threshold <= 1.0:
        raise ValueError(f"vision pipeline {pipeline_id!r} thresholds must be between 0 and 1")
    if inference_fps <= 0.0:
        raise ValueError(f"vision pipeline {pipeline_id!r} inference_fps must be positive")
    if input_size is not None and input_size < 128:
        raise ValueError(f"vision pipeline {pipeline_id!r} input_size must be at least 128")
    return PipelineSpec(
        id=pipeline_id,
        detector=detector,
        model=model,
        targets=targets,
        threshold=threshold,
        text_threshold=text_threshold,
        inference_fps=inference_fps,
        tracker=tracker,
        role=role,
        device=str(value.get("device", "mps")),
        input_size=input_size,
    )


class YoloWorldLayerDetector:
    def __init__(self, spec: PipelineSpec) -> None:
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError("YOLO-World requires the optional ultralytics dependency") from exc
        self._model = YOLO(_resolve_yolo_model(spec.model))
        self._model.set_classes(list(spec.targets))
        self._threshold = spec.threshold
        self._device = spec.device

    def detect(self, frame_bgr: NDArray[np.uint8]) -> list[LayerDetection]:
        result = self._model.predict(
            frame_bgr,
            conf=self._threshold,
            device=self._device,
            verbose=False,
            agnostic_nms=True,
        )[0]
        if result.boxes is None:
            return []
        boxes = result.boxes.xyxy.detach().cpu().numpy()
        scores = result.boxes.conf.detach().cpu().numpy()
        classes = result.boxes.cls.detach().cpu().numpy().astype(int)
        return [
            LayerDetection(
                detection_index=index,
                class_id=int(class_id),
                class_name=str(result.names[int(class_id)]),
                confidence=float(score),
                box=tuple(float(item) for item in box),
            )
            for index, (box, score, class_id) in enumerate(
                zip(boxes, scores, classes, strict=True)
            )
        ]


class GroundingDinoLayerDetector:
    def __init__(self, spec: PipelineSpec) -> None:
        try:
            import torch
            from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
        except ImportError as exc:
            raise RuntimeError(
                "Grounding DINO requires the optional torch and transformers dependencies"
            ) from exc
        self._torch = torch
        self._processor = AutoProcessor.from_pretrained(spec.model)
        self._model = AutoModelForZeroShotObjectDetection.from_pretrained(spec.model).to(spec.device)
        self._model.eval()
        self._prompt = ". ".join(spec.targets) + "."
        self._threshold = spec.threshold
        self._text_threshold = spec.text_threshold
        self._device = spec.device
        self._target_ids = {target.casefold(): index for index, target in enumerate(spec.targets)}
        self._fallback_label = spec.targets[0] if spec.targets else "object"
        self._input_size = spec.input_size
        self._health_events: list[tuple[str, dict[str, Any]]] = []

    def detect(self, frame_bgr: NDArray[np.uint8]) -> list[LayerDetection]:
        image_rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
        height, width = image_rgb.shape[:2]
        processor_kwargs: dict[str, Any] = {}
        if self._input_size is not None:
            processor_kwargs["size"] = {
                "shortest_edge": self._input_size,
                "longest_edge": int(round(self._input_size * 1.66625)),
            }
        inputs = self._processor(
            images=image_rgb,
            text=self._prompt,
            return_tensors="pt",
            **processor_kwargs,
        ).to(self._device)
        with self._torch.inference_mode():
            outputs = self._model(**inputs)
        result = self._processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            threshold=self._threshold,
            text_threshold=self._text_threshold,
            target_sizes=[(height, width)],
        )[0]
        labels = result.get("text_labels")
        if labels is None:
            labels = result.get("labels", [])
        detection_count, aligned_labels, mismatch = _align_grounding_dino_output(
            box_count=len(result["boxes"]),
            score_count=len(result["scores"]),
            labels=labels,
            fallback_label=self._fallback_label,
        )
        if mismatch is not None:
            self._health_events.append(("pipeline_output_mismatch", mismatch))
        detections = [
            LayerDetection(
                detection_index=index,
                class_id=self._target_ids.get(str(label).casefold()),
                class_name=str(label),
                confidence=float(score),
                box=tuple(float(item) for item in box.tolist()),
            )
            for index, (box, score, label) in enumerate(
                zip(
                    result["boxes"][:detection_count],
                    result["scores"][:detection_count],
                    aligned_labels,
                    strict=True,
                )
            )
        ]
        return _nms(detections, threshold=0.5)

    def drain_health_events(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        events = tuple(self._health_events)
        self._health_events.clear()
        return events


class ByteTrackLayerTracker:
    def __init__(self, *, frame_rate: float) -> None:
        try:
            import supervision as sv
        except ImportError as exc:
            raise RuntimeError("ByteTrack requires the optional supervision dependency") from exc
        self._sv = sv
        self._tracker = sv.ByteTrack(frame_rate=max(1, int(round(frame_rate))))

    def update(self, detections: list[LayerDetection]) -> list[LayerTrack]:
        sv = self._sv
        raw = sv.Detections(
            xyxy=np.asarray([item.box for item in detections], dtype=np.float32).reshape((-1, 4)),
            confidence=np.asarray([item.confidence for item in detections], dtype=np.float32),
            class_id=np.asarray(
                [item.class_id if item.class_id is not None else 0 for item in detections],
                dtype=int,
            ),
        )
        tracked = self._tracker.update_with_detections(raw)
        if tracked.tracker_id is None:
            return []
        labels = {
            item.class_id if item.class_id is not None else 0: item.class_name
            for item in detections
        }
        confidence = tracked.confidence if tracked.confidence is not None else [None] * len(tracked)
        class_ids = tracked.class_id if tracked.class_id is not None else [0] * len(tracked)
        return [
            LayerTrack(
                track_id=int(track_id),
                source_detection_index=None,
                class_name=labels.get(int(class_id), "object"),
                confidence=float(score) if score is not None else None,
                box=tuple(float(item) for item in box),
            )
            for box, score, class_id, track_id in zip(
                tracked.xyxy,
                confidence,
                class_ids,
                tracked.tracker_id,
                strict=True,
            )
        ]


class NoopLayerTracker:
    def update(self, detections: list[LayerDetection]) -> list[LayerTrack]:
        del detections
        return []


DetectorFactory = Callable[[PipelineSpec], LayerDetector]
TrackerFactory = Callable[[PipelineSpec], LayerTracker]
ResultCallback = Callable[[DetectionLayerObservation], None]
HealthCallback = Callable[[str, Mapping[str, Any]], None]


def build_detector(spec: PipelineSpec) -> LayerDetector:
    if spec.detector == "yolo-world":
        return YoloWorldLayerDetector(spec)
    if spec.detector == "grounding-dino":
        return GroundingDinoLayerDetector(spec)
    raise ValueError(f"unsupported detector: {spec.detector}")


def build_tracker(spec: PipelineSpec) -> LayerTracker:
    if spec.tracker == "none":
        return NoopLayerTracker()
    if spec.tracker == "bytetrack":
        return ByteTrackLayerTracker(frame_rate=spec.inference_fps)
    raise ValueError(f"unsupported tracker: {spec.tracker}")


class LiveDetectionManager:
    """Run independently paced detector pipelines without blocking frame capture."""

    def __init__(
        self,
        *,
        run_id: str,
        config: PipelineConfig,
        result_callback: ResultCallback,
        health_callback: HealthCallback | None = None,
        detector_factory: DetectorFactory = build_detector,
        tracker_factory: TrackerFactory = build_tracker,
        clock: Callable[[], float] = time.time,
        perf_counter: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.run_id = run_id
        self.config = config
        self._workers: list[_PipelineWorker] = []
        for spec in config.pipelines:
            load_started = perf_counter()
            detector = detector_factory(spec)
            tracker = tracker_factory(spec)
            load_ms = (perf_counter() - load_started) * 1000.0
            worker = _PipelineWorker(
                run_id=run_id,
                spec=spec,
                detector=detector,
                tracker=tracker,
                result_callback=result_callback,
                health_callback=health_callback,
                clock=clock,
                perf_counter=perf_counter,
            )
            self._workers.append(worker)
            if health_callback is not None:
                health_callback(
                    "pipeline_ready",
                    {"pipeline_id": spec.id, "model_load_ms": round(load_ms, 3)},
                )

    def start(self) -> None:
        for worker in self._workers:
            worker.start()

    def submit(self, packet: FramePacket) -> tuple[str, ...]:
        return tuple(worker.spec.id for worker in self._workers if worker.submit(packet))

    def close(self) -> None:
        for worker in self._workers:
            worker.close()

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {worker.spec.id: worker.snapshot() for worker in self._workers}


class _PipelineWorker:
    def __init__(
        self,
        *,
        run_id: str,
        spec: PipelineSpec,
        detector: LayerDetector,
        tracker: LayerTracker,
        result_callback: ResultCallback,
        health_callback: HealthCallback | None,
        clock: Callable[[], float],
        perf_counter: Callable[[], float],
    ) -> None:
        self.run_id = run_id
        self.spec = spec
        self.detector = detector
        self.tracker = tracker
        self.result_callback = result_callback
        self.health_callback = health_callback
        self.clock = clock
        self.perf_counter = perf_counter
        self.queue: queue.Queue[FramePacket | None] = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._run, name=f"vision-{spec.id}", daemon=True)
        self._lock = threading.RLock()
        self._last_submitted_ts: float | None = None
        self._closed = False
        self._started = False
        self._dead_reported = False
        self._submitted = 0
        self._completed = 0
        self._dropped = 0
        self._failed = 0
        self._consecutive_failures = 0

    def start(self) -> None:
        self._started = True
        self.thread.start()

    def submit(self, packet: FramePacket) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._started and not self.thread.is_alive():
                if not self._dead_reported:
                    self._dead_reported = True
                    self._health(
                        "pipeline_worker_dead",
                        {
                            "pipeline_id": self.spec.id,
                            "submitted_frames": self._submitted,
                            "completed_frames": self._completed,
                            "failed_frames": self._failed,
                        },
                    )
                return False
            interval = 1.0 / self.spec.inference_fps
            if self._last_submitted_ts is not None and packet.frame_ts - self._last_submitted_ts < interval - 1e-6:
                return False
            self._last_submitted_ts = packet.frame_ts
            self._submitted += 1
        try:
            self.queue.put_nowait(packet)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                self._dropped += 1
            self.queue.put_nowait(packet)
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(None)
        self.thread.join(timeout=10.0)
        if self.thread.is_alive():
            self._health(
                "pipeline_close_timeout",
                {"pipeline_id": self.spec.id, "timeout_s": 10.0},
            )

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "submitted_frames": self._submitted,
                "completed_frames": self._completed,
                "dropped_frames": self._dropped,
            }

    def _run(self) -> None:
        while True:
            packet = self.queue.get()
            if packet is None:
                return
            wait_started = self.perf_counter()
            try:
                with inference_guard(self.spec.device):
                    inference_started = self.perf_counter()
                    scheduler_wait_ms = (inference_started - wait_started) * 1000.0
                    detections = self.detector.detect(packet.frame_bgr)
                    latency_ms = (self.perf_counter() - inference_started) * 1000.0
                self._drain_detector_health()
                tracks = self.tracker.update(detections)
                completed_ts = self.clock()
                with self._lock:
                    self._completed += 1
                    counters = self.snapshot()
                observation = DetectionLayerObservation(
                    run_id=self.run_id,
                    pipeline_id=self.spec.id,
                    frame_index=packet.frame_index,
                    frame_ts=packet.frame_ts,
                    completed_ts=completed_ts,
                    inference_latency_ms=latency_ms,
                    detector_config={
                        "implementation": self.spec.detector,
                        "model": self.spec.model,
                        "targets": list(self.spec.targets),
                        "threshold": self.spec.threshold,
                        "text_threshold": self.spec.text_threshold,
                        "device": self.spec.device,
                        "inference_fps": self.spec.inference_fps,
                        "role": self.spec.role,
                        "input_size": self.spec.input_size,
                    },
                    tracker_config={"implementation": self.spec.tracker},
                    scheduler_wait_ms=scheduler_wait_ms,
                    detections=tuple(detections),
                    tracks=tuple(tracks),
                    **counters,
                )
                self.result_callback(observation)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._failed += 1
                    self._consecutive_failures += 1
                    consecutive_failures = self._consecutive_failures
                    failed_frames = self._failed
                self._health(
                    "pipeline_frame_failed",
                    {
                        "pipeline_id": self.spec.id,
                        "frame_index": packet.frame_index,
                        "error": repr(exc),
                        "consecutive_failures": consecutive_failures,
                        "failed_frames": failed_frames,
                    },
                )
                if consecutive_failures == 3:
                    self._health(
                        "pipeline_degraded",
                        {
                            "pipeline_id": self.spec.id,
                            "consecutive_failures": consecutive_failures,
                            "failed_frames": failed_frames,
                        },
                    )
                continue
            with self._lock:
                recovered_failures = self._consecutive_failures
                self._consecutive_failures = 0
            if recovered_failures:
                self._health(
                    "pipeline_recovered",
                    {
                        "pipeline_id": self.spec.id,
                        "frame_index": packet.frame_index,
                        "consecutive_failures": recovered_failures,
                        "failed_frames": self._failed,
                    },
                )

    def _drain_detector_health(self) -> None:
        drain = getattr(self.detector, "drain_health_events", None)
        if not callable(drain):
            return
        for event, data in drain():
            self._health(event, {"pipeline_id": self.spec.id, **dict(data)})

    def _health(self, event: str, data: Mapping[str, Any]) -> None:
        if self.health_callback is None:
            return
        try:
            self.health_callback(event, data)
        except Exception:
            # A diagnosis sink must never terminate the inference worker it observes.
            return


def _align_grounding_dino_output(
    *,
    box_count: int,
    score_count: int,
    labels: Any,
    fallback_label: str,
) -> tuple[int, list[str], dict[str, Any] | None]:
    label_values = [str(label) for label in labels]
    detection_count = min(box_count, score_count)
    aligned_labels = label_values[:detection_count]
    if len(aligned_labels) < detection_count:
        aligned_labels.extend([fallback_label] * (detection_count - len(aligned_labels)))
    if box_count == score_count == len(label_values):
        return detection_count, aligned_labels, None
    return (
        detection_count,
        aligned_labels,
        {
            "box_count": box_count,
            "score_count": score_count,
            "label_count": len(label_values),
            "emitted_count": detection_count,
            "fallback_label": fallback_label,
        },
    )


def _nms(detections: list[LayerDetection], *, threshold: float) -> list[LayerDetection]:
    remaining = sorted(detections, key=lambda item: item.confidence, reverse=True)
    kept: list[LayerDetection] = []
    while remaining:
        candidate = remaining.pop(0)
        kept.append(candidate)
        remaining = [item for item in remaining if _iou(candidate.box, item.box) < threshold]
    return [
        LayerDetection(
            detection_index=index,
            class_id=item.class_id,
            class_name=item.class_name,
            confidence=item.confidence,
            box=item.box,
        )
        for index, item in enumerate(kept)
    ]


def _iou(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0


def _resolve_yolo_model(model: str) -> str:
    configured = Path(model).expanduser()
    if configured.is_absolute() or configured.parent != Path("."):
        return str(configured)
    cached = Path.home() / ".cache" / "reachy_mini" / "models" / configured.name
    return str(cached) if cached.is_file() else model
