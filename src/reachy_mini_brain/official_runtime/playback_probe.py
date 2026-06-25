"""One-shot official-runtime playback probe for OPS preflight."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

import click

from .policy_audio_cache import load_policy_audio_frame
from .robot_io import ReachyAudioSink, ReachyRobotSession


def play_wav_once(
    wav_path: str | Path,
    *,
    robot_host: str | None = None,
    audio_timeout_s: float = 60.0,
    post_roll_s: float = 0.5,
    session_factory: Callable[..., ReachyRobotSession] = ReachyRobotSession,
    sink_factory: Callable[[Any], ReachyAudioSink] = ReachyAudioSink,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Play one WAV through the same SDK audio sink used by live runtime."""

    path = Path(wav_path).expanduser()
    sample_rate, audio = load_policy_audio_frame(path)
    session = session_factory(
        host=robot_host,
        warmup_audio=True,
        warmup_video=False,
        audio_timeout_s=audio_timeout_s,
    )
    mini: Any | None = None
    try:
        _setup_gstreamer_environment()
        mini = session.start()
        sink = sink_factory(mini)
        asyncio.run(sink.write((sample_rate, audio)))
        asyncio.run(sink.drain())
        asyncio.run(sink.close())
        if post_roll_s > 0:
            sleep(post_roll_s)
    finally:
        session.stop()
    return {
        "path": str(path),
        "sample_rate": sample_rate,
        "samples": int(audio.shape[0]),
        "robot_host": robot_host,
        "mini_connected": mini is not None,
    }


def _setup_gstreamer_environment() -> None:
    """Apply the selected venv's GStreamer wheel environment, when present."""

    try:
        from gstreamer_libs import setup_python_environment
    except Exception:  # noqa: BLE001
        return
    try:
        setup_python_environment()
    except Exception:  # noqa: BLE001
        return


@click.command()
@click.argument("wav_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--robot-host", default=None, help="Robot host/IP. Uses SDK discovery if omitted.")
@click.option("--audio-timeout-s", type=float, default=60.0, show_default=True)
@click.option("--post-roll-s", type=float, default=0.5, show_default=True)
def cli(wav_path: Path, robot_host: str | None, audio_timeout_s: float, post_roll_s: float) -> None:
    """Play one WAV through official-runtime robot IO."""

    result = play_wav_once(
        wav_path,
        robot_host=robot_host,
        audio_timeout_s=audio_timeout_s,
        post_roll_s=post_roll_s,
    )
    click.echo(
        "official-runtime playback probe complete: "
        f"path={result['path']} sample_rate={result['sample_rate']} samples={result['samples']}"
    )


if __name__ == "__main__":
    cli()
