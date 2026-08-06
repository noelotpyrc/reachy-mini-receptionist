#!/usr/bin/env python3
"""Render two-model door detections for one session in Rerun."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


@dataclass(frozen=True)
class Session:
    name: str
    video: Path
    landmark: Path
    yolo_detections: Path
    grounding_dino_detections: Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--session",
        nargs=5,
        metavar=("NAME", "VIDEO", "LANDMARK", "YOLO_JSONL", "GROUNDING_DINO_JSONL"),
        required=True,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jpeg-quality", type=int, default=85)
    args = parser.parse_args()

    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    if not 1 <= args.jpeg_quality <= 100:
        parser.error("--jpeg-quality must be between 1 and 100")

    name, *values = args.session
    session = Session(name, *(Path(value) for value in values))
    for path in (
        session.video,
        session.landmark,
        session.yolo_detections,
        session.grounding_dino_detections,
    ):
        if not path.is_file():
            parser.error(f"input does not exist: {path}")

    try:
        import rerun as rr
        import rerun.blueprint as rrb
    except ImportError as exc:
        raise RuntimeError("rerun-sdk is required") from exc

    blueprint = _blueprint(rrb, session)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rr.init(
        "reachy-mini-door-detector-comparison",
        recording_id=args.output.parent.name,
        default_blueprint=blueprint,
    )
    rr.save(str(args.output), default_blueprint=blueprint)

    _render_session(rr, session, jpeg_quality=args.jpeg_quality)
    print(f"Rerun comparison -> {args.output}")
    return 0


def _blueprint(rrb: Any, session: Session) -> Any:
    root = f"/door_review/{session.name}"
    view = rrb.Spatial2DView(
        origin=root,
        contents=[
            f"{root}/camera",
            f"{root}/approved_door_boundary",
            f"{root}/yolo_world/detections",
            f"{root}/grounding_dino/detections",
        ],
        name=f"{session.name} closed door",
    )
    return rrb.Blueprint(
        view,
        rrb.SelectionPanel(expanded=True),
        rrb.TimePanel(expanded=True, timeline="time_since_start"),
        auto_views=False,
        auto_layout=False,
    )


def _render_session(rr: Any, session: Session, *, jpeg_quality: int) -> None:
    yolo_rows = _load_rows(session.yolo_detections, session.name)
    grounding_rows = _load_rows(session.grounding_dino_detections, session.name)
    cap = cv2.VideoCapture(str(session.video))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 5.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    root = f"door_review/{session.name}"

    polygon = _load_polygon(session.landmark, width=width, height=height)
    rr.log(
        f"{root}/approved_door_boundary",
        rr.LineStrips2D([polygon + [polygon[0]]], colors=[[255, 196, 0]]),
        static=True,
    )

    frame_index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        rr.set_time("frame", sequence=frame_index)
        rr.set_time("time_since_start", duration=frame_index / fps)
        rr.log(f"{root}/camera", _encoded_image(rr, frame, jpeg_quality=jpeg_quality))
        _log_detections(
            rr,
            f"{root}/yolo_world/detections",
            yolo_rows.get(frame_index, []),
            color=[42, 190, 90],
            model_label="YOLO-World",
        )
        _log_detections(
            rr,
            f"{root}/grounding_dino/detections",
            grounding_rows.get(frame_index, []),
            color=[30, 190, 235],
            model_label="Grounding DINO",
        )
        frame_index += 1
    cap.release()
    if frame_index == 0:
        raise RuntimeError(f"no frames decoded from {session.video}")


def _load_rows(path: Path, session_name: str) -> dict[int, list[dict[str, Any]]]:
    rows: dict[int, list[dict[str, Any]]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("case") != session_name:
                continue
            frame_index = int(payload["frame_index"])
            if frame_index in rows:
                raise ValueError(f"duplicate frame {frame_index} at {path}:{line_number}")
            rows[frame_index] = list(payload.get("detections", []))
    return rows


def _load_polygon(path: Path, *, width: int, height: int) -> list[tuple[float, float]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    points = payload.get("polygon")
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError(f"invalid polygon in {path}")
    return [(float(point[0]) * width, float(point[1]) * height) for point in points]


def _encoded_image(rr: Any, frame_bgr: np.ndarray, *, jpeg_quality: int) -> Any:
    ok, encoded = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not ok:
        raise RuntimeError("failed to JPEG-encode frame")
    return rr.EncodedImage(contents=encoded.tobytes(), media_type="image/jpeg")


def _log_detections(
    rr: Any,
    entity: str,
    detections: list[dict[str, Any]],
    *,
    color: list[int],
    model_label: str,
) -> None:
    if not detections:
        rr.log(entity, rr.Clear(recursive=True))
        return
    boxes = [tuple(float(value) for value in detection["box"]) for detection in detections]
    mins = [[x1, y1] for x1, y1, _, _ in boxes]
    sizes = [[x2 - x1, y2 - y1] for x1, y1, x2, y2 in boxes]
    labels = [
        f"{model_label}: {detection['label']} {float(detection['confidence']):.2f}"
        for detection in detections
    ]
    rr.log(
        entity,
        rr.Boxes2D(
            mins=mins,
            sizes=sizes,
            labels=labels,
            colors=[color] * len(detections),
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
