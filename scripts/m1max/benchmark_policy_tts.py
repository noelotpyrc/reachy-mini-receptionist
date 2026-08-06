#!/usr/bin/env python3
"""Benchmark exact-text policy TTS through the realtime WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import statistics
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets


SAMPLE_RATE = 16_000
SCENARIOS = (
    ("greet", "Welcome to the clinic, how can I help?"),
    ("goodbye", "Goodbye! Have a nice day!"),
)


class BenchmarkError(RuntimeError):
    """Raised when one realtime response violates the benchmark contract."""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8765/v1/realtime")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--voice", default="Sohee")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wav-dir", type=Path)
    args = parser.parse_args()
    if args.runs < 1 or args.warmups < 0:
        parser.error("--runs must be positive and --warmups cannot be negative")
    if args.timeout <= 0.0:
        parser.error("--timeout must be positive")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    return args


def _session_update(voice: str) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": "Policy TTS integration benchmark.",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": None},
                    "transcription": {"model": "gpt-4o-transcribe", "language": "en"},
                    "turn_detection": {"type": "server_vad", "interrupt_response": True},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": None},
                    "voice": voice,
                },
            },
            "tools": [],
            "tool_choice": "auto",
        },
    }


def _response_id(event: dict[str, Any]) -> str | None:
    response_id = event.get("response_id")
    if isinstance(response_id, str):
        return response_id
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        return response["id"]
    return None


def _transcript(event: dict[str, Any]) -> str | None:
    for key in ("transcript", "text"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    return None


async def _wait_for_session_created(ws: Any, timeout: float) -> dict[str, Any]:
    async with asyncio.timeout(timeout):
        while True:
            event = json.loads(await ws.recv())
            event_type = event.get("type")
            if event_type == "session.created":
                return event
            if event_type == "error":
                raise BenchmarkError(f"session failed: {event.get('error')!r}")


async def _run_sample(
    ws: Any,
    *,
    scenario: str,
    text: str,
    iteration: int,
    timeout: float,
) -> tuple[dict[str, Any], bytes]:
    metadata = {
        "benchmark": "policy_tts",
        "scenario": scenario,
        "iteration": str(iteration),
    }
    started = time.perf_counter()
    await ws.send(json.dumps({"type": "response.cancel"}))
    await ws.send(json.dumps({"type": "tts.create", "text": text, "metadata": metadata}))

    marks: dict[str, float] = {}
    response_id: str | None = None
    transcript: str | None = None
    audio = bytearray()
    status: str | None = None
    event_types: list[str] = []
    async with asyncio.timeout(timeout):
        while True:
            event = json.loads(await ws.recv())
            now = time.perf_counter()
            event_type = str(event.get("type") or "unknown")
            event_types.append(event_type)
            if event_type == "error":
                raise BenchmarkError(f"backend error: {event.get('error')!r}")
            event_response_id = _response_id(event)
            if event_type == "response.created":
                response_id = event_response_id
                marks["created"] = now
            elif response_id is not None and event_response_id not in {None, response_id}:
                continue
            elif event_type == "response.output_audio_transcript.done":
                transcript = _transcript(event)
                marks["transcript_done"] = now
            elif event_type == "response.output_audio.delta":
                if "first_audio" not in marks:
                    marks["first_audio"] = now
                delta = event.get("delta")
                if isinstance(delta, str):
                    audio.extend(base64.b64decode(delta))
            elif event_type == "response.output_audio.done":
                marks["audio_done"] = now
            elif event_type == "response.done":
                response = event.get("response")
                status = str(response.get("status")) if isinstance(response, dict) else None
                marks["response_done"] = now
                break

    required = ("created", "transcript_done", "first_audio", "audio_done", "response_done")
    missing = [name for name in required if name not in marks]
    if missing:
        raise BenchmarkError(f"response omitted milestones: {missing}; events={event_types}")
    if status != "completed":
        raise BenchmarkError(f"response status was {status!r}")
    if not audio:
        raise BenchmarkError("response completed without audio bytes")

    def elapsed(name: str) -> float:
        return round((marks[name] - started) * 1000.0, 3)

    return (
        {
            "scenario": scenario,
            "iteration": iteration,
            "expected_text": text,
            "transcript": transcript,
            "exact_transcript": transcript == text,
            "response_id": response_id,
            "status": status,
            "created_ms": elapsed("created"),
            "transcript_done_ms": elapsed("transcript_done"),
            "first_audio_ms": elapsed("first_audio"),
            "audio_done_ms": elapsed("audio_done"),
            "response_done_ms": elapsed("response_done"),
            "audio_bytes": len(audio),
            "audio_duration_ms": round(len(audio) / (2 * SAMPLE_RATE) * 1000.0, 3),
            "event_types": event_types,
        },
        bytes(audio),
    )


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _distribution(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 3),
        "p50": round(statistics.median(values), 3),
        "p90": round(_percentile(values, 0.90), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def _summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = (
        "created_ms",
        "transcript_done_ms",
        "first_audio_ms",
        "audio_done_ms",
        "response_done_ms",
    )
    summary: dict[str, Any] = {
        "samples": len(samples),
        "exact_transcripts": sum(bool(sample["exact_transcript"]) for sample in samples),
    }
    for metric in metrics:
        summary[metric] = _distribution([float(sample[metric]) for sample in samples])
    return summary


def _write_wav(path: Path, audio: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(audio)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    wav_paths: dict[str, str] = {}
    async with websockets.connect(args.url, max_size=None) as ws:
        await ws.send(json.dumps(_session_update(args.voice)))
        session_event = await _wait_for_session_created(ws, args.timeout)
        for scenario, text in SCENARIOS:
            for index in range(-args.warmups, args.runs):
                phase = "warmup" if index < 0 else "sample"
                display_index = index + args.warmups + 1 if index < 0 else index + 1
                print(f"[policy-tts] {scenario} {phase} {display_index}", flush=True)
                sample, audio = await _run_sample(
                    ws,
                    scenario=scenario,
                    text=text,
                    iteration=index + 1,
                    timeout=args.timeout,
                )
                if index < 0:
                    continue
                samples.append(sample)
                if scenario not in wav_paths and args.wav_dir is not None:
                    wav_path = args.wav_dir / f"{scenario}.wav"
                    if wav_path.exists():
                        raise BenchmarkError(f"refusing to overwrite WAV: {wav_path}")
                    _write_wav(wav_path, audio)
                    wav_paths[scenario] = str(wav_path)

    return {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "voice": args.voice,
        "sample_rate": SAMPLE_RATE,
        "runs_per_scenario": args.runs,
        "warmups_per_scenario": args.warmups,
        "session_id": session_event.get("session", {}).get("id"),
        "wav_paths": wav_paths,
        "summary": {
            scenario: _summarize([sample for sample in samples if sample["scenario"] == scenario])
            for scenario, _ in SCENARIOS
        },
        "overall": _summarize(samples),
        "samples": samples,
    }


def main() -> int:
    args = _parse_args()
    try:
        report = asyncio.run(_run(args))
    except (BenchmarkError, TimeoutError) as exc:
        raise SystemExit(f"benchmark failed: {exc}") from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    print(f"report -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
