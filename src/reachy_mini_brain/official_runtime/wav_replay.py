"""WAV replay helpers for official-style realtime handler tests."""

from __future__ import annotations

import asyncio
import contextlib
import wave
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from reachy_mini_brain.audio_pacing import WEBRTC_AUDIO_FRAME_MS, audio_frame_samples

from .events import EventSink, InMemoryEventSink
from .stream_runtime import AudioFrame, OfficialStyleStreamRuntime, RealtimeHandler


class WavAudioSource:
    """Read a PCM WAV file as official-style audio frames."""

    def __init__(
        self,
        path: str | Path,
        *,
        frame_duration_ms: int = WEBRTC_AUDIO_FRAME_MS,
        real_time: bool = False,
    ) -> None:
        if frame_duration_ms <= 0:
            raise ValueError("frame_duration_ms must be positive")
        self.path = Path(path)
        self.frame_duration_ms = frame_duration_ms
        self.real_time = real_time
        self._wav: wave.Wave_read | None = None
        self._sf_audio: np.ndarray | None = None
        self._sf_offset = 0
        try:
            self._wav = wave.open(str(self.path), "rb")
            self.sample_rate = int(self._wav.getframerate())
            self.channels = int(self._wav.getnchannels())
            self.sample_width = int(self._wav.getsampwidth())
            if self.sample_width != 2:
                self._wav.close()
                self._wav = None
                self._load_with_soundfile()
        except wave.Error:
            self._load_with_soundfile()
        self._frames_per_read = audio_frame_samples(self.sample_rate, frame_duration_ms=frame_duration_ms)

    async def read(self) -> AudioFrame | None:
        if self._wav is not None:
            raw = self._wav.readframes(self._frames_per_read)
            if not raw:
                return None
            audio = np.frombuffer(raw, dtype="<i2").astype(np.int16, copy=False)
            if self.channels > 1:
                audio = audio.reshape(-1, self.channels).astype(np.int32).mean(axis=1).astype(np.int16)
        elif self._sf_audio is not None:
            if self._sf_offset >= self._sf_audio.shape[0]:
                return None
            end = min(self._sf_audio.shape[0], self._sf_offset + self._frames_per_read)
            audio = _as_int16_mono(self._sf_audio[self._sf_offset:end])
            self._sf_offset = end
        else:
            return None
        if self.real_time:
            await asyncio.sleep(audio.shape[0] / float(self.sample_rate))
        else:
            await asyncio.sleep(0)
        return self.sample_rate, audio

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None

    def _load_with_soundfile(self) -> None:
        try:
            import soundfile as sf
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"{self.path} is not a 16-bit PCM WAV. Install the audio extra to read float/other WAVs: "
                ".venv/bin/python -m pip install -e '.[audio]'"
            ) from exc
        audio, sample_rate = sf.read(self.path, always_2d=True, dtype="float32")
        self._sf_audio = audio
        self._sf_offset = 0
        self.sample_rate = int(sample_rate)
        self.channels = int(audio.shape[1])
        self.sample_width = 0


class WavAudioSink:
    """Write official-style audio frames to a PCM WAV file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wav: wave.Wave_write | None = None
        self._sample_rate: int | None = None
        self.frames_written = 0

    async def write(self, frame: AudioFrame) -> None:
        sample_rate, audio = frame
        if self._wav is None:
            self._sample_rate = sample_rate
            self._wav = wave.open(str(self.path), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(sample_rate)
        elif sample_rate != self._sample_rate:
            raise ValueError(f"Output sample rate changed from {self._sample_rate} to {sample_rate}")

        audio_i16 = _as_int16_mono(audio)
        self._wav.writeframes(audio_i16.astype("<i2", copy=False).tobytes())
        self.frames_written += int(audio_i16.shape[0])
        await asyncio.sleep(0)

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None


async def run_wav_replay(
    *,
    input_wav: str | Path,
    output_wav: str | Path,
    handler: RealtimeHandler,
    event_sink: EventSink | None = None,
    frame_duration_ms: int = WEBRTC_AUDIO_FRAME_MS,
    real_time: bool = False,
    runtime_options: dict[str, Any] | None = None,
) -> EventSink:
    """Replay one WAV through a handler and write emitted audio to another WAV."""

    events = event_sink or InMemoryEventSink()
    source = WavAudioSource(input_wav, frame_duration_ms=frame_duration_ms, real_time=real_time)
    sink = WavAudioSink(output_wav)
    runtime = OfficialStyleStreamRuntime(
        handler=handler,
        audio_source=source,
        audio_sink=sink,
        event_sink=events,
        **(runtime_options or {}),
    )
    try:
        await runtime.run()
    finally:
        source.close()
        sink.close()
    return events


def _as_int16_mono(audio: NDArray[Any]) -> NDArray[np.int16]:
    arr = np.asarray(audio)
    if arr.ndim == 2:
        if arr.shape[1] > arr.shape[0]:
            arr = arr.T
        if arr.shape[1] > 1:
            arr = arr.astype(np.int32).mean(axis=1)
        else:
            arr = arr[:, 0]
    if arr.dtype == np.int16:
        return arr.astype(np.int16, copy=False)
    if np.issubdtype(arr.dtype, np.floating):
        arr = np.clip(arr, -1.0, 1.0) * 32767.0
    else:
        arr = np.clip(arr, -32768, 32767)
    return arr.astype(np.int16)


@contextlib.contextmanager
def write_test_wav(path: str | Path, sample_rate: int, audio: NDArray[Any]):
    """Write a temporary test WAV and yield its path."""

    sink = WavAudioSink(path)
    try:
        asyncio.run(sink.write((sample_rate, _as_int16_mono(audio))))
        yield Path(path)
    finally:
        sink.close()
