"""Replay identical WAV inputs through realtime backends and compare timing."""

from __future__ import annotations

import csv
import json
import shutil
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from reachy_mini_brain.audio_pacing import WEBRTC_AUDIO_FRAME_MS

from .env import PROJECT_ROOT, load_project_env
from .events import CompositeEventSink, InMemoryEventSink, JsonlEventSink, RuntimeEvent
from .wav_replay import run_wav_replay
from .livekit_handler import LiveKitBackendConfig, LiveKitRealtimeHandler
from .livekit_room_bridge import LiveKitRoomBridge
from .s2s_realtime import S2SRealtimeHandler


load_project_env()

DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "backend-benchmarks"
DEFAULT_INSTRUCTIONS = (
    "You are a concise clinic receptionist. Listen to the user's speech and answer naturally in one short sentence."
)


@dataclass
class BenchmarkSummary:
    batch_id: str
    backend: str
    input_wav: str
    run_dir: str
    status: str
    output_wav: str
    events_jsonl: str
    input_frames: int | None = None
    input_samples: int | None = None
    input_sample_rate: int | None = None
    input_duration_s: float | None = None
    first_input_ts: float | None = None
    input_done_ts: float | None = None
    first_output_audio_ts: float | None = None
    runtime_started_ts: float | None = None
    runtime_stopped_ts: float | None = None
    input_start_to_first_output_audio_s: float | None = None
    input_done_to_first_output_audio_s: float | None = None
    runtime_total_s: float | None = None
    output_audio_frames: int = 0
    output_audio_samples: int = 0
    error: str | None = None


@click.command()
@click.argument("input_wavs", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--backend", "backends", multiple=True, type=click.Choice(["s2s-local", "livekit"]))
@click.option("--batch-id", default=None, help="Benchmark batch id. Defaults to timestamped id.")
@click.option("--artifact-root", default=DEFAULT_ARTIFACT_ROOT, type=click.Path(path_type=Path))
@click.option("--frame-duration-ms", default=WEBRTC_AUDIO_FRAME_MS, show_default=True)
@click.option("--emit-timeout", default=0.1, show_default=True)
@click.option("--drain-idle-polls", default=200, show_default=True)
@click.option("--real-time/--no-real-time", default=True, show_default=True)
@click.option("--instructions", default=DEFAULT_INSTRUCTIONS, show_default=True)
@click.option("--s2s-voice", default="Sohee", show_default=True)
@click.option("--s2s-realtime-ws-url", envvar="HF_REALTIME_WS_URL", default="ws://127.0.0.1:8765/v1/realtime")
@click.option("--livekit-url", envvar="LIVEKIT_URL", default="")
@click.option("--livekit-api-key", envvar="LIVEKIT_API_KEY", default="")
@click.option("--livekit-api-secret", envvar="LIVEKIT_API_SECRET", default="")
@click.option("--livekit-token", envvar="LIVEKIT_TOKEN", default="")
@click.option("--livekit-room-name", envvar="LIVEKIT_ROOM", default="reachy-mini-offline")
@click.option("--livekit-participant-name", default="reachy-mini-replay")
@click.option("--livekit-agent-name", envvar="LIVEKIT_AGENT_NAME", default="reachy-mini-receptionist")
@click.option("--livekit-dispatch-agent/--no-livekit-dispatch-agent", default=True, show_default=True)
@click.option("--livekit-input-sample-rate", default=16_000, show_default=True)
@click.option("--livekit-output-sample-rate", default=24_000, show_default=True)
@click.option("--suppress-silent-output/--keep-silent-output", default=True, show_default=True)
@click.option("--silent-output-peak-threshold", default=4, show_default=True)
@click.option("--fail-fast/--continue-on-error", default=False, show_default=True)
def cli(
    input_wavs: tuple[Path, ...],
    backends: tuple[str, ...],
    batch_id: str | None,
    artifact_root: Path,
    frame_duration_ms: int,
    emit_timeout: float,
    drain_idle_polls: int,
    real_time: bool,
    instructions: str,
    s2s_voice: str,
    s2s_realtime_ws_url: str,
    livekit_url: str,
    livekit_api_key: str,
    livekit_api_secret: str,
    livekit_token: str,
    livekit_room_name: str,
    livekit_participant_name: str,
    livekit_agent_name: str,
    livekit_dispatch_agent: bool,
    livekit_input_sample_rate: int,
    livekit_output_sample_rate: int,
    suppress_silent_output: bool,
    silent_output_peak_threshold: int,
    fail_fast: bool,
) -> None:
    """Replay INPUT_WAVS through selected backends and write benchmark artifacts."""

    if not input_wavs:
        raise click.UsageError("Provide at least one WAV file.")
    selected_backends = list(backends or ("s2s-local", "livekit"))
    batch_id = batch_id or f"backend-benchmark-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    batch_dir = artifact_root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[BenchmarkSummary] = []
    for input_index, input_wav in enumerate(input_wavs, start=1):
        for backend in selected_backends:
            run_name = f"{input_index:03d}-{_safe_stem(input_wav)}-{backend}"
            run_dir = batch_dir / run_name
            try:
                summary = asyncio.run(
                    _run_one_backend(
                        backend=backend,
                        batch_id=batch_id,
                        input_wav=input_wav,
                        run_dir=run_dir,
                        frame_duration_ms=frame_duration_ms,
                        emit_timeout=emit_timeout,
                        drain_idle_polls=drain_idle_polls,
                        real_time=real_time,
                        instructions=instructions,
                        s2s_voice=s2s_voice,
                        s2s_realtime_ws_url=s2s_realtime_ws_url,
                        livekit_url=livekit_url,
                        livekit_api_key=livekit_api_key,
                        livekit_api_secret=livekit_api_secret,
                        livekit_token=livekit_token,
                        livekit_room_name=livekit_room_name,
                        livekit_participant_name=livekit_participant_name,
                        livekit_agent_name=livekit_agent_name,
                        livekit_dispatch_agent=livekit_dispatch_agent,
                        livekit_input_sample_rate=livekit_input_sample_rate,
                        livekit_output_sample_rate=livekit_output_sample_rate,
                        suppress_silent_output=suppress_silent_output,
                        silent_output_peak_threshold=silent_output_peak_threshold,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                summary = BenchmarkSummary(
                    batch_id=batch_id,
                    backend=backend,
                    input_wav=str(input_wav),
                    run_dir=str(run_dir),
                    status="failed",
                    output_wav=str(run_dir / "output.wav"),
                    events_jsonl=str(run_dir / "events.jsonl"),
                    error=repr(exc),
                )
                run_dir.mkdir(parents=True, exist_ok=True)
                _write_json(run_dir / "manifest.json", asdict(summary))
                if fail_fast:
                    raise click.ClickException(f"{backend} failed for {input_wav}: {exc}") from exc
            summaries.append(summary)
            click.echo(_format_summary_line(summary))

    _write_batch_outputs(batch_dir, summaries)
    click.echo(f"benchmark artifacts: {batch_dir}")


async def _run_one_backend(
    *,
    backend: str,
    batch_id: str,
    input_wav: Path,
    run_dir: Path,
    frame_duration_ms: int,
    emit_timeout: float,
    drain_idle_polls: int,
    real_time: bool,
    instructions: str,
    s2s_voice: str,
    s2s_realtime_ws_url: str,
    livekit_url: str,
    livekit_api_key: str,
    livekit_api_secret: str,
    livekit_token: str,
    livekit_room_name: str,
    livekit_participant_name: str,
    livekit_agent_name: str,
    livekit_dispatch_agent: bool,
    livekit_input_sample_rate: int,
    livekit_output_sample_rate: int,
    suppress_silent_output: bool,
    silent_output_peak_threshold: int,
) -> BenchmarkSummary:
    run_dir.mkdir(parents=True, exist_ok=True)
    copied_input = run_dir / "input.wav"
    output_wav = run_dir / "output.wav"
    events_jsonl = run_dir / "events.jsonl"
    manifest_path = run_dir / "manifest.json"
    shutil.copy2(input_wav, copied_input)
    events_jsonl.write_text("", encoding="utf-8")
    memory_events = InMemoryEventSink()
    event_sink = CompositeEventSink(memory_events, JsonlEventSink(events_jsonl))

    if backend == "livekit":
        handler = _build_livekit_handler(
            event_sink=event_sink,
            url=livekit_url,
            api_key=livekit_api_key,
            api_secret=livekit_api_secret,
            token=livekit_token,
            room_name=livekit_room_name,
            participant_name=livekit_participant_name,
            agent_name=livekit_agent_name,
            dispatch_agent=livekit_dispatch_agent,
            input_sample_rate=livekit_input_sample_rate,
            output_sample_rate=livekit_output_sample_rate,
            suppress_silent_output=suppress_silent_output,
            silent_output_peak_threshold=silent_output_peak_threshold,
        )
    elif backend == "s2s-local":
        handler = S2SRealtimeHandler(
            event_sink=event_sink,
            realtime_ws_url=s2s_realtime_ws_url,
            instructions=instructions,
            voice=s2s_voice,
        )
    else:
        raise ValueError(f"unsupported backend: {backend}")

    started_summary = BenchmarkSummary(
        batch_id=batch_id,
        backend=backend,
        input_wav=str(copied_input),
        run_dir=str(run_dir),
        status="started",
        output_wav=str(output_wav),
        events_jsonl=str(events_jsonl),
    )
    _write_json(manifest_path, asdict(started_summary))

    try:
        await run_wav_replay(
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
    except Exception as exc:
        failed = _summarize_run(
            batch_id=batch_id,
            backend=backend,
            input_wav=copied_input,
            run_dir=run_dir,
            output_wav=output_wav,
            events_jsonl=events_jsonl,
            events=memory_events.events,
            status="failed",
            error=repr(exc),
        )
        _write_json(manifest_path, asdict(failed))
        raise

    summary = _summarize_run(
        batch_id=batch_id,
        backend=backend,
        input_wav=copied_input,
        run_dir=run_dir,
        output_wav=output_wav,
        events_jsonl=events_jsonl,
        events=memory_events.events,
        status="completed",
    )
    _write_json(manifest_path, asdict(summary))
    return summary


def _build_livekit_handler(
    *,
    event_sink: CompositeEventSink,
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
    suppress_silent_output: bool,
    silent_output_peak_threshold: int,
) -> LiveKitRealtimeHandler:
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
    bridge = LiveKitRoomBridge(config, event_sink=event_sink)
    return LiveKitRealtimeHandler(config=config, bridge=bridge, event_sink=event_sink)


def _summarize_run(
    *,
    batch_id: str,
    backend: str,
    input_wav: Path,
    run_dir: Path,
    output_wav: Path,
    events_jsonl: Path,
    events: list[RuntimeEvent],
    status: str,
    error: str | None = None,
) -> BenchmarkSummary:
    first_input = _first_event(events, "audio.input_frame")
    input_done = _first_event(events, "audio.input_done")
    first_output = _first_event(events, "audio.output_frame")
    runtime_started = _first_event(events, "runtime.started")
    runtime_stopped = _first_event(events, "runtime.stopped")
    output_events = [event for event in events if event.kind == "audio.output_frame"]
    output_samples = sum(int(event.data.get("samples") or 0) for event in output_events)

    summary = BenchmarkSummary(
        batch_id=batch_id,
        backend=backend,
        input_wav=str(input_wav),
        run_dir=str(run_dir),
        status=status,
        output_wav=str(output_wav),
        events_jsonl=str(events_jsonl),
        input_frames=_int_or_none(input_done.data.get("frames")) if input_done else None,
        input_samples=_int_or_none(input_done.data.get("samples")) if input_done else None,
        input_sample_rate=_int_or_none(input_done.data.get("sample_rate")) if input_done else None,
        input_duration_s=_round_or_none(input_done.data.get("duration_s")) if input_done else None,
        first_input_ts=_round_or_none(first_input.ts) if first_input else None,
        input_done_ts=_round_or_none(input_done.ts) if input_done else None,
        first_output_audio_ts=_round_or_none(first_output.ts) if first_output else None,
        runtime_started_ts=_round_or_none(runtime_started.ts) if runtime_started else None,
        runtime_stopped_ts=_round_or_none(runtime_stopped.ts) if runtime_stopped else None,
        input_start_to_first_output_audio_s=_delta(first_input, first_output),
        input_done_to_first_output_audio_s=_delta(input_done, first_output),
        runtime_total_s=_delta(runtime_started, runtime_stopped),
        output_audio_frames=len(output_events),
        output_audio_samples=output_samples,
        error=error,
    )
    if status == "completed" and first_output is None:
        summary.status = "completed_no_output"
    return summary


def _first_event(events: list[RuntimeEvent], kind: str) -> RuntimeEvent | None:
    return next((event for event in events if event.kind == kind), None)


def _delta(start: RuntimeEvent | None, end: RuntimeEvent | None) -> float | None:
    if start is None or end is None:
        return None
    return round(end.ts - start.ts, 3)


def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 3)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _write_batch_outputs(batch_dir: Path, summaries: list[BenchmarkSummary]) -> None:
    summary_path = batch_dir / "summary.json"
    existing_rows: list[dict[str, Any]] = []
    if summary_path.exists():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing_rows = [row for row in loaded if isinstance(row, dict)]
        except json.JSONDecodeError:
            existing_rows = []
    rows_by_run_dir = {str(row.get("run_dir")): row for row in existing_rows}
    for summary in summaries:
        row = asdict(summary)
        rows_by_run_dir[str(row.get("run_dir"))] = row
    rows = list(rows_by_run_dir.values())
    _write_json(summary_path, rows)
    csv_path = batch_dir / "summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else list(BenchmarkSummary.__annotations__))
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=repr) + "\n", encoding="utf-8")


def _format_summary_line(summary: BenchmarkSummary) -> str:
    latency = summary.input_done_to_first_output_audio_s
    latency_text = "NA" if latency is None else f"{latency:.3f}s"
    return f"{summary.backend} {Path(summary.input_wav).name}: {summary.status}, input_done_to_first_audio={latency_text}"


def _safe_stem(path: Path) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in path.stem)
    return safe[:80] or "input"


if __name__ == "__main__":
    cli()
