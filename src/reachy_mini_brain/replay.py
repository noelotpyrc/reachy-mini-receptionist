"""Legacy stage-1 harness — replay video through the old perception pipeline.

Status: legacy/fallback. The accepted product path uses official-runtime
artifacts and replay tools. Keep this module runnable for regression/reference
until legacy removal is explicitly approved.

Pumps a video file's frames through PerceptionPipeline exactly as the live vision
worker would, and reports the events (approach/depart) + an optional per-frame
trajectory. No robot, no daemon, no WebRTC — reproducible, fast, runnable in the
dev venv / CI. This is where approach & depart logic gets tuned and regression-tested
against labelled scenario clips, *before* spending a live robot session.

    reception-replay clip.mp4                       # report events
    reception-replay clip.mp4 --trace               # + per-frame track stats
    reception-replay approach.mp4 --expect-approach 1 --expect-depart 0   # assert (CI)
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import click


def _annotate_frame(frame, idx, dbg, state, events):
    """Render detection boxes + the visit state machine + event flashes onto a frame
    copy — this is the 'combine log + video' view (state is deterministic from the clip)."""
    import cv2

    img = frame.copy()
    for d in dbg:
        x1, y1, x2, y2 = d.get("box", (0, 0, 0, 0))
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(img, f"id{d['id']} a={d['area']:.2f}", (x1, max(12, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1, cv2.LINE_AA)
    hud = (f"f{idx}  dom={state['dom_area']:.3f}  absent={state['absent']}  "
           f"peak={state['peak']:.3f}  greet={state['greet']}  depart={state['depart']}")
    cv2.putText(img, hud, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, hud, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
    for j, ev in enumerate(events):
        color = (0, 165, 255) if ev["kind"] == "approach" else (255, 80, 80)
        cv2.putText(img, ev["kind"].upper(), (8, 74 + j * 48),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, color, 4, cv2.LINE_AA)
    return img


@click.command()
@click.argument("video", type=click.Path(exists=True))
@click.option("--threshold", default=0.5, help="Detector confidence threshold.")
@click.option("--every", default=1, help="Process every Nth frame (subsample).")
@click.option("--trace/--no-trace", default=False, help="Print per-frame per-track stats.")
@click.option("--reverse", is_flag=True, help="Process frames in reverse — turns an approach clip into a depart test.")
@click.option("--from-frame", type=int, default=0, help="Skip frames before this index (e.g. feed ONLY the walk-away, no walk-up).")
@click.option("--smooth", type=int, default=0, help="DetectionsSmoother window (0=off) — A/B the box-area jitter smoothing.")
@click.option("--annotate", type=click.Path(), default=None,
              help="Write a debug mp4: detection boxes + visit-state overlay + event flashes (combine log+video).")
@click.option("--expect-approach", type=int, default=None, help="Assert this many approach events.")
@click.option("--expect-depart", type=int, default=None, help="Assert this many depart events.")
def main(video, threshold, every, trace, reverse, from_frame, smooth, annotate, expect_approach, expect_depart):
    """Replay VIDEO through the perception pipeline and report/assert events."""
    import cv2

    from reachy_mini_brain.perception import PerceptionPipeline

    # isolated events log so we never touch the daemon's real artifacts/events.jsonl
    tmp = Path(tempfile.gettempdir()) / "reachy_replay_events.jsonl"
    pipe = PerceptionPipeline(events_path=tmp, threshold=threshold, smooth=smooth)

    cap = cv2.VideoCapture(video)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    if reverse:
        frames.reverse()
    if from_frame:
        frames = frames[from_frame:]

    writer = None
    if annotate and frames:
        h0, w0 = frames[0].shape[:2]
        out_fps = max(1.0, (src_fps or 5.0) / max(1, every))
        writer = cv2.VideoWriter(annotate, cv2.VideoWriter_fourcc(*"mp4v"), out_fps, (w0, h0))

    counts = {"approach": 0, "depart": 0}
    processed = 0
    for i, frame in enumerate(frames):
        if i % every != 0:
            continue
        processed += 1
        events, n, dbg = pipe.process(frame, bgr=True)
        for ev in events:
            counts[ev["kind"]] = counts.get(ev["kind"], 0) + 1
            click.echo(f"  frame {i:4d}: {ev['kind'].upper()}  {ev}")
        if trace:
            for d in dbg:
                click.echo(f"    f{i:4d} id={d['id']} area={d['area']:.3f}")
        if writer is not None:
            writer.write(_annotate_frame(frame, i, dbg, pipe._approach.debug_state, events))

    if writer is not None:
        writer.release()
        click.echo(f"   annotated debug video -> {annotate}")

    click.echo(f"=> {processed} frames processed | smooth={smooth} | approach={counts['approach']} depart={counts['depart']}")

    expects = [("approach", expect_approach), ("depart", expect_depart)]
    asserted = [e for e in expects if e[1] is not None]
    if asserted:
        ok = all(counts[k] == v for k, v in asserted)
        for k, v in asserted:
            flag = "ok" if counts[k] == v else "FAIL"
            click.echo(f"  [{flag}] {k}: got {counts[k]}, expected {v}")
        click.echo("PASS" if ok else "FAIL")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
