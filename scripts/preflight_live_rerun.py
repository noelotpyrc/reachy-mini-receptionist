#!/usr/bin/env python3
"""Write a short synthetic live-detection stream through the real Rerun SDK."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np

from reachy_mini_brain.official_runtime.live_detection import (
    DetectionLayerObservation,
    FramePacket,
    LayerDetection,
    LayerTrack,
)
from reachy_mini_brain.official_runtime.live_rerun import LiveRerunPublisher, asdict_stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frames", type=int, default=15)
    parser.add_argument("--fps", type=float, default=5.0)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    if args.frames <= 0 or args.fps <= 0:
        parser.error("--frames and --fps must be positive")

    publisher = LiveRerunPublisher(
        mode="file",
        recording_id=f"live-rerun-preflight-{time.time_ns()}",
        save_path=args.output,
        image_fps=args.fps,
        queue_size=max(8, args.frames * 3),
    )
    publisher.start()
    start_ts = time.time()
    for frame_index in range(args.frames):
        frame_ts = start_ts + frame_index / args.fps
        frame = _frame(frame_index)
        packet = FramePacket(frame_index, frame_ts, frame)
        publisher.submit_frame(packet)
        if frame_index % max(1, round(args.fps)) == 0:
            publisher.submit_detection_layer(
                _layer("door_yolo_world", packet, confidence=0.72, latency_ms=20.0)
            )
            publisher.submit_detection_layer(
                _layer("door_grounding_dino", packet, confidence=0.91, latency_ms=505.0)
            )
    stats = publisher.close()
    print(f"live Rerun preflight -> {args.output}")
    print(asdict_stats(stats))
    return 0


def _frame(frame_index: int) -> np.ndarray:
    frame = np.full((360, 640, 3), 32, dtype=np.uint8)
    x1 = 80 + frame_index * 4
    frame[55:315, x1 : x1 + 145, 1] = 95
    frame[55:315, x1 : x1 + 145, 2] = 125
    return frame


def _layer(
    pipeline_id: str,
    packet: FramePacket,
    *,
    confidence: float,
    latency_ms: float,
) -> DetectionLayerObservation:
    x1 = 80.0 + packet.frame_index * 4.0
    box = (x1, 55.0, x1 + 145.0, 315.0)
    detection = LayerDetection(0, 0, "door", confidence, box)
    track = LayerTrack(1, 0, "door", confidence, box)
    return DetectionLayerObservation(
        run_id="synthetic-live-rerun",
        pipeline_id=pipeline_id,
        frame_index=packet.frame_index,
        frame_ts=packet.frame_ts,
        completed_ts=packet.frame_ts + latency_ms / 1000.0,
        inference_latency_ms=latency_ms,
        detector_config={"implementation": pipeline_id},
        tracker_config={"implementation": "bytetrack"},
        detections=(detection,),
        tracks=(track,),
        submitted_frames=packet.frame_index + 1,
        completed_frames=packet.frame_index + 1,
    )


if __name__ == "__main__":
    raise SystemExit(main())
