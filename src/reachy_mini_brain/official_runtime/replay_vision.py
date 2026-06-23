"""Offline replay for reception perception clips."""

from __future__ import annotations

import tempfile
from types import SimpleNamespace
from pathlib import Path
from typing import Any

import click


def handle_replay_command(args: Any) -> int:
    """Replay a video through the reception perception pipeline."""
    import cv2

    from .perception import PerceptionPipeline

    video = Path(args.video)
    events_path = Path(args.events) if args.events else Path(tempfile.gettempdir()) / "reachy_reception_replay.jsonl"
    pipe = PerceptionPipeline(
        events_path=events_path,
        threshold=args.threshold,
        smooth=args.smooth,
        gestures=args.gestures,
    )

    cap = cv2.VideoCapture(str(video))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    if args.reverse:
        frames.reverse()
    if args.from_frame:
        frames = frames[args.from_frame :]

    writer = None
    if args.annotate and frames:
        h, w = frames[0].shape[:2]
        out_fps = max(1.0, (src_fps or 5.0) / max(1, args.every))
        writer = cv2.VideoWriter(str(args.annotate), cv2.VideoWriter_fourcc(*"mp4v"), out_fps, (w, h))

    counts: dict[str, int] = {"approach": 0, "depart": 0, "wave": 0}
    processed = 0
    for idx, frame in enumerate(frames):
        if idx % args.every != 0:
            continue
        processed += 1
        events, people, tracks = pipe.process(frame, bgr=True)
        for event in events:
            kind = event["kind"]
            counts[kind] = counts.get(kind, 0) + 1
            print(f"frame {idx:4d}: {kind.upper()} {event}")
        if args.trace:
            for track in tracks:
                print(f"  f{idx:4d} id={track['id']} area={track['area']:.3f}")
        if writer is not None:
            writer.write(_annotate_frame(frame, idx, people, tracks, pipe.debug_state, events))

    if writer is not None:
        writer.release()
        print(f"annotated debug video -> {args.annotate}")

    print(
        f"=> {processed} frames processed | smooth={args.smooth} | "
        f"approach={counts.get('approach', 0)} depart={counts.get('depart', 0)} wave={counts.get('wave', 0)}"
    )
    print(f"events -> {events_path}")

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
@click.option("--events", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Output event JSONL path.")
@click.option("--threshold", type=float, default=0.5, show_default=True, help="Detector confidence threshold.")
@click.option("--smooth", type=int, default=0, show_default=True, help="Approach tracker smoothing window.")
@click.option("--gestures", is_flag=True, default=False, help="Enable wave detection.")
@click.option("--every", type=int, default=1, show_default=True, help="Process every Nth frame.")
@click.option("--reverse", is_flag=True, default=False, help="Process frames in reverse.")
@click.option("--from-frame", type=int, default=0, show_default=True, help="Skip frames before this index.")
@click.option("--trace", is_flag=True, default=False, help="Print per-frame track stats.")
@click.option("--annotate", type=click.Path(dir_okay=False, path_type=Path), default=None, help="Output annotated debug video.")
@click.option("--expect-approach", type=int, default=None, help="Assert approach count.")
@click.option("--expect-depart", type=int, default=None, help="Assert depart count.")
@click.option("--expect-wave", type=int, default=None, help="Assert wave count.")
def cli(**kwargs: Any) -> None:
    """Replay recorded video through the reception perception pipeline."""

    args = SimpleNamespace(**kwargs)
    raise SystemExit(handle_replay_command(args))


def _annotate_frame(frame, idx: int, people: int, tracks: list[dict], state: dict, events: list[dict]):  # type: ignore[no-untyped-def]
    import cv2

    img = frame.copy()
    for track in tracks:
        x1, y1, x2, y2 = track.get("box", (0, 0, 0, 0))
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(
            img,
            f"id{track['id']} a={track['area']:.2f}",
            (x1, max(12, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 200, 0),
            1,
            cv2.LINE_AA,
        )
    hud = (
        f"f{idx} people={people} dom={state.get('dom_area', 0):.3f} "
        f"peak={state.get('peak', 0):.3f} greet={state.get('greet')} depart={state.get('depart')}"
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
