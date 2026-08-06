#!/usr/bin/env python3
"""Run Grounding DINO over a recorded frame interval using the live output contract."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import cv2
import numpy as np


class GroundingDinoDetector:
    def __init__(self, *, device: str, input_size: int | None) -> None:
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        model = "IDEA-Research/grounding-dino-tiny"
        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(model)
        self.model = AutoModelForZeroShotObjectDetection.from_pretrained(model).to(device)
        self.model.eval()
        self.device = device
        self.input_size = input_size
        self.prompt = "door. doorway. entrance door."

    def detect(self, frame_bgr):  # type: ignore[no-untyped-def]
        image_rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
        height, width = image_rgb.shape[:2]
        processor_kwargs = {}
        if self.input_size is not None:
            processor_kwargs["size"] = {
                "shortest_edge": self.input_size,
                "longest_edge": int(round(self.input_size * 1.66625)),
            }
        inputs = self.processor(
            images=image_rgb,
            text=self.prompt,
            return_tensors="pt",
            **processor_kwargs,
        ).to(self.device)
        with self.torch.inference_mode():
            outputs = self.model(**inputs)
        result = self.processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            threshold=0.30,
            text_threshold=0.15,
            target_sizes=[(height, width)],
        )[0]
        labels = result.get("text_labels", result.get("labels", []))
        rows = []
        for index, (box, score, label) in enumerate(
            zip(result["boxes"], result["scores"], labels, strict=True)
        ):
            rows.append(
                {
                    "detection_index": index,
                    "class_id": None,
                    "class_name": str(label),
                    "confidence": float(score),
                    "box": [float(value) for value in box.tolist()],
                }
            )
        return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--timestamp-sidecar", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--from-frame", type=int, required=True)
    parser.add_argument("--to-frame", type=int, required=True)
    parser.add_argument("--inference-fps", type=float, default=2.0)
    parser.add_argument("--input-size", type=int)
    parser.add_argument("--device", default="mps")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    if args.to_frame < args.from_frame:
        parser.error("--to-frame must be >= --from-frame")
    if args.inference_fps <= 0.0:
        parser.error("--inference-fps must be positive")

    timestamps = _load_timestamps(args.timestamp_sidecar) if args.timestamp_sidecar else {}
    load_started = time.perf_counter()
    detector = GroundingDinoDetector(device=args.device, input_size=args.input_size)
    model_load_s = time.perf_counter() - load_started
    cap = cv2.VideoCapture(str(args.video))
    source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 5.0)
    interval_s = 1.0 / args.inference_fps
    last_selected_ts: float | None = None
    warmed = False
    rows = []
    frame_index = 0
    while True:
        ok, frame = cap.read()
        if not ok or frame_index > args.to_frame:
            break
        if frame_index < args.from_frame:
            frame_index += 1
            continue
        frame_ts = timestamps.get(frame_index, frame_index / source_fps)
        if last_selected_ts is not None and frame_ts - last_selected_ts < interval_s - 1e-6:
            frame_index += 1
            continue
        if not warmed:
            detector.detect(frame)
            warmed = True
        started = time.perf_counter()
        detections = detector.detect(frame)
        latency_ms = (time.perf_counter() - started) * 1000.0
        completed_ts = frame_ts + latency_ms / 1000.0
        rows.append(
            {
                "schema_version": 1,
                "run_id": args.output.stem,
                "pipeline_id": "door_grounding_dino",
                "frame_index": frame_index,
                "frame_ts": frame_ts,
                "completed_ts": completed_ts,
                "inference_latency_ms": latency_ms,
                "scheduler_wait_ms": 0.0,
                "detector_config": {
                    "implementation": "grounding-dino",
                    "model": "IDEA-Research/grounding-dino-tiny",
                    "targets": ["door", "doorway", "entrance door"],
                    "threshold": 0.30,
                    "text_threshold": 0.15,
                    "device": args.device,
                    "inference_fps": args.inference_fps,
                    "input_size": args.input_size,
                    "role": "policy",
                },
                "tracker_config": {"implementation": "none"},
                "detections": [
                    {
                        **item,
                    }
                    for item in detections
                ],
                "tracks": [],
                "submitted_frames": len(rows) + 1,
                "completed_frames": len(rows) + 1,
                "dropped_frames": 0,
                "type": "detection_layer",
                "ts": completed_ts,
            }
        )
        last_selected_ts = frame_ts
        frame_index += 1
    cap.release()
    if not rows:
        raise RuntimeError("no frames selected")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    latencies = [row["inference_latency_ms"] for row in rows]
    summary = {
        "output": str(args.output),
        "selected_frames": len(rows),
        "frames_with_detection": sum(bool(row["detections"]) for row in rows),
        "model_load_s": round(model_load_s, 3),
        "latency_ms_p50": round(statistics.median(latencies), 3),
        "latency_ms_max": round(max(latencies), 3),
        "input_size": args.input_size,
        "inference_fps": args.inference_fps,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _load_timestamps(path: Path) -> dict[int, float]:
    timestamps = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("type") == "frame" and row.get("ts") is not None:
            timestamps[int(row["frame_index"])] = float(row["ts"])
    return timestamps


if __name__ == "__main__":
    raise SystemExit(main())
