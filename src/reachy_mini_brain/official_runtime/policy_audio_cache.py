"""Cached audio playback for fixed reception policy lines."""

from __future__ import annotations

import json
import wave
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from numpy.typing import NDArray

from .stream_runtime import AudioFrame


DEFAULT_POLICY_AUDIO_FILENAMES: dict[str, str] = {
    "Goodbye! Have a nice day!": "goodbye-have-a-nice-day.wav",
    "Welcome!": "welcome.wav",
    "Hi! How can I help?": "hi-how-can-i-help.wav",
}


class PolicyAudioCache:
    """Resolve fixed policy text to cached WAV files."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        defaults: Mapping[str, str] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir).expanduser()
        self._mapping = dict(defaults or DEFAULT_POLICY_AUDIO_FILENAMES)
        self._mapping.update(self._load_manifest_mapping())

    def resolve(self, text: str) -> Path | None:
        """Return an existing cached WAV path for *text*, or None."""

        relative = self._mapping.get(text)
        if not relative:
            return None
        path = Path(relative)
        if not path.is_absolute():
            path = self.cache_dir / path
        if path.is_file():
            return path
        return None

    def expected_path(self, text: str) -> Path | None:
        """Return the configured cache path for diagnostics, even if missing."""

        relative = self._mapping.get(text)
        if not relative:
            return None
        path = Path(relative)
        return path if path.is_absolute() else self.cache_dir / path

    def _load_manifest_mapping(self) -> dict[str, str]:
        manifest_path = self.cache_dir / "manifest.json"
        if not manifest_path.is_file():
            return {}
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("texts"), dict):
            data = data["texts"]
        if not isinstance(data, dict):
            raise ValueError(f"Policy audio cache manifest must be an object: {manifest_path}")
        mapping: dict[str, str] = {}
        for text, path in data.items():
            if isinstance(text, str) and isinstance(path, str):
                mapping[text] = path
        return mapping


def load_policy_audio_frame(path: str | Path) -> AudioFrame:
    """Load a cached WAV as a mono int16 audio frame."""

    audio_path = Path(path)
    try:
        return _load_pcm_wav_with_wave(audio_path)
    except (ValueError, wave.Error):
        pass

    try:
        import soundfile as sf
    except Exception:  # noqa: BLE001
        return _load_pcm_wav_with_wave(audio_path)

    audio, sample_rate = sf.read(audio_path, always_2d=True, dtype="float32")
    return int(sample_rate), _as_int16_mono(audio)


def _load_pcm_wav_with_wave(path: Path) -> AudioFrame:
    with wave.open(str(path), "rb") as wav:
        sample_rate = int(wav.getframerate())
        channels = int(wav.getnchannels())
        sample_width = int(wav.getsampwidth())
        if sample_width != 2:
            raise ValueError(
                f"{path} is not a 16-bit PCM WAV and soundfile is unavailable; "
                "install the audio extra or use a PCM16 cache file."
            )
        raw = wav.readframes(wav.getnframes())
    audio = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        audio = audio.reshape(-1, channels).astype(np.int32).mean(axis=1).astype(np.int16)
    return sample_rate, audio.astype(np.int16, copy=False)


def _as_int16_mono(audio: NDArray[Any]) -> NDArray[np.int16]:
    arr = np.asarray(audio)
    if arr.ndim == 2:
        if arr.shape[1] > arr.shape[0]:
            arr = arr.T
        if arr.shape[1] > 1:
            arr = arr.astype(np.float32).mean(axis=1)
        else:
            arr = arr[:, 0]
    if arr.dtype == np.int16:
        return arr.astype(np.int16, copy=False)
    if np.issubdtype(arr.dtype, np.floating):
        arr = np.clip(arr, -1.0, 1.0) * 32767.0
    else:
        arr = np.clip(arr, -32768, 32767)
    return arr.astype(np.int16)
