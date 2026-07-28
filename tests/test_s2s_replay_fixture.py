from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from reachy_mini_brain.official_runtime.s2s_replay_fixture import (
    ReplayTurn,
    SourceSegment,
    prepare_replay_fixture,
)


def test_prepare_replay_fixture_extracts_channel_and_inserts_stable_silence(tmp_path: Path) -> None:
    sample_rate = 100
    left = np.arange(100, dtype=np.int16)
    right = np.full(100, 2000, dtype=np.int16)
    source = tmp_path / "aligned.wav"
    _write_stereo_wav(source, sample_rate, left, right)
    output_dir = tmp_path / "fixture"
    turns = (
        ReplayTurn(
            index=1,
            transcript="First test turn.",
            segments=(SourceSegment(0.10, 0.20), SourceSegment(0.30, 0.40)),
            semantic_check="Remember the first turn.",
        ),
        ReplayTurn(
            index=2,
            transcript="Second test turn.",
            segments=(SourceSegment(0.50, 0.70),),
            semantic_check="Answer the second turn.",
        ),
    )

    manifest_path = prepare_replay_fixture(
        source_wav=source,
        output_dir=output_dir,
        turns=turns,
        leading_silence_s=0.02,
        internal_silence_s=0.03,
        trailing_silence_s=0.04,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first, first_rate = _read_mono_wav(output_dir / "turn-01.wav")
    second, second_rate = _read_mono_wav(output_dir / "turn-02.wav")

    assert first_rate == second_rate == sample_rate
    assert np.array_equal(
        first,
        np.concatenate(
            [
                np.zeros(2, dtype=np.int16),
                left[10:20],
                np.zeros(3, dtype=np.int16),
                left[30:40],
                np.zeros(4, dtype=np.int16),
            ]
        ),
    )
    assert np.array_equal(
        second,
        np.concatenate([np.zeros(2, dtype=np.int16), left[50:70], np.zeros(4, dtype=np.int16)]),
    )
    assert manifest["audio_format"] == {
        "channels": 1,
        "encoding": "PCM_S16LE",
        "sample_rate": sample_rate,
    }
    assert manifest["session"] == {
        "conversation_count": 1,
        "reconnect_between_turns": False,
        "turn_count": 2,
    }
    assert manifest["source"]["channels"] == 2
    assert manifest["source"]["selected_channel"] == 0
    assert manifest["turns"][0]["expected_transcript"] == "First test turn."
    assert len(manifest["turns"][0]["sha256"]) == 64
    assert (output_dir / "review.html").exists()
    assert (output_dir / "review.m3u").exists()


def test_prepare_replay_fixture_refuses_to_overwrite_existing_directory(tmp_path: Path) -> None:
    source = tmp_path / "aligned.wav"
    samples = np.arange(100, dtype=np.int16)
    _write_stereo_wav(source, 100, samples, samples)
    output_dir = tmp_path / "fixture"
    output_dir.mkdir()
    turns = (ReplayTurn(1, "Test.", (SourceSegment(0.0, 0.5),), "Respond."),)

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        prepare_replay_fixture(source_wav=source, output_dir=output_dir, turns=turns)


def _write_stereo_wav(path: Path, sample_rate: int, left: np.ndarray, right: np.ndarray) -> None:
    audio = np.column_stack([left, right]).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio.tobytes())


def _read_mono_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        sample_rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    return np.frombuffer(raw, dtype="<i2"), sample_rate
