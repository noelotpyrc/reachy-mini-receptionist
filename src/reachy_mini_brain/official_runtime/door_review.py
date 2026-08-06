"""Generate a focused offline door-state review from recorded artifacts."""

from __future__ import annotations

import bisect
import hashlib
import json
import time
from dataclasses import dataclass
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
@click.option("--pipeline-id", default="door_grounding_dino", show_default=True)
@click.option("--jpeg-quality", type=click.IntRange(1, 100), default=85, show_default=True)
@click.option("--motion-enter-threshold", type=click.FloatRange(0.0, 1.0), default=0.10, show_default=True)
@click.option("--motion-exit-threshold", type=click.FloatRange(0.0, 1.0), default=0.035, show_default=True)
@click.option(
    "--relative-motion/--geometry-only",
    default=False,
    show_default=True,
    help="Enable relative door-leaf motion as an additive fallback to geometry.",
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
    pipeline_id: str,
    jpeg_quality: int,
    motion_enter_threshold: float,
    motion_exit_threshold: float,
    relative_motion: bool,
) -> None:
    """Create a focused Rerun artifact for one source-frame interval."""

    if to_frame < from_frame:
        raise click.ClickException("--to-frame must be greater than or equal to --from-frame")
    if output_dir.exists():
        raise click.ClickException(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)

    settings = DoorObserverSettings(
        motion_enter_threshold=motion_enter_threshold,
        motion_exit_threshold=motion_exit_threshold,
        relative_motion_enabled=relative_motion,
    )
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
        settings=settings,
        outputs={"frames": str(frames_path), "rrd": str(rrd_path)},
    )
    _write_json(manifest_path, manifest)

    observer = DoorMotionObserver(settings)
    policy_settings = DoorPolicySettings()
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

        cap = cv2.VideoCapture(str(video))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 5.0)
        with frames_path.open("x", encoding="utf-8") as trace:
            frame_index = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if frame_index > to_frame or frame_index > detection_frame_indices[-1]:
                    break
                if frame_index < from_frame:
                    frame_index += 1
                    continue
                frame_ts = frame_timestamps.get(frame_index, frame_index / fps)
                row = detection_rows.get(frame_index)
                decision_ts = _next_detection_completion_ts(
                    frame_index,
                    frame_ts,
                    detection_frame_indices=detection_frame_indices,
                    detection_rows=detection_rows,
                )
                observation = observer.update(
                    frame_index=frame_index,
                    frame_ts=frame_ts,
                    frame_bgr=frame,
                    door_detections=list(row.detections) if row is not None else None,
                    people=people.nearest(frame_ts),
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
        state_frame_counts=state_counts,
        policy_event_counts=event_counts,
        policy_settings=policy_settings.to_dict(),
    )
    _write_json(manifest_path, manifest)
    click.echo(f"door review -> {rrd_path}")
    click.echo(f"frame observations -> {frames_path}")
    click.echo(f"manifest -> {manifest_path}")


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
    settings: DoorObserverSettings,
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
            "observer": settings.to_dict(),
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
