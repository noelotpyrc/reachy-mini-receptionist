"""Replay WAV files through the LiveKit backend handler."""

from __future__ import annotations

import json
import shutil
import asyncio
from pathlib import Path
from datetime import datetime

import click

from reachy_mini_brain.audio_pacing import WEBRTC_AUDIO_FRAME_MS

from .events import CompositeEventSink, InMemoryEventSink, JsonlEventSink
from .env import PROJECT_ROOT, load_project_env
from .wav_replay import run_wav_replay
from .livekit_handler import LiveKitBackendConfig, LiveKitRealtimeHandler
from .livekit_room_bridge import LiveKitRoomBridge


load_project_env()
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "livekit-replays"


@click.command()
@click.argument("input_wav", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--run-id", default=None, help="Replay run id. Defaults to timestamped id.")
@click.option("--artifact-root", default=DEFAULT_ARTIFACT_ROOT, type=click.Path(path_type=Path))
@click.option("--url", envvar="LIVEKIT_URL", default="", help="LiveKit server URL.")
@click.option("--api-key", envvar="LIVEKIT_API_KEY", default="", help="LiveKit API key.")
@click.option("--api-secret", envvar="LIVEKIT_API_SECRET", default="", help="LiveKit API secret.")
@click.option("--token", envvar="LIVEKIT_TOKEN", default="", help="Pre-generated LiveKit room token.")
@click.option("--room-name", envvar="LIVEKIT_ROOM", default="reachy-mini-offline")
@click.option("--participant-name", default="reachy-mini-replay")
@click.option("--agent-name", envvar="LIVEKIT_AGENT_NAME", default="reachy-mini-receptionist")
@click.option("--dispatch-agent/--no-dispatch-agent", default=True, show_default=True)
@click.option("--input-sample-rate", default=16_000, show_default=True)
@click.option("--output-sample-rate", default=24_000, show_default=True)
@click.option("--frame-duration-ms", default=WEBRTC_AUDIO_FRAME_MS, show_default=True)
@click.option("--emit-timeout", default=0.1, show_default=True)
@click.option("--drain-idle-polls", default=200, show_default=True, help="Output drain polls after input ends.")
@click.option("--real-time/--no-real-time", default=True, show_default=True, help="Replay WAV at realtime speed.")
@click.option("--suppress-silent-output/--keep-silent-output", default=True, show_default=True)
@click.option("--silent-output-peak-threshold", default=4, show_default=True)
def cli(
    input_wav: Path,
    run_id: str | None,
    artifact_root: Path,
    url: str,
    api_key: str,
    api_secret: str,
    token: str,
    room_name: str,
    participant_name: str,
    agent_name: str,
    dispatch_agent: bool,
    input_sample_rate: int,
    output_sample_rate: int,
    frame_duration_ms: int,
    emit_timeout: float,
    drain_idle_polls: int,
    real_time: bool,
    suppress_silent_output: bool,
    silent_output_peak_threshold: int,
) -> None:
    """Replay INPUT_WAV through LiveKitRealtimeHandler and save artifacts."""

    run_id = run_id or f"livekit-replay-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_dir = artifact_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    copied_input = run_dir / "input.wav"
    output_wav = run_dir / "output.wav"
    events_jsonl = run_dir / "events.jsonl"
    transcript_jsonl = run_dir / "transcript.jsonl"
    manifest_path = run_dir / "manifest.json"
    shutil.copy2(input_wav, copied_input)
    transcript_jsonl.write_text("", encoding="utf-8")

    config = LiveKitBackendConfig(
        url=url,
        api_key=api_key,
        api_secret=api_secret,
        token=token,
        room_name=room_name,
        participant_name=participant_name,
        agent_name=agent_name,
        dispatch_agent=dispatch_agent,
        input_sample_rate=input_sample_rate,
        output_sample_rate=output_sample_rate,
        suppress_silent_output=suppress_silent_output,
        silent_output_peak_threshold=silent_output_peak_threshold,
    )
    memory_events = InMemoryEventSink()
    event_sink = CompositeEventSink(memory_events, JsonlEventSink(events_jsonl))
    bridge = LiveKitRoomBridge(config, event_sink=event_sink)
    handler = LiveKitRealtimeHandler(config=config, bridge=bridge, event_sink=event_sink)
    manifest = {
        "run_id": run_id,
        "status": "started",
        "input_wav": str(copied_input),
        "source_input_wav": str(input_wav),
        "output_wav": str(output_wav),
        "events_jsonl": str(events_jsonl),
        "transcript_jsonl": str(transcript_jsonl),
        "config": {
            "url_set": bool(url),
            "room_name": room_name,
            "participant_name": participant_name,
            "agent_name": agent_name,
            "dispatch_agent": dispatch_agent,
            "input_sample_rate": input_sample_rate,
            "output_sample_rate": output_sample_rate,
            "frame_duration_ms": frame_duration_ms,
            "real_time": real_time,
            "suppress_silent_output": suppress_silent_output,
            "silent_output_peak_threshold": silent_output_peak_threshold,
            "token_set": bool(token),
            "api_key_set": bool(api_key),
            "api_secret_set": bool(api_secret),
        },
        "started_at": datetime.now().isoformat(),
    }
    _write_json(manifest_path, manifest)

    try:
        asyncio.run(
            run_wav_replay(
                input_wav=copied_input,
                output_wav=output_wav,
                handler=handler,
                event_sink=event_sink,
                frame_duration_ms=frame_duration_ms,
                real_time=real_time,
                runtime_options={
                    "emit_timeout": emit_timeout,
                    "drain_idle_polls": drain_idle_polls,
                },
            )
        )
    except Exception as exc:  # noqa: BLE001
        _write_transcript_jsonl(transcript_jsonl, memory_events)
        manifest["status"] = "failed"
        manifest["error"] = repr(exc)
        manifest["completed_at"] = datetime.now().isoformat()
        manifest["event_counts"] = _event_counts(memory_events.kinds())
        _write_json(manifest_path, manifest)
        raise click.ClickException(str(exc)) from exc

    _write_transcript_jsonl(transcript_jsonl, memory_events)
    manifest["status"] = "completed"
    manifest["completed_at"] = datetime.now().isoformat()
    manifest["event_counts"] = _event_counts(memory_events.kinds())
    _write_json(manifest_path, manifest)
    click.echo(f"LiveKit replay artifacts: {run_dir}")


def _event_counts(kinds: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_transcript_jsonl(path: Path, events: InMemoryEventSink) -> None:
    rows = []
    for event in events.events:
        if event.kind == "livekit.room.transcription":
            rows.append({"ts": event.ts, **event.data})
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


if __name__ == "__main__":
    cli()
