from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import wave
from pathlib import Path

import numpy as np
import pytest

from reachy_mini_brain.official_runtime.s2s_replay import _parse_turns, run_s2s_replay


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        return json.dumps(item)

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(StopAsyncIteration)


def test_run_s2s_replay_paces_audio_and_correlates_turn_artifacts(tmp_path: Path) -> None:
    async def run():
        fixture_dir = tmp_path / "fixture"
        fixture_dir.mkdir()
        wav_path = fixture_dir / "turn-01.wav"
        samples = np.arange(640, dtype=np.int16)
        _write_wav(wav_path, samples)
        manifest_path = _write_manifest(fixture_dir, wav_path)
        websocket = _FakeWebSocket()
        sleep_calls: list[float] = []

        async def connect_factory(_url: str):
            await websocket.incoming.put({"type": "session.created", "session": {"id": "session-1"}})
            return websocket

        async def fake_sleep(duration: float) -> None:
            sleep_calls.append(duration)
            await asyncio.sleep(0)

        async def simulate_server() -> None:
            while len([item for item in websocket.sent if item["type"] == "input_audio_buffer.append"]) < 2:
                await asyncio.sleep(0)
            audio = np.array([11, -12, 13, -14], dtype="<i2")
            encoded = base64.b64encode(audio.tobytes()).decode("ascii")
            for event in (
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item-1",
                    "transcript": "Hello.",
                },
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item-1",
                    "transcript": "Hello clinic.",
                },
                {"type": "response.output_audio.delta", "response_id": "resp-1", "delta": encoded},
                {
                    "type": "response.output_audio_transcript.done",
                    "response_id": "resp-1",
                    "transcript": "Hello. How can I help?",
                },
                {"type": "response.output_audio.done", "response_id": "resp-1"},
                {"type": "response.done", "response": {"id": "resp-1", "status": "completed"}},
            ):
                await websocket.incoming.put(event)

        server_task = asyncio.create_task(simulate_server())
        results = await run_s2s_replay(
            manifest_path=manifest_path,
            turn_indexes=(1,),
            output_dir=tmp_path / "run",
            connect_factory=connect_factory,
            sleep=fake_sleep,
            turn_timeout_s=1.0,
        )
        await server_task
        return results, websocket, sleep_calls

    results, websocket, sleep_calls = asyncio.run(run())
    result = results[0]
    assert websocket.closed is True
    assert [item["type"] for item in websocket.sent] == [
        "session.update",
        "input_audio_buffer.append",
        "input_audio_buffer.append",
    ]
    assert sleep_calls == pytest.approx([0.02, 0.02])
    assert result.observed_transcript == "Hello clinic."
    assert result.assistant_text == "Hello. How can I help?"
    assert result.response_id == "resp-1"
    assert result.response_created_ts is None
    assert result.input_samples == 640
    assert result.output_samples == 4
    assert (tmp_path / "run" / "events.jsonl").exists()
    report = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    assert report["connection_count"] == 1
    assert report["turn_indexes"] == [1]
    with wave.open(result.output_wav, "rb") as output:
        assert np.array_equal(np.frombuffer(output.readframes(output.getnframes()), dtype="<i2"), [11, -12, 13, -14])


def test_run_s2s_replay_rejects_existing_output_without_connecting(tmp_path: Path) -> None:
    output_dir = tmp_path / "existing"
    output_dir.mkdir()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        asyncio.run(
            run_s2s_replay(
                manifest_path=tmp_path / "unused.json",
                turn_indexes=(1,),
                output_dir=output_dir,
            )
        )


def test_parse_turns_requires_explicit_unique_ascending_ranges() -> None:
    assert _parse_turns("2,3,8") == (2, 3, 8)
    assert _parse_turns("1-3,8") == (1, 2, 3, 8)
    with pytest.raises(ValueError, match="descending"):
        _parse_turns("3-1")


def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(samples.astype("<i2").tobytes())


def _write_manifest(fixture_dir: Path, wav_path: Path) -> Path:
    digest = hashlib.sha256(wav_path.read_bytes()).hexdigest()
    manifest = {
        "fixture": "hermes-s2s-stable-replay",
        "turns": [
            {
                "index": 1,
                "path": wav_path.name,
                "sha256": digest,
                "expected_transcript": "Hello clinic.",
            }
        ],
    }
    path = fixture_dir / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path
