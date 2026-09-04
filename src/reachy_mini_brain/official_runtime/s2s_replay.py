"""Replay reviewed WAV turns through the local S2S realtime backend."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import wave
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import numpy as np

from .agent_profile import compose_agent_profile
from .env import PROJECT_ROOT
from .events import JsonlEventSink, RuntimeEvent
from .realtime_tools import ToolExecutionContext, build_reference_tool_registry
from .s2s_realtime import ConnectFactory, S2SRealtimeHandler


DEFAULT_MANIFEST = (
    PROJECT_ROOT
    / "artifacts"
    / "hermes-s2s-e2e"
    / "official-live-20260625-133754"
    / "stable-v2"
    / "manifest.json"
)
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "hermes-s2s-e2e-runs"
DEFAULT_WS_URL = "ws://127.0.0.1:8765/v1/realtime"
DEFAULT_PROFILE_DIR = PROJECT_ROOT / "profiles" / "clinic_receptionist"
DEFAULT_PROFILE_ID = "lakeside-test"
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class ReplayTurnResult:
    index: int
    input_wav: str
    expected_transcript: str
    observed_transcript: str
    assistant_text: str
    response_id: str
    output_wav: str
    input_samples: int
    output_samples: int
    input_duration_s: float
    input_started_ts: float
    input_done_ts: float
    transcript_done_ts: float
    response_created_ts: float | None
    first_audio_ts: float
    audio_done_ts: float
    response_done_ts: float
    input_done_to_transcript_s: float
    transcript_to_first_audio_s: float | None
    transcript_to_response_done_s: float


@dataclass(frozen=True)
class _CompletedAudioResponse:
    response_id: str
    transcript_event: RuntimeEvent
    first_audio_event: RuntimeEvent
    audio_text_event: RuntimeEvent
    audio_done_event: RuntimeEvent
    response_done_event: RuntimeEvent


class ReplayEventSink:
    """Record events and wake replay waiters without polling the filesystem."""

    def __init__(self, jsonl_path: Path) -> None:
        self.events: list[RuntimeEvent] = []
        self._changed = asyncio.Event()
        self._jsonl = JsonlEventSink(jsonl_path)

    def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)
        self._jsonl.emit(event)
        self._changed.set()

    async def wait_for(
        self,
        predicate: Callable[[RuntimeEvent], bool],
        *,
        start_index: int,
        timeout_s: float,
        label: str,
    ) -> tuple[int, RuntimeEvent]:
        async with asyncio.timeout(timeout_s):
            while True:
                self._changed.clear()
                for index in range(start_index, len(self.events)):
                    event = self.events[index]
                    if event.kind == "hf.realtime.error":
                        raise RuntimeError(
                            f"S2S realtime error while waiting for {label}: {event.data}"
                        )
                    if predicate(event):
                        return index, event
                await self._changed.wait()

    def raise_for_errors(self, *, start_index: int) -> None:
        for event in self.events[start_index:]:
            if event.kind == "hf.realtime.error":
                raise RuntimeError(f"S2S realtime error: {event.data}")


class OutputCollector:
    def __init__(self, handler: S2SRealtimeHandler) -> None:
        self.handler = handler
        self.audio_by_response: dict[str, list[np.ndarray[Any, Any]]] = {}

    async def run(self) -> None:
        while True:
            output = await self.handler.emit()
            if not isinstance(output, tuple):
                continue
            _sample_rate, audio, metadata = output
            response_id = str(metadata.get("response_id") or "")
            if response_id:
                self.audio_by_response.setdefault(response_id, []).append(
                    np.asarray(audio, dtype="<i2").copy()
                )

    def audio(self, response_id: str) -> np.ndarray[Any, Any]:
        chunks = self.audio_by_response.get(response_id, [])
        if not chunks:
            return np.empty(0, dtype="<i2")
        return np.concatenate(chunks).astype("<i2", copy=False)


async def run_s2s_replay(
    *,
    manifest_path: Path,
    turn_indexes: Sequence[int],
    output_dir: Path,
    ws_url: str = DEFAULT_WS_URL,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    profile_id: str = DEFAULT_PROFILE_ID,
    frame_duration_ms: int = 20,
    turn_timeout_s: float = 90.0,
    real_time: bool = True,
    connect_factory: ConnectFactory | None = None,
    sleep: Sleep = asyncio.sleep,
) -> list[ReplayTurnResult]:
    """Run selected turns in one WebSocket conversation and write evidence."""

    manifest_path = manifest_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(
            f"refusing to overwrite existing replay run: {output_dir}"
        )
    if frame_duration_ms <= 0:
        raise ValueError("frame_duration_ms must be positive")
    selected = _load_selected_turns(manifest_path, turn_indexes)
    profile = compose_agent_profile(
        profile_id=profile_id,
        public_dir=profile_dir,
    )

    output_dir.mkdir(parents=True, exist_ok=False)
    events_path = output_dir / "events.jsonl"
    events_path.write_text("", encoding="utf-8")
    sink = ReplayEventSink(events_path)
    tool_registry = build_reference_tool_registry(profile.reference_store)
    tool_context = ToolExecutionContext(
        profile_id=profile.profile_id,
        visitor_session_id="pre-session",
        reference_store=profile.reference_store,
        event_sink=sink,
    )
    handler = S2SRealtimeHandler(
        realtime_ws_url=ws_url,
        instructions=profile.instructions,
        instructions_source=f"profile:{profile.profile_id}",
        instructions_sha256=profile.sha256,
        event_sink=sink,
        startup_timeout_s=min(turn_timeout_s, 20.0),
        connect_factory=connect_factory,
        tool_registry=tool_registry,
        tool_context=tool_context,
    )
    collector = OutputCollector(handler)
    collector_task: asyncio.Task[None] | None = None
    results: list[ReplayTurnResult] = []
    run_started_ts = time.time()
    conversation_session: dict[str, Any] | None = None

    try:
        await handler.start_up()
        conversation_session = await handler.begin_conversation_session()
        collector_task = asyncio.create_task(collector.run(), name="s2s-replay-output")
        for turn in selected:
            result = await _run_turn(
                handler=handler,
                collector=collector,
                sink=sink,
                manifest_dir=manifest_path.parent,
                output_dir=output_dir,
                turn=turn,
                frame_duration_ms=frame_duration_ms,
                turn_timeout_s=turn_timeout_s,
                real_time=real_time,
                sleep=sleep,
            )
            results.append(result)
    finally:
        if collector_task is not None:
            collector_task.cancel()
            try:
                await collector_task
            except asyncio.CancelledError:
                pass
        await handler.shutdown()

    report = {
        "schema_version": 2,
        "status": "completed",
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "ws_url": ws_url,
        "turn_indexes": [result.index for result in results],
        "connection_count": 1,
        "conversation_session": conversation_session,
        "profile": profile.provenance(),
        "registered_tools": tool_registry.names(),
        "started_ts": run_started_ts,
        "done_ts": time.time(),
        "events_jsonl": events_path.name,
        "turns": [asdict(result) for result in results],
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return results


async def _run_turn(
    *,
    handler: S2SRealtimeHandler,
    collector: OutputCollector,
    sink: ReplayEventSink,
    manifest_dir: Path,
    output_dir: Path,
    turn: dict[str, Any],
    frame_duration_ms: int,
    turn_timeout_s: float,
    real_time: bool,
    sleep: Sleep,
) -> ReplayTurnResult:
    turn_index = int(turn["index"])
    input_wav = (manifest_dir / str(turn["path"])).resolve()
    start_index = len(sink.events)
    input_started_ts = time.time()
    input_samples, sample_rate = await _stream_wav(
        handler,
        input_wav,
        frame_duration_ms=frame_duration_ms,
        real_time=real_time,
        sleep=sleep,
        sink=sink,
        event_start_index=start_index,
    )
    input_done_ts = time.time()

    _transcript_index, _transcript_event = await sink.wait_for(
        lambda event: (
            event.kind
            == "hf.realtime.conversation.item.input_audio_transcription.completed"
        ),
        start_index=start_index,
        timeout_s=turn_timeout_s,
        label=f"turn {turn_index} input transcript",
    )
    completed = await _wait_for_completed_audio_response(
        sink=sink,
        collector=collector,
        start_index=start_index,
        turn_index=turn_index,
        timeout_s=turn_timeout_s,
    )
    sink.raise_for_errors(start_index=start_index)
    response_id = completed.response_id
    audio = collector.audio(response_id)
    if audio.size == 0:
        raise RuntimeError(
            f"turn {turn_index} completed response {response_id!r} produced no decodable audio"
        )
    output_wav = output_dir / f"turn-{turn_index:02d}-assistant.wav"
    _write_pcm16_mono(output_wav, audio, 16_000)
    response_created_event = next(
        (
            event
            for event in sink.events[start_index:]
            if event.kind == "hf.realtime.response.created"
            and _event_response_id(event) == response_id
        ),
        None,
    )
    transcript_event = completed.transcript_event
    first_audio_event = completed.first_audio_event
    audio_text_event = completed.audio_text_event
    audio_done_event = completed.audio_done_event
    response_done_event = completed.response_done_event
    transcript_ts = transcript_event.ts
    return ReplayTurnResult(
        index=turn_index,
        input_wav=str(input_wav),
        expected_transcript=str(turn["expected_transcript"]),
        observed_transcript=str(transcript_event.data.get("transcript") or ""),
        assistant_text=str(audio_text_event.data.get("transcript") or ""),
        response_id=response_id,
        output_wav=str(output_wav),
        input_samples=input_samples,
        output_samples=int(audio.shape[0]),
        input_duration_s=input_samples / float(sample_rate),
        input_started_ts=input_started_ts,
        input_done_ts=input_done_ts,
        transcript_done_ts=transcript_ts,
        response_created_ts=response_created_event.ts
        if response_created_event is not None
        else None,
        first_audio_ts=first_audio_event.ts,
        audio_done_ts=audio_done_event.ts,
        response_done_ts=response_done_event.ts,
        input_done_to_transcript_s=transcript_ts - input_done_ts,
        transcript_to_first_audio_s=first_audio_event.ts - transcript_ts,
        transcript_to_response_done_s=response_done_event.ts - transcript_ts,
    )


async def _wait_for_completed_audio_response(
    *,
    sink: ReplayEventSink,
    collector: OutputCollector,
    start_index: int,
    turn_index: int,
    timeout_s: float,
) -> _CompletedAudioResponse:
    deadline = asyncio.get_running_loop().time() + timeout_s
    cursor = start_index
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(
                f"turn {turn_index} did not produce a completed audio response"
            )
        response_done_index, response_done_event = await sink.wait_for(
            lambda event: event.kind == "hf.realtime.response.done",
            start_index=cursor,
            timeout_s=remaining,
            label=f"turn {turn_index} completed audio response",
        )
        cursor = response_done_index + 1
        response_id = _event_response_id(response_done_event)
        if (
            not response_id
            or response_done_event.data.get("response_status") != "completed"
        ):
            continue

        response_events = [
            event
            for event in sink.events[start_index : response_done_index + 1]
            if _event_response_id(event) == response_id
        ]
        first_audio_event = next(
            (
                event
                for event in response_events
                if event.kind == "hf.realtime.response.output_audio.delta"
            ),
            None,
        )
        audio_text_event = next(
            (
                event
                for event in reversed(response_events)
                if event.kind == "hf.realtime.response.output_audio_transcript.done"
            ),
            None,
        )
        audio_done_event = next(
            (
                event
                for event in reversed(response_events)
                if event.kind == "hf.realtime.response.output_audio.done"
            ),
            None,
        )
        if (
            first_audio_event is None
            or audio_text_event is None
            or audio_done_event is None
        ):
            continue
        transcript_event = next(
            (
                event
                for event in reversed(
                    sink.events[start_index : response_done_index + 1]
                )
                if event.kind
                == "hf.realtime.conversation.item.input_audio_transcription.completed"
                and event.ts <= first_audio_event.ts
            ),
            None,
        )
        if transcript_event is None:
            continue

        for _attempt in range(10):
            if collector.audio(response_id).size:
                break
            await asyncio.sleep(0)
        if collector.audio(response_id).size == 0:
            raise RuntimeError(
                f"turn {turn_index} completed response {response_id!r} produced no decodable audio"
            )
        return _CompletedAudioResponse(
            response_id=response_id,
            transcript_event=transcript_event,
            first_audio_event=first_audio_event,
            audio_text_event=audio_text_event,
            audio_done_event=audio_done_event,
            response_done_event=response_done_event,
        )


async def _stream_wav(
    handler: S2SRealtimeHandler,
    path: Path,
    *,
    frame_duration_ms: int,
    real_time: bool,
    sleep: Sleep,
    sink: ReplayEventSink,
    event_start_index: int,
) -> tuple[int, int]:
    with wave.open(str(path), "rb") as wav:
        channels = int(wav.getnchannels())
        sample_width = int(wav.getsampwidth())
        sample_rate = int(wav.getframerate())
        compression = wav.getcomptype()
        if channels != 1 or sample_width != 2 or compression != "NONE":
            raise ValueError(f"{path} must be mono, uncompressed PCM16 WAV")
        frame_samples = max(1, int(round(sample_rate * frame_duration_ms / 1000.0)))
        total_samples = 0
        while True:
            raw = wav.readframes(frame_samples)
            if not raw:
                break
            audio = np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=True)
            await handler.receive((sample_rate, audio))
            total_samples += int(audio.shape[0])
            sink.raise_for_errors(start_index=event_start_index)
            if real_time:
                await sleep(audio.shape[0] / float(sample_rate))
            else:
                await asyncio.sleep(0)
    return total_samples, sample_rate


def _load_selected_turns(
    manifest_path: Path, turn_indexes: Sequence[int]
) -> list[dict[str, Any]]:
    if not turn_indexes:
        raise ValueError("at least one explicit turn index is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("fixture") != "hermes-s2s-stable-replay":
        raise ValueError(f"unsupported replay fixture: {manifest.get('fixture')!r}")
    turns = {int(turn["index"]): turn for turn in manifest.get("turns", [])}
    if len(set(turn_indexes)) != len(turn_indexes):
        raise ValueError("turn indexes must not contain duplicates")
    selected: list[dict[str, Any]] = []
    for index in turn_indexes:
        if index not in turns:
            raise ValueError(f"turn {index} is not present in {manifest_path}")
        turn = turns[index]
        wav_path = manifest_path.parent / str(turn["path"])
        actual_sha = _sha256_file(wav_path)
        if actual_sha != turn.get("sha256"):
            raise ValueError(f"turn {index} WAV checksum mismatch: {wav_path}")
        selected.append(turn)
    return selected


def _event_response_id(event: RuntimeEvent) -> str:
    return str(event.data.get("response_id") or "")


def _write_pcm16_mono(
    path: Path, audio: np.ndarray[Any, Any], sample_rate: int
) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(np.asarray(audio, dtype="<i2").reshape(-1).tobytes())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_turns(value: str) -> tuple[int, ...]:
    indexes: list[int] = []
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"invalid descending turn range: {token}")
            indexes.extend(range(start, end + 1))
        else:
            indexes.append(int(token))
    if not indexes:
        raise ValueError("provide at least one turn, for example --turns 2,3,8")
    return tuple(indexes)


@click.command()
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=DEFAULT_MANIFEST,
    show_default=True,
)
@click.option(
    "--turns",
    required=True,
    help="Explicit indexes or ranges, for example 2,3,8 or 1-12.",
)
@click.option("--output-dir", type=click.Path(path_type=Path), default=None)
@click.option("--ws-url", default=DEFAULT_WS_URL, show_default=True)
@click.option(
    "--profile-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_PROFILE_DIR,
    show_default=True,
)
@click.option("--profile-id", default=DEFAULT_PROFILE_ID, show_default=True)
@click.option("--frame-duration-ms", default=20, show_default=True)
@click.option("--turn-timeout-s", default=90.0, show_default=True)
def cli(
    manifest_path: Path,
    turns: str,
    output_dir: Path | None,
    ws_url: str,
    profile_dir: Path,
    profile_id: str,
    frame_duration_ms: int,
    turn_timeout_s: float,
) -> None:
    """Replay explicitly selected reviewed turns through one S2S session."""

    try:
        turn_indexes = _parse_turns(turns)
        if output_dir is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            output_dir = DEFAULT_ARTIFACT_ROOT / f"s2s-replay-{stamp}"
        results = asyncio.run(
            run_s2s_replay(
                manifest_path=manifest_path,
                turn_indexes=turn_indexes,
                output_dir=output_dir,
                ws_url=ws_url,
                profile_dir=profile_dir,
                profile_id=profile_id,
                frame_duration_ms=frame_duration_ms,
                turn_timeout_s=turn_timeout_s,
            )
        )
    except (FileExistsError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    for result in results:
        click.echo(
            f"turn {result.index:02d}: transcript={result.observed_transcript!r} "
            f"assistant={result.assistant_text!r} response={result.response_id}"
        )
    click.echo(f"replay artifacts: {output_dir}")


if __name__ == "__main__":
    cli()
