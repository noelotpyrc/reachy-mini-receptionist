"""Video CLI tools for Reachy Mini.

Video recording uses the SDK's WebRTC camera pipeline with OpenCV VideoWriter.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click

from reachy_mini_brain import robot

# Default save location: <project_root>/artifacts/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ARTIFACTS_DIR = _PROJECT_ROOT / "artifacts"

# Resolution presets (same as vision.py)
RESOLUTIONS = {
    "720p": "R1280x720at30fps",
    "1080p": "R1920x1080at30fps",
}


@click.group()
def cli():
    pass


@cli.command()
@click.option("--duration", default=10.0, help="Recording duration in seconds")
@click.option("--out", default=None, help="Output video path (default: artifacts/reachy_video.mp4)")
@click.option(
    "--resolution", "res",
    default="720p",
    type=click.Choice(list(RESOLUTIONS.keys())),
    help="Capture resolution (default: 720p)",
)
@click.option("--fps", default=None, type=float, help="Target FPS (default: from resolution)")
def record(duration, out, res, fps):
    """Record video from robot camera."""
    import cv2
    from reachy_mini import ReachyMini
    from reachy_mini.media.camera_constants import CameraResolution

    if out is None:
        _ARTIFACTS_DIR.mkdir(exist_ok=True)
        out = str(_ARTIFACTS_DIR / "reachy_video.mp4")

    # Determine target FPS from resolution name
    if fps is None:
        fps = 30.0 if "30fps" in RESOLUTIONS[res] else 10.0

    robot.ensure_ready()

    print(f"  Recording {duration}s at {res} ({fps} fps)...", file=sys.stderr)

    with ReachyMini() as mini:
        # Set resolution before pipeline starts
        target_res = getattr(CameraResolution, RESOLUTIONS[res])
        try:
            mini.media.camera.set_resolution(target_res)
        except (RuntimeError, ValueError, AttributeError) as e:
            print(f"  Could not set resolution to {res}: {e}", file=sys.stderr)

        # Wait for first frame (WebRTC warmup)
        frame = None
        for i in range(30):
            frame = mini.media.get_frame()
            if frame is not None:
                break
            print(f"  Waiting for camera ({i + 1}/30)...", file=sys.stderr)
            time.sleep(1)

        if frame is None:
            click.echo("Error: no camera frame after warmup", err=True)
            raise SystemExit(1)

        h, w = frame.shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(out, fourcc, fps, (w, h))

        if not writer.isOpened():
            click.echo(f"Error: could not open video writer for {out}", err=True)
            raise SystemExit(1)

        frame_count = 0
        frame_interval = 1.0 / fps
        start = time.time()
        next_frame_time = start

        # Write the first frame we already have
        writer.write(frame)
        frame_count += 1
        next_frame_time += frame_interval

        while time.time() - start < duration:
            frame = mini.media.get_frame()
            if frame is not None:
                now = time.time()
                if now >= next_frame_time:
                    writer.write(frame)
                    frame_count += 1
                    next_frame_time += frame_interval
            else:
                time.sleep(0.01)

        writer.release()

    file_size = os.path.getsize(out)
    actual_duration = frame_count / fps
    print(
        f"  {w}x{h}, {frame_count} frames, {actual_duration:.1f}s, {file_size:,} bytes",
        file=sys.stderr,
    )
    click.echo(out)


if __name__ == "__main__":
    cli()
