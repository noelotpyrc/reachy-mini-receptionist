"""Prepare stable, turn-based audio fixtures for S2S integration replay."""

from __future__ import annotations

import hashlib
import html
import json
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import numpy as np

from .env import PROJECT_ROOT


SOURCE_RUN_ID = "official-live-20260625-133754"
FIXTURE_REVISION = 2
DEFAULT_SOURCE_WAV = (
    PROJECT_ROOT
    / "artifacts"
    / "official-runtime-live"
    / "audio-review"
    / SOURCE_RUN_ID
    / f"audio-review-{SOURCE_RUN_ID}.wav"
)
DEFAULT_SOURCE_METADATA = DEFAULT_SOURCE_WAV.with_suffix(".json")
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "hermes-s2s-e2e" / SOURCE_RUN_ID / "stable-v2"


@dataclass(frozen=True)
class SourceSegment:
    start_s: float
    end_s: float


@dataclass(frozen=True)
class ReplayTurn:
    index: int
    transcript: str
    segments: tuple[SourceSegment, ...]
    semantic_check: str


# These are sample-clock ranges in the aligned review WAV, not backend event-clock
# ranges. The latter drift from the recorded mic samples later in this run.
STABLE_REPLAY_TURNS = (
    ReplayTurn(
        1,
        "Hey, nice to meet you.",
        (SourceSegment(49.70, 51.35),),
        "Respond naturally to the visitor greeting.",
    ),
    ReplayTurn(
        2,
        "My name is Mike. I'm here for appointment.",
        (SourceSegment(57.20, 59.35), SourceSegment(59.50, 61.25)),
        "Retain the visitor name Mike and the appointment intent.",
    ),
    ReplayTurn(
        3,
        "Two thirty.",
        (SourceSegment(67.25, 68.10),),
        "Interpret 2:30 as the appointment time in the active conversation.",
    ),
    ReplayTurn(
        4,
        "I think I'm late for the tournament.",
        (
            SourceSegment(76.85, 78.78),
            SourceSegment(79.08, 80.70),
            SourceSegment(80.76, 82.08),
        ),
        "Respond helpfully to the visitor's concern about being late.",
    ),
    ReplayTurn(
        5,
        "So what can you do here?",
        (SourceSegment(87.55, 88.80),),
        "Describe only supported receptionist capabilities.",
    ),
    ReplayTurn(
        6,
        "Excuse me?",
        (SourceSegment(108.55, 109.30),),
        "Recover naturally from a request for clarification.",
    ),
    ReplayTurn(
        7,
        "Yeah, but do we have water?",
        (SourceSegment(118.85, 120.40),),
        "Do not invent an unknown water-availability fact.",
    ),
    ReplayTurn(
        8,
        "Okay, okay. You don't know anything. Do you know my name?",
        (SourceSegment(127.65, 130.35),),
        "Recall that the visitor said their name is Mike.",
    ),
    ReplayTurn(
        9,
        "That's pretty good. You remember my name. What's your name?",
        (SourceSegment(135.65, 138.35), SourceSegment(138.55, 139.70)),
        "Preserve visitor-name continuity and identify the receptionist appropriately.",
    ),
    ReplayTurn(
        10,
        "You uh you were speaking too fast.",
        (SourceSegment(148.85, 151.10),),
        "Acknowledge the feedback and adapt the next response.",
    ),
    ReplayTurn(
        11,
        "Okay, thank you for repeating that.",
        (SourceSegment(160.65, 161.75),),
        "Acknowledge the visitor without restarting the interaction.",
    ),
    ReplayTurn(
        12,
        "Okay, bye.",
        (SourceSegment(166.85, 168.10),),
        "Close the conversation naturally.",
    ),
)


def prepare_replay_fixture(
    *,
    source_wav: Path,
    output_dir: Path,
    turns: tuple[ReplayTurn, ...] = STABLE_REPLAY_TURNS,
    source_metadata: Path | None = None,
    leading_silence_s: float = 0.25,
    internal_silence_s: float = 0.16,
    trailing_silence_s: float = 0.90,
) -> Path:
    """Write one PCM16 mono WAV per semantic turn and return its manifest."""

    source_wav = source_wav.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    source_metadata = source_metadata.expanduser().resolve() if source_metadata is not None else None
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing replay fixture: {output_dir}")
    audio, sample_rate, source_channels = _read_pcm16_channel(source_wav, channel=0)
    _validate_turns(turns, source_samples=int(audio.shape[0]), sample_rate=sample_rate)

    output_dir.mkdir(parents=True, exist_ok=False)
    leading_samples = _duration_samples(leading_silence_s, sample_rate)
    internal_samples = _duration_samples(internal_silence_s, sample_rate)
    trailing_samples = _duration_samples(trailing_silence_s, sample_rate)
    silence_leading = np.zeros(leading_samples, dtype="<i2")
    silence_internal = np.zeros(internal_samples, dtype="<i2")
    silence_trailing = np.zeros(trailing_samples, dtype="<i2")

    manifest_turns: list[dict[str, Any]] = []
    for turn in turns:
        parts: list[np.ndarray] = [silence_leading]
        segment_rows: list[dict[str, Any]] = []
        for segment_index, segment in enumerate(turn.segments):
            start_sample = _seconds_to_sample(segment.start_s, sample_rate)
            end_sample = _seconds_to_sample(segment.end_s, sample_rate)
            parts.append(audio[start_sample:end_sample])
            segment_rows.append(
                {
                    "start_s": segment.start_s,
                    "end_s": segment.end_s,
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "samples": end_sample - start_sample,
                }
            )
            if segment_index + 1 < len(turn.segments):
                parts.append(silence_internal)
        parts.append(silence_trailing)
        clip = np.concatenate(parts).astype("<i2", copy=False)
        wav_path = output_dir / f"turn-{turn.index:02d}.wav"
        _write_pcm16_mono(wav_path, clip, sample_rate)
        manifest_turns.append(
            {
                "index": turn.index,
                "path": wav_path.name,
                "sha256": _sha256_file(wav_path),
                "sample_rate": sample_rate,
                "channels": 1,
                "sample_width_bytes": 2,
                "samples": int(clip.shape[0]),
                "duration_s": round(clip.shape[0] / float(sample_rate), 3),
                "expected_transcript": turn.transcript,
                "semantic_check": turn.semantic_check,
                "source_segments": segment_rows,
                "review_status": "pending",
            }
        )

    manifest = {
        "schema_version": 1,
        "fixture_revision": FIXTURE_REVISION,
        "fixture": "hermes-s2s-stable-replay",
        "mode": "stable",
        "source_run_id": SOURCE_RUN_ID,
        "source": _source_manifest(
            source_wav=source_wav,
            source_metadata=source_metadata,
            sample_rate=sample_rate,
            channels=source_channels,
            samples=int(audio.shape[0]),
        ),
        "audio_format": {
            "encoding": "PCM_S16LE",
            "sample_rate": sample_rate,
            "channels": 1,
        },
        "silence": {
            "leading_s": leading_silence_s,
            "between_source_segments_s": internal_silence_s,
            "trailing_s": trailing_silence_s,
        },
        "session": {
            "conversation_count": 1,
            "reconnect_between_turns": False,
            "turn_count": len(manifest_turns),
        },
        "turns": manifest_turns,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_playlist(output_dir, manifest_turns)
    _write_review_html(output_dir, manifest_turns)
    return manifest_path


def _read_pcm16_channel(path: Path, *, channel: int) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as wav:
        channels = int(wav.getnchannels())
        sample_width = int(wav.getsampwidth())
        sample_rate = int(wav.getframerate())
        compression = wav.getcomptype()
        frames = int(wav.getnframes())
        raw = wav.readframes(frames)
    if compression != "NONE" or sample_width != 2:
        raise ValueError(f"{path} must be uncompressed 16-bit PCM WAV")
    if channel < 0 or channel >= channels:
        raise ValueError(f"{path} has {channels} channels; channel {channel} is unavailable")
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size % channels:
        raise ValueError(f"{path} has an incomplete interleaved PCM frame")
    return samples.reshape(-1, channels)[:, channel].copy(), sample_rate, channels


def _write_pcm16_mono(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(np.asarray(audio, dtype="<i2").reshape(-1).tobytes())


def _validate_turns(turns: tuple[ReplayTurn, ...], *, source_samples: int, sample_rate: int) -> None:
    if not turns:
        raise ValueError("at least one replay turn is required")
    expected_indexes = list(range(1, len(turns) + 1))
    indexes = [turn.index for turn in turns]
    if indexes != expected_indexes:
        raise ValueError(f"turn indexes must be contiguous from 1: {indexes}")
    for turn in turns:
        if not turn.segments:
            raise ValueError(f"turn {turn.index} has no source segments")
        previous_end = -1
        for segment in turn.segments:
            start_sample = _seconds_to_sample(segment.start_s, sample_rate)
            end_sample = _seconds_to_sample(segment.end_s, sample_rate)
            if start_sample < 0 or end_sample <= start_sample or end_sample > source_samples:
                raise ValueError(f"turn {turn.index} has invalid source segment {segment}")
            if start_sample < previous_end:
                raise ValueError(f"turn {turn.index} source segments overlap or are out of order")
            previous_end = end_sample


def _source_manifest(
    *,
    source_wav: Path,
    source_metadata: Path | None,
    sample_rate: int,
    channels: int,
    samples: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "wav_path": _display_path(source_wav),
        "wav_sha256": _sha256_file(source_wav),
        "sample_rate": sample_rate,
        "channels": channels,
        "samples": samples,
        "selected_channel": 0,
        "selected_channel_label": "input mic",
    }
    if source_metadata is not None:
        payload["metadata_path"] = _display_path(source_metadata)
        if source_metadata.exists():
            payload["metadata_sha256"] = _sha256_file(source_metadata)
            metadata = json.loads(source_metadata.read_text(encoding="utf-8"))
            payload["alignment"] = metadata.get("alignment")
    return payload


def _write_playlist(output_dir: Path, turns: list[dict[str, Any]]) -> None:
    lines = ["#EXTM3U"]
    for turn in turns:
        lines.append(f"#EXTINF:{turn['duration_s']},{turn['index']:02d} - {turn['expected_transcript']}")
        lines.append(str(turn["path"]))
    (output_dir / "review.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_review_html(output_dir: Path, turns: list[dict[str, Any]]) -> None:
    items = []
    for turn in turns:
        transcript = html.escape(str(turn["expected_transcript"]))
        semantic_check = html.escape(str(turn["semantic_check"]))
        path = html.escape(str(turn["path"]), quote=True)
        items.append(
            "<section>"
            f"<h2>{turn['index']:02d}. {transcript}</h2>"
            f"<audio controls preload=\"metadata\" src=\"{path}\"></audio>"
            f"<p>{turn['duration_s']:.3f}s</p>"
            f"<small>{semantic_check}</small>"
            "</section>"
        )
    document = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes S2S stable replay review</title>
<style>
body { margin: 0; font: 15px/1.45 system-ui, sans-serif; color: #1f2428; background: #f6f8fa; }
main { width: min(880px, calc(100% - 32px)); margin: 32px auto; }
h1 { font-size: 24px; }
section { padding: 16px 0; border-top: 1px solid #d0d7de; }
h2 { margin: 0 0 10px; font-size: 16px; font-weight: 600; }
audio { width: 100%; }
p, small { color: #59636e; }
</style>
</head>
<body>
<main>
<h1>Hermes S2S stable replay: 12 input turns</h1>
<!-- TURNS -->
</main>
</body>
</html>
""".replace("<!-- TURNS -->", "\n".join(items))
    (output_dir / "review.html").write_text(document, encoding="utf-8")


def _duration_samples(duration_s: float, sample_rate: int) -> int:
    if duration_s < 0:
        raise ValueError("silence durations must be non-negative")
    return int(round(duration_s * sample_rate))


def _seconds_to_sample(value: float, sample_rate: int) -> int:
    return int(round(value * sample_rate))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


@click.command()
@click.option("--source-wav", type=click.Path(path_type=Path), default=DEFAULT_SOURCE_WAV, show_default=True)
@click.option(
    "--source-metadata",
    type=click.Path(path_type=Path),
    default=DEFAULT_SOURCE_METADATA,
    show_default=True,
)
@click.option("--output-dir", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT_DIR, show_default=True)
def cli(source_wav: Path, source_metadata: Path, output_dir: Path) -> None:
    """Prepare the stable 12-turn Hermes S2S replay fixture."""

    try:
        manifest_path = prepare_replay_fixture(
            source_wav=source_wav,
            source_metadata=source_metadata,
            output_dir=output_dir,
        )
    except (FileExistsError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Hermes S2S replay fixture written: {manifest_path}")


if __name__ == "__main__":
    cli()
