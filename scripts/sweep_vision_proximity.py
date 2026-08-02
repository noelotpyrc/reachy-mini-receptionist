#!/usr/bin/env python3
"""Sweep visitor proximity thresholds over one recorded video clip."""

from __future__ import annotations

import argparse
import copy
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from reachy_mini_brain.official_runtime.perception import ApproachTracker, PersonDetector
from reachy_mini_brain.official_runtime.visitor_triggers import HeightSignalConfig, VisitorTriggerConfig


@dataclass
class Candidate:
    near_exit: float
    near_enter: float
    proximity_persist: float
    approach_slope: float
    recede_slope: float
    motion_persist: float
    ema_alpha: float
    goodbye_confirm: float
    goodbye_additional_shrink: float
    tracker: ApproachTracker
    events: list[dict[str, object]] = field(default_factory=list)
    transitions: list[dict[str, object]] = field(default_factory=list)
    frame_trace: list[dict[str, object]] = field(default_factory=list)


def _floats(value: str) -> list[float]:
    return [float(item) for item in value.split(",")]


def _event_kinds(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--near-exit", type=_floats, required=True)
    parser.add_argument("--near-enter", type=_floats, required=True)
    parser.add_argument("--proximity-persist", type=_floats, default=[0.4])
    parser.add_argument("--approach-slope", type=_floats, default=[0.2])
    parser.add_argument("--recede-slope", type=_floats, default=[-0.2])
    parser.add_argument("--motion-persist", type=_floats, default=[0.2])
    parser.add_argument("--ema-alpha", type=_floats, default=[0.5])
    parser.add_argument("--goodbye-confirm", type=_floats, default=[0.6])
    parser.add_argument("--goodbye-additional-shrink", type=_floats, default=[0.03])
    parser.add_argument("--expect", type=_event_kinds, default=[])
    parser.add_argument("--detector-threshold", type=float, default=0.5)
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--trace-from", type=int)
    parser.add_argument("--timestamps", type=Path)
    parser.add_argument("--timestamp-start-frame", type=int, default=0)
    args = parser.parse_args()

    cap = cv2.VideoCapture(str(args.video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 5.0
    timestamps: list[float] | None = None
    if args.timestamps is not None:
        with args.timestamps.open("r", encoding="utf-8") as handle:
            source_timestamps = [
                float(row["ts"])
                for line in handle
                if line.strip()
                for row in [json.loads(line)]
                if row.get("type") in {"frame", "vision_frame"} and "ts" in row
            ]
        timestamps = source_timestamps[args.timestamp_start_frame :]
    ok, first = cap.read()
    if not ok:
        raise RuntimeError(f"could not decode {args.video}")
    height, width = first.shape[:2]

    candidates = [
        Candidate(
            near_exit=near_exit,
            near_enter=near_enter,
            proximity_persist=proximity_persist,
            approach_slope=approach_slope,
            recede_slope=recede_slope,
            motion_persist=motion_persist,
            ema_alpha=ema_alpha,
            goodbye_confirm=goodbye_confirm,
            goodbye_additional_shrink=goodbye_additional_shrink,
            tracker=ApproachTracker(
                (width, height),
                trigger_config=VisitorTriggerConfig(
                    near_exit_height=near_exit,
                    near_enter_height=near_enter,
                    proximity_persist_s=proximity_persist,
                    goodbye_confirm_s=goodbye_confirm,
                    goodbye_additional_shrink=goodbye_additional_shrink,
                    height_signal=HeightSignalConfig(
                        approach_slope=approach_slope,
                        recede_slope=recede_slope,
                        motion_persist_s=motion_persist,
                        ema_alpha=ema_alpha,
                    ),
                ),
            ),
        )
        for near_exit in args.near_exit
        for near_enter in args.near_enter
        for proximity_persist in args.proximity_persist
        for approach_slope in args.approach_slope
        for recede_slope in args.recede_slope
        for motion_persist in args.motion_persist
        for ema_alpha in args.ema_alpha
        for goodbye_confirm in args.goodbye_confirm
        for goodbye_additional_shrink in args.goodbye_additional_shrink
        if near_exit < near_enter
        and approach_slope > 0.0
        and recede_slope < 0.0
        and 0.0 < ema_alpha <= 1.0
    ]
    if args.trace_from is not None and len(candidates) != 1:
        parser.error("--trace-from requires exactly one threshold pair")
    detector = PersonDetector(threshold=args.detector_threshold)
    frame_idx = 0
    frame = first
    while True:
        if timestamps is not None:
            if frame_idx >= len(timestamps):
                raise RuntimeError("timestamp source has fewer rows than the video clip")
            ts = timestamps[frame_idx] - timestamps[0]
        else:
            ts = frame_idx / fps
        persons = detector.detect(frame, bgr=True)
        for candidate in candidates:
            events = candidate.tracker.update(copy.deepcopy(persons), ts=ts)
            for event in events:
                candidate.events.append({"frame": frame_idx, "ts": round(ts, 3), **event})
            state = candidate.tracker.debug_state
            if args.trace_from is not None and frame_idx >= args.trace_from:
                track = candidate.tracker.frame_debug[0] if candidate.tracker.frame_debug else {}
                candidate.frame_trace.append(
                    {
                        "frame": frame_idx,
                        "ts": round(ts, 3),
                        "raw_height": track.get("height"),
                        "filtered_height": state.get("height_filtered"),
                        "clipped": track.get("clipped"),
                        "tracking_source": track.get("tracking_source"),
                        "proximity": state.get("proximity"),
                        "motion": state.get("motion"),
                        "decision": state.get("trigger_decision"),
                    }
                )
            if state.get("proximity_change") or state.get("motion_change"):
                candidate.transitions.append(
                    {
                        "frame": frame_idx,
                        "ts": round(ts, 3),
                        "proximity_change": state.get("proximity_change"),
                        "motion_change": state.get("motion_change"),
                        "height": state.get("height_filtered"),
                    }
                )
        frame_idx += 1
        ok, frame = cap.read()
        if not ok:
            break
    cap.release()

    expected = args.expect
    passing = [
        candidate
        for candidate in candidates
        if [str(event["kind"]) for event in candidate.events] == expected
    ]
    outcome_groups: dict[tuple[tuple[str, ...], tuple[str, ...]], list[dict[str, float]]] = defaultdict(list)
    for candidate in candidates:
        event_sequence = tuple(str(event["kind"]) for event in candidate.events)
        proximity_sequence = tuple(
            str(item["proximity_change"])
            for item in candidate.transitions
            if item["proximity_change"] is not None
        )
        outcome_groups[(event_sequence, proximity_sequence)].append(_settings(candidate))
    result: dict[str, object] = {
        "video": str(args.video),
        "frames": frame_idx,
        "fps": fps,
        "timing": "recorded" if timestamps is not None else "nominal_fps",
        "expected": expected,
        "candidate_count": len(candidates),
        "passing_count": len(passing),
        "outcomes": [
            {
                "events": list(event_sequence),
                "proximity_transitions": list(proximity_sequence),
                "count": len(pairs),
                "settings": pairs,
            }
            for (event_sequence, proximity_sequence), pairs in outcome_groups.items()
        ],
        "passing": [
            {
                **_settings(candidate),
                "events": candidate.events if args.details else [event["kind"] for event in candidate.events],
                **({"transitions": candidate.transitions} if args.details else {}),
                **({"frame_trace": candidate.frame_trace} if args.trace_from is not None else {}),
            }
            for candidate in passing
        ],
    }
    if args.details:
        result["failures"] = [
            {
                **_settings(candidate),
                "events": candidate.events,
                "transitions": candidate.transitions,
                **({"frame_trace": candidate.frame_trace} if args.trace_from is not None else {}),
            }
            for candidate in candidates
            if candidate not in passing
        ]
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if passing else 1


def _settings(candidate: Candidate) -> dict[str, float]:
    return {
        "near_exit": candidate.near_exit,
        "near_enter": candidate.near_enter,
        "proximity_persist": candidate.proximity_persist,
        "approach_slope": candidate.approach_slope,
        "recede_slope": candidate.recede_slope,
        "motion_persist": candidate.motion_persist,
        "ema_alpha": candidate.ema_alpha,
        "goodbye_confirm": candidate.goodbye_confirm,
        "goodbye_additional_shrink": candidate.goodbye_additional_shrink,
    }


if __name__ == "__main__":
    raise SystemExit(main())
