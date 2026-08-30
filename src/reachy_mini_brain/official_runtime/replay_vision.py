"""Offline replay for reception perception clips."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import click

from .perception import GESTURE_RUNNING_MODES, WAVE_DETECTION_MODES
from .events import JsonlEventSink
from .visitor_trigger_profiles import LEGACY_VISITOR_TRIGGER_PROFILE, VISITOR_TRIGGER_PROFILE_NAMES


class _EmptyPersonDetector:
    def detect(self, frame: Any, *, bgr: bool = False) -> list[Any]:
        return []


class _EmptyPersonTracker:
    frame_debug: list[dict[str, Any]] = []
    debug_state: dict[str, Any] = {}
    last_track_boxes: list[Any] = []

    def update(self, persons: Any, *, ts: float | None = None) -> list[dict[str, Any]]:
        return []


def handle_replay_command(args: Any) -> int:
    """Replay a video through the reception perception pipeline."""
    import cv2

    from .perception import PerceptionPipeline
    from .rerun_vision import RerunVisionRenderer
    from .visitor_zones import TrackedPolygonZone, load_polygon_zone_config

    video = Path(args.video)
    output_dir = Path(args.output_dir) if getattr(args, "output_dir", None) else None
    if output_dir is not None:
        if output_dir.exists():
            raise click.ClickException(f"output directory already exists: {output_dir}")
        output_dir.mkdir(parents=True)

    events_path = _resolve_output_path(
        getattr(args, "events", None),
        output_dir / "events.jsonl" if output_dir is not None else None,
        fallback=Path(tempfile.gettempdir()) / f"reachy-reception-replay-{time.time_ns()}.jsonl",
    )
    trace_path = _resolve_output_path(
        getattr(args, "trace_jsonl", None),
        output_dir / "frames.jsonl" if output_dir is not None else None,
    )
    save_rrd = _resolve_output_path(
        getattr(args, "save_rrd", None),
        output_dir / "review.rrd" if output_dir is not None else None,
    )
    gesture_diagnostics_path = (
        output_dir / "gesture-diagnostics.jsonl"
        if output_dir is not None and args.gestures
        else None
    )
    annotate_path = Path(args.annotate) if args.annotate else None
    for path in (events_path, trace_path, save_rrd, gesture_diagnostics_path, annotate_path):
        if path is not None and path.exists():
            raise click.ClickException(f"refusing to overwrite existing output: {path}")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    run_id = output_dir.name if output_dir is not None else f"vision-replay-{time.time_ns()}"
    zone_config = (
        load_polygon_zone_config(args.doorway_zone_config)
        if getattr(args, "doorway_zone_config", None)
        else None
    )
    doorway_zone = TrackedPolygonZone(zone_config) if zone_config is not None else None
    pipe = PerceptionPipeline(
        events_path=events_path,
        threshold=args.threshold,
        smooth=args.smooth,
        gestures=args.gestures,
        gesture_running_mode=args.gesture_running_mode,
        wave_detection_mode=args.wave_detection_mode,
        detector=_EmptyPersonDetector() if args.gesture_only else None,
        tracker_factory=(lambda frame_wh: _EmptyPersonTracker()) if args.gesture_only else None,
        event_sink=(
            JsonlEventSink(gesture_diagnostics_path)
            if gesture_diagnostics_path is not None
            else None
        ),
        visitor_trigger_profile=args.visitor_trigger_profile,
        observation_mode="replay",
        observation_run_id=run_id,
        track_trail_window_s=args.track_trail_window_s,
        doorway_zone=doorway_zone,
    )

    cap = cv2.VideoCapture(str(video))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    reported_frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    source_frame_count = reported_frame_count if reported_frame_count > 0 else 0
    indexed_frames = []
    source_index = 0
    while True:
        if args.to_frame is not None and source_index > args.to_frame:
            break
        ok, frame = cap.read()
        if not ok:
            break
        if source_index >= args.from_frame:
            indexed_frames.append((source_index, frame))
        source_index += 1
    cap.release()
    decoded_frames = source_frame_count or source_index
    if args.reverse:
        if args.gestures and args.gesture_running_mode == "video":
            raise click.ClickException("VIDEO gesture mode does not support reverse replay")
        indexed_frames.reverse()

    frame_timestamps, timestamp_source = _load_replay_timestamps(args, video)
    if args.reverse:
        timestamp_source = "reverse_nominal_fps"

    writer = None
    if annotate_path is not None and indexed_frames:
        h, w = indexed_frames[0][1].shape[:2]
        out_fps = max(1.0, (src_fps or 5.0) / max(1, args.every))
        writer = cv2.VideoWriter(str(annotate_path), cv2.VideoWriter_fourcc(*"mp4v"), out_fps, (w, h))

    renderer = (
        RerunVisionRenderer(
            save_path=save_rrd,
            spawn=bool(args.spawn_rerun),
            recording_id=run_id,
            jpeg_quality=args.rerun_jpeg_quality,
        )
        if save_rrd is not None or args.spawn_rerun
        else None
    )
    manifest_path = output_dir / "replay-manifest.json" if output_dir is not None else None
    started_ts = time.time()
    manifest = _initial_manifest(
        run_id=run_id,
        video=video,
        decoded_frames=decoded_frames,
        src_fps=src_fps,
        timestamp_source=timestamp_source,
        args=args,
        pipe=pipe,
        zone_config=zone_config.to_dict() if zone_config is not None else None,
        outputs={
            "events": str(events_path),
            "frames": str(trace_path) if trace_path is not None else None,
            "rrd": str(save_rrd) if save_rrd is not None else None,
            "gesture_diagnostics": (
                str(gesture_diagnostics_path)
                if gesture_diagnostics_path is not None
                else None
            ),
            "annotated": str(annotate_path) if annotate_path is not None else None,
        },
        started_ts=started_ts,
    )
    if manifest_path is not None:
        _write_manifest(manifest_path, manifest)

    counts: dict[str, int] = {"approach": 0, "depart": 0, "wave": 0}
    processed = 0
    trace_file = trace_path.open("x", encoding="utf-8") if trace_path is not None else None
    try:
        for replay_index, (frame_index, frame) in enumerate(indexed_frames):
            if replay_index % args.every != 0:
                continue
            replay_ts = _replay_frame_ts(
                frame_index=frame_index,
                replay_index=replay_index,
                frame_timestamps=frame_timestamps,
                src_fps=src_fps,
                reverse=args.reverse,
            )
            processed += 1
            events, people, tracks = pipe.process(
                frame,
                bgr=True,
                ts=replay_ts,
                frame_index=frame_index,
                timestamp_source=timestamp_source,
            )
            observation = pipe.last_observation
            if observation is None:
                raise RuntimeError(f"no observation produced for frame {frame_index}")
            if trace_file is not None:
                trace_file.write(json.dumps(observation.to_dict(), sort_keys=True) + "\n")
            if renderer is not None:
                renderer.render(observation, frame)
            for event in events:
                kind = event["kind"]
                counts[kind] = counts.get(kind, 0) + 1
                print(f"frame {frame_index:4d}: {kind.upper()} {event}")
            if args.trace:
                for track in observation.tracks:
                    print(
                        f"  f{frame_index:4d} logical={track.logical_track_id} "
                        f"source={track.source_track_id} h={track.height:.3f} motion={track.motion}"
                    )
            if writer is not None:
                writer.write(_annotate_frame(frame, frame_index, people, tracks, pipe.debug_state, events))
    except Exception as exc:
        manifest.update(
            status="failed",
            finished_ts=round(time.time(), 3),
            processed_frames=processed,
            event_counts=counts,
            error=repr(exc),
        )
        if manifest_path is not None:
            _write_manifest(manifest_path, manifest)
        raise
    finally:
        if trace_file is not None:
            trace_file.close()
        if writer is not None:
            writer.release()
        if renderer is not None:
            renderer.close()

    manifest.update(
        status="completed",
        finished_ts=round(time.time(), 3),
        processed_frames=processed,
        event_counts=counts,
    )
    if manifest_path is not None:
        _write_manifest(manifest_path, manifest)
    if annotate_path is not None:
        print(f"annotated debug video -> {annotate_path}")

    print(
        f"=> {processed} frames processed | profile={args.visitor_trigger_profile} | smooth={args.smooth} | "
        f"approach={counts.get('approach', 0)} depart={counts.get('depart', 0)} wave={counts.get('wave', 0)}"
    )
    print(f"events -> {events_path}")
    if trace_path is not None:
        print(f"frame observations -> {trace_path}")
    if save_rrd is not None:
        print(f"Rerun review -> {save_rrd}")
    if manifest_path is not None:
        print(f"manifest -> {manifest_path}")

    ok = True
    for key, expected in (
        ("approach", args.expect_approach),
        ("depart", args.expect_depart),
        ("wave", args.expect_wave),
    ):
        if expected is None:
            continue
        got = counts.get(key, 0)
        flag = "ok" if got == expected else "FAIL"
        print(f"[{flag}] {key}: got {got}, expected {expected}")
        ok = ok and got == expected
    return 0 if ok else 1


@click.command()
@click.argument("video", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), default=None, help="New replay artifact directory; existing directories are rejected.")
@click.option("--events", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Output event JSONL path.")
@click.option("--trace-jsonl", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Output versioned per-frame observation JSONL.")
@click.option("--save-rrd", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Save the tracking review as a Rerun .rrd file.")
@click.option("--spawn-rerun", is_flag=True, default=False, help="Open a live Rerun viewer while replaying.")
@click.option("--rerun-jpeg-quality", type=click.IntRange(1, 100), default=85, show_default=True, help="JPEG quality for frames embedded in Rerun output.")
@click.option("--timestamp-sidecar", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Video frame timestamp JSONL.")
@click.option("--capture-jsonl", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Capture JSONL fallback for frame timestamps.")
@click.option("--doorway-zone-config", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Optional normalized doorway polygon JSON.")
@click.option("--threshold", type=float, default=0.5, show_default=True, help="Detector confidence threshold.")
@click.option("--smooth", type=int, default=0, show_default=True, help="Approach tracker smoothing window.")
@click.option("--track-trail-window-s", type=click.FloatRange(min=0.01), default=3.0, show_default=True, help="Image-plane movement trail duration.")
@click.option(
    "--visitor-trigger-profile",
    envvar="RECEPTION_VISITOR_TRIGGER_PROFILE",
    type=click.Choice(VISITOR_TRIGGER_PROFILE_NAMES),
    default=LEGACY_VISITOR_TRIGGER_PROFILE,
    show_default=True,
    help="Versioned greet/goodbye trigger implementation.",
)
@click.option("--gestures", is_flag=True, default=False, help="Enable wave detection.")
@click.option(
    "--gesture-only",
    is_flag=True,
    default=False,
    help="Skip person inference and evaluate only the selected wave detector.",
)
@click.option(
    "--gesture-running-mode",
    type=click.Choice(GESTURE_RUNNING_MODES),
    default="image",
    show_default=True,
    help="MediaPipe gesture recognizer running mode.",
)
@click.option(
    "--wave-detection-mode",
    type=click.Choice(WAVE_DETECTION_MODES),
    default="open_palm",
    show_default=True,
    help="Wave decision applied to MediaPipe output.",
)
@click.option("--every", type=int, default=1, show_default=True, help="Process every Nth frame.")
@click.option("--reverse", is_flag=True, default=False, help="Process frames in reverse.")
@click.option("--from-frame", type=int, default=0, show_default=True, help="Skip frames before this index.")
@click.option("--to-frame", type=int, default=None, help="Stop after this source frame index.")
@click.option("--trace", is_flag=True, default=False, help="Print per-frame track stats.")
@click.option("--annotate", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Output annotated debug video.")
@click.option("--expect-approach", type=int, default=None, help="Assert approach count.")
@click.option("--expect-depart", type=int, default=None, help="Assert depart count.")
@click.option("--expect-wave", type=int, default=None, help="Assert wave count.")
def cli(**kwargs: Any) -> None:
    """Replay recorded video through the reception perception pipeline."""

    args = SimpleNamespace(**kwargs)
    raise SystemExit(handle_replay_command(args))


def _resolve_output_path(explicit: Any, default: Path | None, *, fallback: Path | None = None) -> Path | None:
    if explicit is not None:
        return Path(explicit)
    return default if default is not None else fallback


def _load_replay_timestamps(args: Any, video: Path) -> tuple[dict[int, float], str]:
    sidecar = Path(args.timestamp_sidecar) if getattr(args, "timestamp_sidecar", None) else None
    if sidecar is None:
        candidate = video.with_suffix(".jsonl")
        if candidate.exists():
            sidecar = candidate
    if sidecar is not None:
        timestamps = _load_timestamp_jsonl(sidecar, allowed_types={None, "frame"})
        if timestamps:
            return timestamps, f"sidecar:{sidecar.name}"
    capture = Path(args.capture_jsonl) if getattr(args, "capture_jsonl", None) else None
    if capture is not None:
        timestamps = _load_timestamp_jsonl(capture, allowed_types={"vision_frame"})
        if timestamps:
            return timestamps, f"capture:{capture.name}"
    return {}, "nominal_fps"


def _load_timestamp_jsonl(path: Path, *, allowed_types: set[str | None]) -> dict[int, float]:
    timestamps: dict[int, float] = {}
    sequential_index = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("type") not in allowed_types or row.get("ts") is None:
            continue
        frame_index = int(row.get("frame_index", sequential_index))
        timestamps[frame_index] = float(row["ts"])
        sequential_index += 1
    return timestamps


def _replay_frame_ts(
    *,
    frame_index: int,
    replay_index: int,
    frame_timestamps: dict[int, float],
    src_fps: float,
    reverse: bool,
) -> float:
    fps = src_fps if src_fps > 0.0 else 5.0
    if not reverse and frame_index in frame_timestamps:
        return frame_timestamps[frame_index]
    return replay_index / fps


def _initial_manifest(
    *,
    run_id: str,
    video: Path,
    decoded_frames: int,
    src_fps: float,
    timestamp_source: str,
    args: Any,
    pipe: Any,
    zone_config: dict[str, Any] | None,
    outputs: dict[str, str | None],
    started_ts: float,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "started_ts": round(started_ts, 3),
        "source": {
            "path": str(video.resolve()),
            "sha256": _sha256(video),
            "bytes": video.stat().st_size,
            "decoded_frames": decoded_frames,
            "fps": src_fps,
            "timestamp_source": timestamp_source,
        },
        "configuration": {
            "detector_threshold": args.threshold,
            "tracker_smoothing_window": args.smooth,
            "visitor_profile": pipe.visitor_trigger_profile.metadata(smooth=args.smooth),
            "gestures": args.gestures,
            "gesture_only": args.gesture_only,
            "gesture_running_mode": args.gesture_running_mode,
            "wave_detection_mode": args.wave_detection_mode,
            "every": args.every,
            "reverse": args.reverse,
            "from_frame": args.from_frame,
            "to_frame": args.to_frame,
            "track_trail_window_s": args.track_trail_window_s,
            "rerun_jpeg_quality": args.rerun_jpeg_quality,
            "doorway_zone": zone_config,
        },
        "outputs": outputs,
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _annotate_frame(frame, idx: int, people: int, tracks: list[dict], state: dict, events: list[dict]):  # type: ignore[no-untyped-def]
    import cv2

    img = frame.copy()
    for track in tracks:
        x1, y1, x2, y2 = track.get("box", (0, 0, 0, 0))
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(
            img,
            f"id{track['id']} h={track.get('height', 0):.2f} {track.get('motion', 'UNKNOWN')}",
            (x1, max(12, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 200, 0),
            1,
            cv2.LINE_AA,
        )
    hud = (
        f"f{idx} people={people} presence={state.get('presence', 'ABSENT')} "
        f"proximity={state.get('proximity', 'UNKNOWN')} motion={state.get('motion', 'UNKNOWN')} "
        f"greet={state.get('greet')} depart={state.get('depart')} pending={state.get('goodbye_pending')}"
    )
    cv2.putText(img, hud, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, hud, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    for j, event in enumerate(events):
        cv2.putText(
            img,
            event["kind"].upper(),
            (8, 74 + j * 48),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.4,
            (0, 165, 255),
            4,
            cv2.LINE_AA,
        )
    return img
