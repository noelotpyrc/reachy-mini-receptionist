"""Vision CLI tools for Reachy Mini.

Camera frames come via the SDK's WebRTC pipeline (port 8443).
The REST API doesn't expose camera frames — this is the one place
we still use the reachy-mini SDK.
"""

import os
import sys
import time
from pathlib import Path

import click

from reachy_mini_brain import robot

# Default save location: <project_root>/artifacts/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ARTIFACTS_DIR = _PROJECT_ROOT / "artifacts"

# Friendly name → SDK CameraResolution enum member name
# Available on Reachy Mini Wireless: 720p@30, 1080p@30, 720p@60,
# 3840x2592@10, 4K@10, 3264x2448@10, 3072x1728@10
RESOLUTIONS = {
    "720p": "R1280x720at30fps",
    "1080p": "R1920x1080at30fps",
    "4k": "R3840x2160at10fps",
    "max": "R3840x2592at10fps",   # Near-full-sensor, highest res
}


@click.group()
def cli():
    pass


@cli.command()
@click.option("--out", default=None, help="Output image path (default: artifacts/reachy_photo.jpg)")
@click.option("--retries", default=5, help="Frame grab retries (WebRTC needs warmup)")
@click.option(
    "--resolution", "res",
    default="720p",
    type=click.Choice(list(RESOLUTIONS.keys())),
    help="Capture resolution (default: 720p). Higher = slower first frame.",
)
def take_photo(out, retries, res):
    """Capture a camera frame and save as JPEG."""
    import cv2
    from reachy_mini import ReachyMini
    from reachy_mini.media.camera_constants import CameraResolution

    if out is None:
        _ARTIFACTS_DIR.mkdir(exist_ok=True)
        out = str(_ARTIFACTS_DIR / "reachy_photo.jpg")

    # Make sure daemon is running before SDK connects
    robot.ensure_ready()

    with ReachyMini() as mini:
        # Set resolution before pipeline starts streaming
        target_res = getattr(CameraResolution, RESOLUTIONS[res])
        try:
            mini.media.camera.set_resolution(target_res)
        except (RuntimeError, ValueError, AttributeError) as e:
            print(f"  Could not set resolution to {res}: {e}", file=sys.stderr)
            print(f"  Falling back to default resolution", file=sys.stderr)

        # WebRTC pipeline may need a moment to start streaming
        frame = None
        for i in range(retries):
            frame = mini.media.get_frame()
            if frame is not None:
                break
            print(f"  Waiting for camera frame ({i + 1}/{retries})...", file=sys.stderr)
            time.sleep(1)

        if frame is None:
            click.echo("Error: no frame from camera after retries", err=True)
            raise SystemExit(1)

        cv2.imwrite(out, frame)
        h, w = frame.shape[:2]
        print(f"  {w}x{h}, {os.path.getsize(out):,} bytes", file=sys.stderr)
        click.echo(out)


if __name__ == "__main__":
    cli()
