#!/usr/bin/env python3
"""Benchmark dynamic door detectors against approved landmark annotations."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np


DEFAULT_MODELS = {
    "yolo-world": "yolov8s-worldv2.pt",
    "grounding-dino": "IDEA-Research/grounding-dino-tiny",
}


@dataclass(frozen=True)
class Case:
    name: str
    video: Path
    landmark: Path


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]
    confidence: float
    label: str


class Detector(Protocol):
    def detect(self, frame_bgr: np.ndarray) -> tuple[list[Detection], float]: ...


class YoloWorldDetector:
    def __init__(
        self,
        *,
        model_id: str,
        prompts: list[str],
        threshold: float,
        device: str,
    ) -> None:
        from ultralytics import YOLO

        self._model = YOLO(model_id)
        self._model.set_classes(prompts)
        self._threshold = threshold
        self._device = device

    def detect(self, frame_bgr: np.ndarray) -> tuple[list[Detection], float]:
        _synchronize(self._device)
        started = time.perf_counter()
        result = self._model.predict(
            frame_bgr,
            conf=self._threshold,
            device=self._device,
            verbose=False,
            agnostic_nms=True,
        )[0]
        _synchronize(self._device)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        detections = []
        if result.boxes is not None:
            boxes = result.boxes.xyxy.detach().cpu().numpy()
            scores = result.boxes.conf.detach().cpu().numpy()
            classes = result.boxes.cls.detach().cpu().numpy().astype(int)
            detections = [
                Detection(
                    box=tuple(float(value) for value in box),
                    confidence=float(score),
                    label=str(result.names[int(class_id)]),
                )
                for box, score, class_id in zip(boxes, scores, classes, strict=True)
            ]
        return detections, elapsed_ms


class GroundingDinoDetector:
    def __init__(
        self,
        *,
        model_id: str,
        prompts: list[str],
        threshold: float,
        text_threshold: float,
        device: str,
    ) -> None:
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        self._torch = torch
        self._processor = AutoProcessor.from_pretrained(model_id)
        self._model = AutoModelForZeroShotObjectDetection.from_pretrained(model_id).to(device)
        self._model.eval()
        self._prompt = ". ".join(prompts) + "."
        self._threshold = threshold
        self._text_threshold = text_threshold
        self._device = device

    def detect(self, frame_bgr: np.ndarray) -> tuple[list[Detection], float]:
        image_rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
        height, width = image_rgb.shape[:2]
        inputs = self._processor(images=image_rgb, text=self._prompt, return_tensors="pt").to(
            self._device
        )
        _synchronize(self._device)
        started = time.perf_counter()
        with self._torch.inference_mode():
            outputs = self._model(**inputs)
        _synchronize(self._device)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        result = self._processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            threshold=self._threshold,
            text_threshold=self._text_threshold,
            target_sizes=[(height, width)],
        )[0]
        labels = result.get("text_labels", result.get("labels", []))
        detections = [
            Detection(
                box=tuple(float(value) for value in box.tolist()),
                confidence=float(score),
                label=str(label),
            )
            for box, score, label in zip(result["boxes"], result["scores"], labels, strict=True)
        ]
        return _nms(detections, threshold=0.5), elapsed_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--detector", choices=sorted(DEFAULT_MODELS), required=True)
    parser.add_argument("--model")
    parser.add_argument(
        "--case",
        action="append",
        nargs=3,
        metavar=("NAME", "VIDEO", "LANDMARK_JSON"),
        required=True,
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prompt", action="append", default=[])
    parser.add_argument("--threshold", type=float, default=0.15)
    parser.add_argument("--text-threshold", type=float, default=0.15)
    parser.add_argument("--device", default="mps")
    parser.add_argument("--every", type=int, default=5)
    args = parser.parse_args()

    if args.output_dir.exists():
        parser.error(f"output directory already exists: {args.output_dir}")
    if args.every <= 0:
        parser.error("--every must be positive")
    cases = [Case(name, Path(video), Path(landmark)) for name, video, landmark in args.case]
    for case in cases:
        if not case.video.is_file():
            parser.error(f"video does not exist: {case.video}")
        if not case.landmark.is_file():
            parser.error(f"landmark does not exist: {case.landmark}")

    prompts = args.prompt or ["door", "doorway", "entrance door"]
    model_id = args.model or DEFAULT_MODELS[args.detector]
    args.output_dir.mkdir(parents=True)
    model_started = time.perf_counter()
    detector = _build_detector(
        detector_name=args.detector,
        model_id=model_id,
        prompts=prompts,
        threshold=args.threshold,
        text_threshold=args.text_threshold,
        device=args.device,
    )
    model_load_s = time.perf_counter() - model_started

    rows: list[dict[str, Any]] = []
    for case in cases:
        rows.extend(
            _run_case(
                detector=detector,
                detector_name=args.detector,
                case=case,
                output_dir=args.output_dir,
                every=args.every,
            )
        )

    summary = {
        "schema_version": 1,
        "detector": args.detector,
        "model": model_id,
        "prompts": prompts,
        "threshold": args.threshold,
        "text_threshold": args.text_threshold,
        "device": args.device,
        "every": args.every,
        "model_load_s": round(model_load_s, 3),
        "cases": [_summarize_case(case, rows) for case in cases],
    }
    (args.output_dir / "detections.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _build_detector(
    *,
    detector_name: str,
    model_id: str,
    prompts: list[str],
    threshold: float,
    text_threshold: float,
    device: str,
) -> Detector:
    if detector_name == "yolo-world":
        return YoloWorldDetector(
            model_id=model_id,
            prompts=prompts,
            threshold=threshold,
            device=device,
        )
    return GroundingDinoDetector(
        model_id=model_id,
        prompts=prompts,
        threshold=threshold,
        text_threshold=text_threshold,
        device=device,
    )


def _run_case(
    *,
    detector: Detector,
    detector_name: str,
    case: Case,
    output_dir: Path,
    every: int,
) -> list[dict[str, Any]]:
    cap = cv2.VideoCapture(str(case.video))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 5.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    polygon = _load_polygon(case.landmark, width=width, height=height)
    ground_truth_box = _polygon_box(polygon)
    case_output = output_dir / case.name
    case_output.mkdir()
    rows: list[dict[str, Any]] = []
    frame_index = 0
    warmed_up = False
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_index % every:
            frame_index += 1
            continue
        if not warmed_up:
            detector.detect(frame)
            warmed_up = True
        detections, latency_ms = detector.detect(frame)
        ious = [_iou(detection.box, ground_truth_box) for detection in detections]
        best_index = int(np.argmax(ious)) if ious else None
        best_iou = ious[best_index] if best_index is not None else 0.0
        row = {
            "detector": detector_name,
            "case": case.name,
            "frame_index": frame_index,
            "frame_ts": round(frame_index / fps, 3),
            "latency_ms": round(latency_ms, 3),
            "ground_truth_box": [round(value, 2) for value in ground_truth_box],
            "best_detection_index": best_index,
            "best_iou": round(best_iou, 4),
            "detections": [
                {
                    "box": [round(value, 2) for value in detection.box],
                    "confidence": round(detection.confidence, 4),
                    "label": detection.label,
                    "iou": round(ious[index], 4),
                }
                for index, detection in enumerate(detections)
            ],
        }
        rows.append(row)
        annotated = _annotate(frame, polygon, detections, best_index, frame_index, latency_ms)
        cv2.imwrite(str(case_output / f"frame-{frame_index:04d}.jpg"), annotated)
        frame_index += 1
    cap.release()
    if not rows:
        raise RuntimeError(f"no frames decoded from {case.video}")
    return rows


def _summarize_case(case: Case, rows: list[dict[str, Any]]) -> dict[str, Any]:
    selected = [row for row in rows if row["case"] == case.name]
    latencies = [float(row["latency_ms"]) for row in selected]
    best_ious = [float(row["best_iou"]) for row in selected]
    return {
        "name": case.name,
        "video": str(case.video),
        "video_sha256": _sha256(case.video),
        "landmark": str(case.landmark),
        "sampled_frames": len(selected),
        "frames_with_detection": sum(bool(row["detections"]) for row in selected),
        "frames_iou_at_least_0_3": sum(value >= 0.3 for value in best_ious),
        "frames_iou_at_least_0_5": sum(value >= 0.5 for value in best_ious),
        "best_iou_median": round(statistics.median(best_ious), 4),
        "best_iou_max": round(max(best_ious), 4),
        "latency_ms_p50": round(statistics.median(latencies), 3),
        "latency_ms_p95": round(_percentile(latencies, 0.95), 3),
    }


def _load_polygon(path: Path, *, width: int, height: int) -> np.ndarray:
    payload = json.loads(path.read_text(encoding="utf-8"))
    points = payload.get("polygon")
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError(f"invalid polygon in {path}")
    return np.asarray(
        [(float(point[0]) * width, float(point[1]) * height) for point in points],
        dtype=np.float32,
    )


def _polygon_box(polygon: np.ndarray) -> tuple[float, float, float, float]:
    return (
        float(np.min(polygon[:, 0])),
        float(np.min(polygon[:, 1])),
        float(np.max(polygon[:, 0])),
        float(np.max(polygon[:, 1])),
    )


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


def _nms(detections: list[Detection], *, threshold: float) -> list[Detection]:
    remaining = sorted(detections, key=lambda item: item.confidence, reverse=True)
    kept: list[Detection] = []
    while remaining:
        candidate = remaining.pop(0)
        kept.append(candidate)
        remaining = [item for item in remaining if _iou(candidate.box, item.box) < threshold]
    return kept


def _annotate(
    frame: np.ndarray,
    polygon: np.ndarray,
    detections: list[Detection],
    best_index: int | None,
    frame_index: int,
    latency_ms: float,
) -> np.ndarray:
    image = frame.copy()
    cv2.polylines(image, [polygon.astype(np.int32)], True, (0, 210, 255), 3, cv2.LINE_AA)
    for index, detection in enumerate(detections):
        x1, y1, x2, y2 = (int(round(value)) for value in detection.box)
        color = (40, 210, 90) if index == best_index else (50, 80, 230)
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 3)
        cv2.putText(
            image,
            f"{detection.label} {detection.confidence:.2f}",
            (max(0, x1), max(24, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            color,
            2,
            cv2.LINE_AA,
        )
    cv2.putText(
        image,
        f"frame={frame_index} latency={latency_ms:.1f}ms",
        (20, image.shape[0] - 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return image


def _synchronize(device: str) -> None:
    if device != "mps":
        return
    import torch

    torch.mps.synchronize()


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
