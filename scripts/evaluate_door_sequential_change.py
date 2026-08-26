#!/usr/bin/env python3
"""Evaluate the sequential door-change detector against recorded live traces."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from reachy_mini_brain.official_runtime.door_observation import (
    DoorObserverSettings,
    SequentialDoorChangeDetector,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--terminal-grace-s", type=float, default=2.0)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    if args.terminal_grace_s < 0.0:
        parser.error("--terminal-grace-s must be non-negative")

    settings = DoorObserverSettings(sequential_change_enabled=True)
    results = [
        _evaluate_trace(path, settings, terminal_grace_s=args.terminal_grace_s)
        for path in args.traces
    ]
    payload = {
        "method": "robust_one_sided_cusum_v1",
        "settings": settings.to_dict(),
        "terminal_grace_s": args.terminal_grace_s,
        "traces": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)


def _evaluate_trace(
    path: Path,
    settings: DoorObserverSettings,
    *,
    terminal_grace_s: float,
) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    total_policy_rows = 0
    accepted_updates = 0
    for line_number, line in enumerate(path.open(encoding="utf-8"), start=1):
        payload = json.loads(line)
        if payload.get("type") != "vision.door_policy":
            continue
        total_policy_rows += 1
        door = payload.get("door", {})
        if not door.get("semantic_updated") or door.get("semantic_accepted") is False:
            continue
        try:
            frame_index = int(door["frame_index"])
            frame_ts = float(door["frame_ts"])
            score = float(door["motion_score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid door row at {path}:{line_number}") from exc
        accepted_updates += 1
        observations.append(
            {
                "frame_index": frame_index,
                "frame_ts": frame_ts,
                "motion_score": score,
                "baseline_eligible": not bool(door.get("people")),
            }
        )

    if not observations:
        raise ValueError(f"no accepted semantic door updates found in {path}")
    end_ts = float(observations[-1]["frame_ts"])
    triggers = []
    detector = SequentialDoorChangeDetector(settings)
    moving = False
    low_since: float | None = None
    for observation in observations:
        frame_ts = float(observation["frame_ts"])
        score = float(observation["motion_score"])
        if moving:
            if score <= settings.motion_exit_threshold:
                low_since = frame_ts if low_since is None else low_since
                if frame_ts - low_since >= settings.stable_dwell_s:
                    moving = False
                    low_since = None
                    detector.interrupt()
            else:
                low_since = None
            continue
        evidence = detector.update(
            score,
            baseline_eligible=bool(observation["baseline_eligible"]),
        )
        if not evidence.triggered:
            continue
        moving = True
        low_since = None
        seconds_from_end = end_ts - frame_ts
        triggers.append(
            {
                "frame_index": observation["frame_index"],
                "frame_ts": observation["frame_ts"],
                "seconds_from_end": round(seconds_from_end, 6),
                "motion_score": observation["motion_score"],
                "baseline": evidence.baseline,
                "noise_scale": evidence.noise_scale,
                "normalized_score": evidence.normalized_score,
                "accumulator": evidence.accumulator,
                "terminal": seconds_from_end <= terminal_grace_s,
            }
        )
        detector.interrupt()
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "policy_rows": total_policy_rows,
        "accepted_semantic_updates": accepted_updates,
        "trigger_count": len(triggers),
        "nonterminal_trigger_count": sum(not item["terminal"] for item in triggers),
        "triggers": triggers,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
