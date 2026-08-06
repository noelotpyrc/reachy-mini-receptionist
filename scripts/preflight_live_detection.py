#!/usr/bin/env python3
"""Replay video frames through configured live detectors and a real Rerun file sink."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2

from reachy_mini_brain.official_runtime.live_detection import (
    DetectionLayerObservation,
    FramePacket,
    LiveDetectionManager,
    load_pipeline_config,
)
from reachy_mini_brain.official_runtime.live_rerun import LiveRerunPublisher, asdict_stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=15)
    parser.add_argument("--grpc-url")
    args = parser.parse_args()
    summary_path = args.output.with_suffix(".json")
    for output in (args.output, summary_path):
        if output.exists():
            parser.error(f"output already exists: {output}")
    if args.frames <= 0:
        parser.error("--frames must be positive")

    config = load_pipeline_config(args.config)
    publisher = LiveRerunPublisher(
        mode="grpc+file" if args.grpc_url else "file",
        recording_id=f"live-detection-preflight-{time.time_ns()}",
        grpc_url=args.grpc_url,
        save_path=args.output,
        image_fps=5.0,
        queue_size=max(16, args.frames * 3),
    )
    results: list[DetectionLayerObservation] = []

    def result_callback(observation: DetectionLayerObservation) -> None:
        results.append(observation)
        publisher.submit_detection_layer(observation)

    publisher.start()
    manager = LiveDetectionManager(
        run_id="live-detection-preflight",
        config=config,
        result_callback=result_callback,
    )
    manager.start()
    cap = cv2.VideoCapture(str(args.video))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 5.0)
    started_ts = time.time()
    frame_count = 0
    try:
        while frame_count < args.frames:
            ok, frame = cap.read()
            if not ok:
                break
            packet = FramePacket(frame_count, started_ts + frame_count / fps, frame.copy())
            publisher.submit_frame(packet)
            manager.submit(packet)
            frame_count += 1
            time.sleep(1.0 / fps)
        _wait_for_results(manager, timeout_s=15.0)
    finally:
        cap.release()
        manager.close()
        rerun_stats = publisher.close()

    summary = {
        "schema_version": 1,
        "config": config.to_dict(),
        "video": str(args.video),
        "frames": frame_count,
        "pipelines": manager.snapshot(),
        "results": [item.to_dict() for item in results],
        "rerun": asdict_stats(rerun_stats),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"live detector preflight -> {args.output}")
    print(json.dumps({"pipelines": summary["pipelines"], "rerun": summary["rerun"]}, indent=2))
    return 0


def _wait_for_results(manager: LiveDetectionManager, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snapshots = manager.snapshot().values()
        if all(item["completed_frames"] == item["submitted_frames"] for item in snapshots):
            return
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for detector results: {manager.snapshot()}")


if __name__ == "__main__":
    raise SystemExit(main())
