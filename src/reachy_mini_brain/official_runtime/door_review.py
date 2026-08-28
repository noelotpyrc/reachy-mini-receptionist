"""Generate a focused offline door-state review from recorded artifacts."""

from __future__ import annotations

import bisect
import hashlib
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import click

from .door_observation import (
    DoorDetectionInput,
    DoorMotionObserver,
    DoorObserverSettings,
    PersonBoxInput,
)
from .door_review_rerun import DoorReviewRenderer
from .door_policy import DoorPolicySettings, DoorPolicyTriggerEngine
from .visitor_trigger_profiles import (
    DOOR_V2_20260809,
    VISITOR_TRIGGER_PROFILE_NAMES,
    resolve_visitor_trigger_profile,
)


@dataclass(frozen=True)
class DoorDetectionFrame:
    detections: tuple[DoorDetectionInput, ...]
    completed_ts: float | None
    inference_latency_ms: float | None


@dataclass(frozen=True)
class PersonTimeline:
    timestamps: tuple[float, ...]
    people: tuple[tuple[PersonBoxInput, ...], ...]
    max_age_s: float = 0.75

    def nearest(self, frame_ts: float) -> list[PersonBoxInput]:
        if not self.timestamps:
            return []
        index = bisect.bisect_left(self.timestamps, frame_ts)
        candidates = [item for item in (index - 1, index) if 0 <= item < len(self.timestamps)]
        nearest = min(candidates, key=lambda item: abs(self.timestamps[item] - frame_ts))
        if abs(self.timestamps[nearest] - frame_ts) > self.max_age_s:
            return []
        return list(self.people[nearest])


@click.command()
@click.option("--video", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option(
    "--timestamp-sidecar",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--door-detections",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--person-capture",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.option("--from-frame", type=click.IntRange(min=0), required=True)
@click.option("--to-frame", type=click.IntRange(min=0), required=True)
@click.option(
    "--source-frame-offset",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Add this offset when looking up original timestamp, person, and DINO sidecars.",
)
@click.option(
    "--warmup-source-frames",
    type=click.IntRange(min=0),
    default=600,
    show_default=True,
    help="Sidecar prehistory used to warm retained door, sequential baseline, and policy state.",
)
@click.option(
    "--visitor-trigger-profile",
    type=click.Choice(VISITOR_TRIGGER_PROFILE_NAMES),
    default=DOOR_V2_20260809,
    show_default=True,
)
@click.option("--pipeline-id", default="door_grounding_dino", show_default=True)
@click.option("--jpeg-quality", type=click.IntRange(1, 100), default=85, show_default=True)
@click.option("--motion-enter-threshold", type=click.FloatRange(0.0, 1.0), default=None)
@click.option("--motion-exit-threshold", type=click.FloatRange(0.0, 1.0), default=None)
@click.option(
    "--relative-motion/--geometry-only",
    default=None,
    help="Override the selected profile's relative door-leaf motion setting.",
)
@click.option(
    "--sequential-change/--single-threshold",
    default=None,
    help="Override the selected profile's sequential MOVING-entry setting.",
)
def cli(
    *,
    video: Path,
    timestamp_sidecar: Path,
    door_detections: Path,
    person_capture: Path,
    output_dir: Path,
    from_frame: int,
    to_frame: int,
    source_frame_offset: int,
    warmup_source_frames: int,
    visitor_trigger_profile: str,
    pipeline_id: str,
    jpeg_quality: int,
    motion_enter_threshold: float | None,
    motion_exit_threshold: float | None,
    relative_motion: bool | None,
    sequential_change: bool | None,
) -> None:
    """Create a focused Rerun artifact for one source-frame interval."""

    if to_frame < from_frame:
        raise click.ClickException("--to-frame must be greater than or equal to --from-frame")
    if output_dir.exists():
        raise click.ClickException(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)

    profile = resolve_visitor_trigger_profile(visitor_trigger_profile)
    if not profile.implementation.startswith("door_policy_"):
        raise click.ClickException(
            f"{visitor_trigger_profile} is not a door-policy visitor profile"
        )
    settings = DoorObserverSettings(**profile.parameters["door_observer"])
    settings = replace(
        settings,
        motion_enter_threshold=(
            settings.motion_enter_threshold
            if motion_enter_threshold is None
            else motion_enter_threshold
        ),
        motion_exit_threshold=(
            settings.motion_exit_threshold
            if motion_exit_threshold is None
            else motion_exit_threshold
        ),
        relative_motion_enabled=(
            settings.relative_motion_enabled if relative_motion is None else relative_motion
        ),
        sequential_change_enabled=(
            settings.sequential_change_enabled
            if sequential_change is None
            else sequential_change
        ),
    )
    policy_settings = DoorPolicySettings(**profile.parameters["door_policy"])
    frame_timestamps = _load_frame_timestamps(timestamp_sidecar)
    detection_rows = _load_detections(door_detections, pipeline_id=pipeline_id)
    detection_frame_indices = tuple(sorted(detection_rows))
    people = _load_people(person_capture)
    frames_path = output_dir / "frames.jsonl"
    rrd_path = output_dir / "review.rrd"
    manifest_path = output_dir / "manifest.json"
    recording_id = output_dir.name
    manifest = _manifest(
        video=video,
        timestamp_sidecar=timestamp_sidecar,
        door_detections=door_detections,
        person_capture=person_capture,
        pipeline_id=pipeline_id,
        frame_range=(from_frame, to_frame),
        source_frame_offset=source_frame_offset,
        warmup_source_frames=warmup_source_frames,
        visitor_profile=profile.metadata(),
        settings=settings,
        policy_settings=policy_settings,
        outputs={"frames": str(frames_path), "rrd": str(rrd_path)},
    )
    _write_json(manifest_path, manifest)

    observer = DoorMotionObserver(settings)
    policy = DoorPolicyTriggerEngine(policy_settings)
    renderer = DoorReviewRenderer(
        save_path=rrd_path,
        recording_id=recording_id,
        jpeg_quality=jpeg_quality,
    )
    processed = 0
    state_counts = {"UNKNOWN": 0, "STABLE": 0, "MOVING": 0}
    event_counts = {"approach": 0, "depart": 0}
    try:
        import cv2
        import numpy as np

        cap = cv2.VideoCapture(str(video))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 5.0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width <= 0 or height <= 0:
            raise RuntimeError(f"invalid video dimensions: {width}x{height}")
        warmup_frame = np.zeros((height, width, 3), dtype=np.uint8)
        warmup_indices = _warmup_frame_indices(
            source_frame_offset=source_frame_offset,
            warmup_source_frames=warmup_source_frames,
            timestamp_frame_indices=tuple(sorted(frame_timestamps)),
            detection_frame_indices=detection_frame_indices,
        )
        for source_frame_index in warmup_indices:
            frame_ts = frame_timestamps[source_frame_index]
            row = detection_rows.get(source_frame_index)
            frame_people = people.nearest(frame_ts)
            warmup_observation = observer.update(
                frame_index=source_frame_index,
                frame_ts=frame_ts,
                frame_bgr=warmup_frame,
                door_detections=list(row.detections) if row is not None else None,
                people=frame_people,
                occluders=frame_people,
                semantic_completed_ts=row.completed_ts if row is not None else None,
                semantic_inference_latency_ms=(
                    row.inference_latency_ms if row is not None else None
                ),
            )
            policy.update(
                warmup_observation,
                decision_ts=_next_detection_completion_ts(
                    source_frame_index,
                    frame_ts,
                    detection_frame_indices=detection_frame_indices,
                    detection_rows=detection_rows,
                ),
            )
        with frames_path.open("x", encoding="utf-8") as trace:
            frame_index = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                source_frame_index = frame_index + source_frame_offset
                if (
                    frame_index > to_frame
                    or source_frame_index > detection_frame_indices[-1]
                ):
                    break
                if frame_index < from_frame:
                    frame_index += 1
                    continue
                frame_ts = frame_timestamps.get(
                    source_frame_index,
                    source_frame_index / fps,
                )
                row = detection_rows.get(source_frame_index)
                decision_ts = _next_detection_completion_ts(
                    source_frame_index,
                    frame_ts,
                    detection_frame_indices=detection_frame_indices,
                    detection_rows=detection_rows,
                )
                frame_people = people.nearest(frame_ts)
                observation = observer.update(
                    frame_index=source_frame_index,
                    frame_ts=frame_ts,
                    frame_bgr=frame,
                    door_detections=list(row.detections) if row is not None else None,
                    people=frame_people,
                    occluders=frame_people,
                    semantic_completed_ts=row.completed_ts if row is not None else None,
                    semantic_inference_latency_ms=(
                        row.inference_latency_ms if row is not None else None
                    ),
                )
                policy_observation = policy.update(
                    observation,
                    decision_ts=decision_ts,
                )
                payload = observation.to_dict()
                payload["policy"] = policy_observation.to_dict()
                trace.write(json.dumps(payload, sort_keys=True) + "\n")
                renderer.render(observation, frame, policy=policy_observation)
                state_counts[observation.state] += 1
                for event in policy_observation.events:
                    event_counts[event["kind"]] += 1
                processed += 1
                frame_index += 1
        cap.release()
        if processed == 0:
            raise RuntimeError("no frames were decoded in the requested interval")
    except Exception as exc:
        manifest.update(status="failed", finished_ts=round(time.time(), 3), error=repr(exc))
        _write_json(manifest_path, manifest)
        raise
    finally:
        renderer.close()

    manifest.update(
        status="completed",
        finished_ts=round(time.time(), 3),
        processed_frames=processed,
        warmup_processed_frames=len(warmup_indices),
        state_frame_counts=state_counts,
        policy_event_counts=event_counts,
        policy_settings=policy_settings.to_dict(),
    )
    _write_json(manifest_path, manifest)
    click.echo(f"door review -> {rrd_path}")
    click.echo(f"frame observations -> {frames_path}")
    click.echo(f"manifest -> {manifest_path}")


def _warmup_frame_indices(
    *,
    source_frame_offset: int,
    warmup_source_frames: int,
    timestamp_frame_indices: tuple[int, ...],
    detection_frame_indices: tuple[int, ...],
    policy_tail_frames: int = 25,
) -> tuple[int, ...]:
    if source_frame_offset <= 0 or warmup_source_frames <= 0:
        return ()
    first = max(0, source_frame_offset - warmup_source_frames)
    tail_start = max(first, source_frame_offset - policy_tail_frames)
    timestamps = {
        frame_index
        for frame_index in timestamp_frame_indices
        if first <= frame_index < source_frame_offset
    }
    semantic = {
        frame_index
        for frame_index in detection_frame_indices
        if first <= frame_index < source_frame_offset and frame_index in timestamps
    }
    policy_tail = {frame_index for frame_index in timestamps if frame_index >= tail_start}
    return tuple(sorted(semantic | policy_tail))


def _load_frame_timestamps(path: Path) -> dict[int, float]:
    rows: dict[int, float] = {}
    for line_number, payload in _jsonl(path):
        if payload.get("type") != "frame":
            continue
        try:
            frame_index = int(payload["frame_index"])
            frame_ts = float(payload["ts"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid frame timestamp at {path}:{line_number}") from exc
        if frame_index in rows:
            raise ValueError(f"duplicate frame timestamp {frame_index} at {path}:{line_number}")
        rows[frame_index] = frame_ts
    if not rows:
        raise ValueError(f"no frame timestamps found in {path}")
    return rows


def _load_detections(path: Path, *, pipeline_id: str) -> dict[int, DoorDetectionFrame]:
    rows: dict[int, DoorDetectionFrame] = {}
    for line_number, payload in _jsonl(path):
        if payload.get("pipeline_id") != pipeline_id:
            continue
        frame_index = int(payload["frame_index"])
        if frame_index in rows:
            raise ValueError(f"duplicate {pipeline_id} frame {frame_index} at {path}:{line_number}")
        rows[frame_index] = DoorDetectionFrame(
            detections=tuple(
                DoorDetectionInput(
                    confidence=float(item["confidence"]),
                    box=tuple(float(value) for value in item["box"]),  # type: ignore[arg-type]
                )
                for item in payload.get("detections", [])
            ),
            completed_ts=(
                float(payload["completed_ts"])
                if payload.get("completed_ts") is not None
                else None
            ),
            inference_latency_ms=(
                float(payload["inference_latency_ms"])
                if payload.get("inference_latency_ms") is not None
                else None
            ),
        )
    if not rows:
        raise ValueError(f"no {pipeline_id!r} detections found in {path}")
    return rows


def _next_detection_completion_ts(
    frame_index: int,
    frame_ts: float,
    *,
    detection_frame_indices: tuple[int, ...],
    detection_rows: dict[int, DoorDetectionFrame],
) -> float:
    """Match live buffering: the next DINO result releases this source frame."""

    position = bisect.bisect_left(detection_frame_indices, frame_index)
    if position >= len(detection_frame_indices):
        return frame_ts
    detection = detection_rows[detection_frame_indices[position]]
    return max(frame_ts, detection.completed_ts or frame_ts)


def _load_people(path: Path) -> PersonTimeline:
    rows: list[tuple[float, tuple[PersonBoxInput, ...]]] = []
    for _, payload in _jsonl(path):
        if payload.get("type") != "vision_frame" or payload.get("ts") is None:
            continue
        tracks = tuple(
            PersonBoxInput(
                track_id=f"track-{int(item['id'])}",
                box=tuple(float(value) for value in item["box"]),  # type: ignore[arg-type]
            )
            for item in payload.get("tracks", [])
        )
        rows.append((float(payload["ts"]), tracks))
    rows.sort(key=lambda item: item[0])
    return PersonTimeline(
        timestamps=tuple(item[0] for item in rows),
        people=tuple(item[1] for item in rows),
    )


def _jsonl(path: Path):  # type: ignore[no-untyped-def]
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                yield line_number, json.loads(line)


def _manifest(
    *,
    video: Path,
    timestamp_sidecar: Path,
    door_detections: Path,
    person_capture: Path,
    pipeline_id: str,
    frame_range: tuple[int, int],
    source_frame_offset: int,
    warmup_source_frames: int,
    visitor_profile: dict[str, Any],
    settings: DoorObserverSettings,
    policy_settings: DoorPolicySettings,
    outputs: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "running",
        "started_ts": round(time.time(), 3),
        "source": {
            "video": _file_record(video),
            "timestamp_sidecar": _file_record(timestamp_sidecar),
            "door_detections": _file_record(door_detections),
            "person_capture": _file_record(person_capture),
        },
        "configuration": {
            "pipeline_id": pipeline_id,
            "from_frame": frame_range[0],
            "to_frame": frame_range[1],
            "source_frame_offset": source_frame_offset,
            "warmup_source_frames": warmup_source_frames,
            "source_from_frame": frame_range[0] + source_frame_offset,
            "source_to_frame": frame_range[1] + source_frame_offset,
            "visitor_trigger_profile": visitor_profile,
            "observer": settings.to_dict(),
            "policy": policy_settings.to_dict(),
        },
        "outputs": outputs,
    }


def _file_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    cli()
