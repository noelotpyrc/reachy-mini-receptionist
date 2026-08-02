#!/usr/bin/env python3
"""Prepare unlabeled vision-trigger review clips from recorded live sessions."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRIGGER_TYPES = {"greet", "farewell", "greet_suppressed", "farewell_suppressed"}
CONTEXT_TYPES = TRIGGER_TYPES | {"wave_received"}


@dataclass
class ClipSpec:
    clip_id: str
    run_id: str
    candidate_kind: str
    requested_start_ts: float
    requested_end_ts: float
    candidate_events: list[dict[str, Any]] = field(default_factory=list)
    context_events: list[dict[str, Any]] = field(default_factory=list)
    markers: list[dict[str, Any]] = field(default_factory=list)
    start_frame: int = 0
    end_frame: int = 0
    first_frame_ts: float = 0.0
    last_frame_ts: float = 0.0
    output_path: Path | None = None
    written_frames: int = 0


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}") from exc
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _policy_event(row: dict[str, Any], index: int) -> dict[str, Any]:
    event = row.get("event") if isinstance(row.get("event"), dict) else {}
    return {
        "candidate_id": f"event-{index:02d}",
        "ts": float(row["ts"]),
        "policy_type": str(row["type"]),
        "vision_kind": event.get("kind"),
        "track_id": event.get("id"),
        "old_area": event.get("area"),
        "ground_truth": "unreviewed",
        "review_notes": None,
    }


def _cluster_rows(rows: list[dict[str, Any]], gap_s: float) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for row in sorted(rows, key=lambda item: float(item["ts"])):
        if not clusters or float(row["ts"]) - float(clusters[-1][-1]["ts"]) > gap_s:
            clusters.append([row])
        else:
            clusters[-1].append(row)
    return clusters


def _load_frame_timestamps(
    *,
    sidecar_path: Path,
    capture_path: Path,
) -> tuple[list[float], str]:
    if sidecar_path.exists():
        indexed = []
        for row in _iter_jsonl(sidecar_path):
            if row.get("type") not in (None, "frame") or "ts" not in row:
                continue
            indexed.append((int(row.get("frame_index", len(indexed))), float(row["ts"])))
        if indexed:
            indexed.sort(key=lambda item: item[0])
            expected = list(range(len(indexed)))
            actual = [index for index, _ in indexed]
            if actual != expected:
                raise ValueError(f"Non-contiguous frame indexes in {sidecar_path}")
            return [ts for _, ts in indexed], f"video_sidecar:{sidecar_path.name}"

    timestamps = [
        float(row["ts"])
        for row in _iter_jsonl(capture_path)
        if row.get("type") == "vision_frame" and "ts" in row
    ]
    if not timestamps:
        raise ValueError(f"No frame timestamps found in {sidecar_path} or {capture_path}")
    return timestamps, f"capture_row_fallback:{capture_path.name}"


def _marker_paths(artifact_root: Path, run_id: str) -> list[Path]:
    artifact_parent = artifact_root.parent
    return [
        artifact_parent / f"markers-{run_id}.jsonl",
        artifact_parent / "vision-trigger-eval" / "source-markers" / f"markers-{run_id}.jsonl",
    ]


def _load_markers(artifact_root: Path, run_id: str) -> list[dict[str, Any]]:
    for path in _marker_paths(artifact_root, run_id):
        if path.exists():
            return _iter_jsonl(path)
    return []


def _build_specs(
    *,
    run_id: str,
    policy_rows: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    pre_roll_s: float,
    post_roll_s: float,
    cluster_gap_s: float,
    include_wave_only: bool,
) -> list[ClipSpec]:
    trigger_rows = [row for row in policy_rows if row.get("type") in TRIGGER_TYPES and "ts" in row]
    wave_rows = [row for row in policy_rows if row.get("type") == "wave_received" and "ts" in row]
    specs: list[ClipSpec] = []

    for cluster_index, cluster in enumerate(_cluster_rows(trigger_rows, cluster_gap_s), start=1):
        start_ts = float(cluster[0]["ts"]) - pre_roll_s
        end_ts = float(cluster[-1]["ts"]) + post_roll_s
        candidate_events = [_policy_event(row, index) for index, row in enumerate(cluster, start=1)]
        specs.append(
            ClipSpec(
                clip_id=f"{run_id}--trigger-{cluster_index:02d}",
                run_id=run_id,
                candidate_kind="old_policy_trigger_cluster",
                requested_start_ts=start_ts,
                requested_end_ts=end_ts,
                candidate_events=candidate_events,
            )
        )

    if include_wave_only:
        covered = [(spec.requested_start_ts, spec.requested_end_ts) for spec in specs]
        wave_only = [
            row
            for row in wave_rows
            if not any(start <= float(row["ts"]) <= end for start, end in covered)
        ]
        for wave_index, cluster in enumerate(_cluster_rows(wave_only, cluster_gap_s), start=1):
            specs.append(
                ClipSpec(
                    clip_id=f"{run_id}--wave-only-{wave_index:02d}",
                    run_id=run_id,
                    candidate_kind="no_old_greet_or_goodbye_near_wave",
                    requested_start_ts=float(cluster[0]["ts"]) - pre_roll_s,
                    requested_end_ts=float(cluster[-1]["ts"]) + post_roll_s,
                )
            )

    for spec in specs:
        spec.context_events = [
            {
                "ts": float(row["ts"]),
                "policy_type": str(row["type"]),
                "reason": row.get("reason"),
            }
            for row in policy_rows
            if row.get("type") in CONTEXT_TYPES
            and "ts" in row
            and spec.requested_start_ts <= float(row["ts"]) <= spec.requested_end_ts
        ]
        spec.markers = [
            {
                "ts": float(row["ts"]),
                "clock": row.get("clock"),
                "note": row.get("note"),
            }
            for row in markers
            if "ts" in row and spec.requested_start_ts <= float(row["ts"]) <= spec.requested_end_ts
        ]
    return sorted(specs, key=lambda spec: spec.requested_start_ts)


def _assign_frames(specs: list[ClipSpec], timestamps: list[float]) -> None:
    if not timestamps:
        raise ValueError("Cannot assign clips without frame timestamps")
    for spec in specs:
        start = min(max(0, bisect.bisect_left(timestamps, spec.requested_start_ts)), len(timestamps) - 1)
        end = min(max(start, bisect.bisect_right(timestamps, spec.requested_end_ts) - 1), len(timestamps) - 1)
        spec.start_frame = start
        spec.end_frame = end
        spec.first_frame_ts = timestamps[start]
        spec.last_frame_ts = timestamps[end]
        for event in spec.candidate_events:
            event_frame = min(
                max(start, bisect.bisect_left(timestamps, float(event["ts"]))),
                end,
            )
            event["source_frame"] = event_frame
            event["clip_frame"] = event_frame - start


def _decoded_frame_count(video_path: Path) -> tuple[int, float, int, int]:
    import cv2

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open video: {video_path}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 5.0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    count = 0
    while True:
        ok, _ = capture.read()
        if not ok:
            break
        count += 1
    capture.release()
    return count, fps, width, height


def _write_clips(
    *,
    video_path: Path,
    specs: list[ClipSpec],
    output_dir: Path,
    fps: float,
    width: int,
    height: int,
) -> None:
    import cv2

    output_dir.mkdir(parents=True, exist_ok=False)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open video: {video_path}")
    writers: dict[str, Any] = {}
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    try:
        frame_index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            for spec in specs:
                if spec.start_frame <= frame_index <= spec.end_frame:
                    writer = writers.get(spec.clip_id)
                    if writer is None:
                        output_path = output_dir / f"{spec.clip_id}.mp4"
                        writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
                        if not writer.isOpened():
                            raise RuntimeError(f"Unable to create clip: {output_path}")
                        writers[spec.clip_id] = writer
                        spec.output_path = output_path
                    writer.write(frame)
                    spec.written_frames += 1
                if frame_index == spec.end_frame and spec.clip_id in writers:
                    writers.pop(spec.clip_id).release()
            frame_index += 1
    finally:
        capture.release()
        for writer in writers.values():
            writer.release()

    for spec in specs:
        expected = spec.end_frame - spec.start_frame + 1
        if spec.output_path is None or spec.written_frames != expected:
            raise RuntimeError(
                f"Clip {spec.clip_id} wrote {spec.written_frames} frames; expected {expected}"
            )


def _verify_output_clips(specs: list[ClipSpec]) -> None:
    for spec in specs:
        assert spec.output_path is not None
        decoded, _, _, _ = _decoded_frame_count(spec.output_path)
        if decoded != spec.written_frames:
            raise RuntimeError(
                f"Clip {spec.output_path} decodes {decoded} frames; expected {spec.written_frames}"
            )


def _clip_payload(spec: ClipSpec, output_root: Path, fps: float, alignment_source: str) -> dict[str, Any]:
    assert spec.output_path is not None
    for event in spec.candidate_events:
        event["wall_offset_s"] = round(float(event["ts"]) - spec.first_frame_ts, 3)
        event["playback_offset_s"] = round(int(event["clip_frame"]) / fps, 3)
    return {
        "clip_id": spec.clip_id,
        "run_id": spec.run_id,
        "candidate_kind": spec.candidate_kind,
        "review_status": "unreviewed",
        "scenario_ground_truth": None,
        "review_notes": None,
        "requested_start_ts": round(spec.requested_start_ts, 3),
        "requested_end_ts": round(spec.requested_end_ts, 3),
        "first_frame_ts": round(spec.first_frame_ts, 3),
        "last_frame_ts": round(spec.last_frame_ts, 3),
        "start_frame": spec.start_frame,
        "end_frame": spec.end_frame,
        "frame_count": spec.written_frames,
        "output_fps": fps,
        "playback_timing": "constant nominal fps; wall timestamps used only for frame selection",
        "alignment_source": alignment_source,
        "path": str(spec.output_path.relative_to(output_root)),
        "sha256": _sha256(spec.output_path),
        "candidate_events": spec.candidate_events,
        "context_events": spec.context_events,
        "markers": spec.markers,
    }


def _write_review_csv(path: Path, clip_payloads: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "clip_id",
                "candidate_id",
                "candidate_kind",
                "policy_type",
                "clip_frame",
                "playback_offset_s",
                "wall_offset_s",
                "ground_truth",
                "review_notes",
            ],
        )
        writer.writeheader()
        for clip in clip_payloads:
            events = clip["candidate_events"] or [
                {
                    "candidate_id": "scenario",
                    "policy_type": "none",
                    "clip_frame": "",
                    "playback_offset_s": "",
                    "wall_offset_s": "",
                    "ground_truth": clip["scenario_ground_truth"] or "unreviewed",
                    "review_notes": clip["review_notes"] or "",
                }
            ]
            for event in events:
                writer.writerow(
                    {
                        "clip_id": clip["clip_id"],
                        "candidate_id": event["candidate_id"],
                        "candidate_kind": clip["candidate_kind"],
                        "policy_type": event["policy_type"],
                        "clip_frame": event["clip_frame"],
                        "playback_offset_s": event["playback_offset_s"],
                        "wall_offset_s": event["wall_offset_s"],
                        "ground_truth": event["ground_truth"],
                        "review_notes": event["review_notes"] or "",
                    }
                )


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    artifact_root = args.artifact_root.resolve()
    output_root = args.output_root.resolve()
    if output_root.exists():
        raise FileExistsError(f"Output already exists; choose a new directory: {output_root}")

    sessions = []
    all_specs: list[ClipSpec] = []
    session_inputs = []
    for run_id in args.run:
        video_path = artifact_root / "video" / f"video-{run_id}-01.mkv"
        sidecar_path = artifact_root / "video" / f"video-{run_id}-01.jsonl"
        capture_path = artifact_root / "capture" / f"capture-{run_id}-01.jsonl"
        policy_path = artifact_root / "policies" / f"policies-{run_id}-01.jsonl"
        for required in (video_path, capture_path, policy_path):
            if not required.exists():
                raise FileNotFoundError(required)

        timestamps, alignment_source = _load_frame_timestamps(
            sidecar_path=sidecar_path,
            capture_path=capture_path,
        )
        decoded_count, fps, width, height = _decoded_frame_count(video_path)
        timestamp_count = len(timestamps)
        alignment_warning = None
        if decoded_count > timestamp_count:
            raise ValueError(
                f"{run_id}: decoded {decoded_count} video frames but found only {timestamp_count} timestamps"
            )
        if decoded_count < timestamp_count:
            unmatched = timestamp_count - decoded_count
            alignment_warning = (
                f"Video decodes {decoded_count} frames; using the matching timestamp prefix and "
                f"ignoring {unmatched} trailing timestamp rows without decodable frames."
            )
            timestamps = timestamps[:decoded_count]
        policy_rows = _iter_jsonl(policy_path)
        specs = _build_specs(
            run_id=run_id,
            policy_rows=policy_rows,
            markers=_load_markers(artifact_root, run_id),
            pre_roll_s=args.pre_roll,
            post_roll_s=args.post_roll,
            cluster_gap_s=args.cluster_gap,
            include_wave_only=args.include_wave_only,
        )
        if not specs:
            raise ValueError(f"{run_id}: no review candidates found")
        _assign_frames(specs, timestamps)
        sessions.append(
            {
                "run_id": run_id,
                "video_path": str(video_path),
                "video_sha256": _sha256(video_path),
                "frame_count": decoded_count,
                "source_timestamp_count": timestamp_count,
                "first_frame_ts": timestamps[0],
                "last_frame_ts": timestamps[-1],
                "nominal_fps": fps,
                "alignment_source": alignment_source,
                "alignment_warning": alignment_warning,
                "policy_path": str(policy_path),
                "policy_sha256": _sha256(policy_path),
                "capture_path": str(capture_path),
                "capture_sha256": _sha256(capture_path),
            }
        )
        session_inputs.append((video_path, specs, fps, width, height, alignment_source))
        all_specs.extend(specs)

    output_root.mkdir(parents=True, exist_ok=False)
    for video_path, specs, fps, width, height, _ in session_inputs:
        _write_clips(
            video_path=video_path,
            specs=specs,
            output_dir=output_root / "clips" / specs[0].run_id,
            fps=fps,
            width=width,
            height=height,
        )
    _verify_output_clips(all_specs)

    alignment_by_run = {session["run_id"]: session["alignment_source"] for session in sessions}
    fps_by_run = {session["run_id"]: session["nominal_fps"] for session in sessions}
    clip_payloads = [
        _clip_payload(spec, output_root, fps_by_run[spec.run_id], alignment_by_run[spec.run_id])
        for spec in all_specs
    ]
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ground_truth_status": "unreviewed",
        "warning": "Old policy events select candidates only and are not ground truth.",
        "settings": {
            "pre_roll_s": args.pre_roll,
            "post_roll_s": args.post_roll,
            "cluster_gap_s": args.cluster_gap,
            "include_wave_only": args.include_wave_only,
        },
        "sessions": sessions,
        "clips": clip_payloads,
    }
    manifest_path = output_root / "review-manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_review_csv(output_root / "review-labels.csv", clip_payloads)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/official-runtime-live"))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--run", action="append", required=True, help="Run ID; repeat for each session.")
    parser.add_argument("--pre-roll", type=float, default=8.0)
    parser.add_argument("--post-roll", type=float, default=8.0)
    parser.add_argument("--cluster-gap", type=float, default=8.0)
    parser.add_argument("--include-wave-only", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    payload = prepare(args)
    print(f"Prepared {len(payload['clips'])} unlabeled clips in {args.output_root}")
    for clip in payload["clips"]:
        print(f"  {clip['clip_id']}: {clip['frame_count']} frames -> {clip['path']}")


if __name__ == "__main__":
    main()
