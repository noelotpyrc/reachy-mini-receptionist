"""Legacy text-to-speech wrapper using piper-tts.

Status: legacy/manual-audio helper. The accepted product path uses the m1max S2S
backend TTS. This module is still imported by the old daemon and the manual
``audio speak`` CLI; do not remove it until those CLI decisions are explicit.

Pure functions — no SDK dependency. Takes text, produces a WAV file
or numpy float32 array. Voice models are auto-downloaded on first use.
"""

from __future__ import annotations

import io
import json
import os
import sys
import wave
from pathlib import Path
from urllib.request import urlopen

import numpy as np

# Voice models are cached in ~/.local/share/piper-voices/
_VOICES_DIR = Path.home() / ".local" / "share" / "piper-voices"

# Default voice — clear, natural US English
DEFAULT_VOICE = "en_US-lessac-medium"

# Piper voice download base URL
_VOICE_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Cache loaded PiperVoice instances
_voice_cache: dict[str, object] = {}


def _voice_files(voice: str) -> tuple[Path, Path]:
    """Return (model_path, config_path) for a voice."""
    # Voice names like "en_US-lessac-medium" map to:
    #   en/en_US/lessac/medium/en_US-lessac-medium.onnx
    parts = voice.split("-")
    lang_country = parts[0]  # en_US
    lang = lang_country.split("_")[0]  # en
    name = parts[1]  # lessac
    quality = parts[2] if len(parts) > 2 else "medium"

    voice_dir = _VOICES_DIR / lang / lang_country / name / quality
    model_path = voice_dir / f"{voice}.onnx"
    config_path = voice_dir / f"{voice}.onnx.json"
    return model_path, config_path


def _download_voice(voice: str) -> tuple[Path, Path]:
    """Download a piper voice model if not already cached."""
    model_path, config_path = _voice_files(voice)

    if model_path.exists() and config_path.exists():
        return model_path, config_path

    parts = voice.split("-")
    lang_country = parts[0]
    lang = lang_country.split("_")[0]
    name = parts[1]
    quality = parts[2] if len(parts) > 2 else "medium"

    model_path.parent.mkdir(parents=True, exist_ok=True)

    base = f"{_VOICE_BASE_URL}/{lang}/{lang_country}/{name}/{quality}"

    for fname, dest in [
        (f"{voice}.onnx", model_path),
        (f"{voice}.onnx.json", config_path),
    ]:
        url = f"{base}/{fname}"
        print(f"  Downloading {fname}...", file=sys.stderr)
        with urlopen(url) as resp:
            dest.write_bytes(resp.read())
        print(f"  Saved to {dest}", file=sys.stderr)

    return model_path, config_path


def _get_voice(voice: str = DEFAULT_VOICE):
    """Get or create a cached PiperVoice."""
    if voice not in _voice_cache:
        from piper import PiperVoice

        model_path, config_path = _download_voice(voice)
        _voice_cache[voice] = PiperVoice.load(str(model_path), str(config_path))
    return _voice_cache[voice]


def synthesize(text: str, output_path: str, voice: str = DEFAULT_VOICE) -> str:
    """Synthesize text to a WAV file.

    Args:
        text: Text to speak.
        output_path: Path to write WAV file.
        voice: Piper voice name (e.g. "en_US-lessac-medium").

    Returns:
        The output_path.
    """
    piper_voice = _get_voice(voice)
    with wave.open(output_path, "wb") as wav_file:
        piper_voice.synthesize_wav(text, wav_file)
    return output_path


def synthesize_array(
    text: str, voice: str = DEFAULT_VOICE
) -> tuple[np.ndarray, int]:
    """Synthesize text to a numpy float32 array.

    Args:
        text: Text to speak.
        voice: Piper voice name.

    Returns:
        (samples, sample_rate) — samples is float32 in [-1, 1].
    """
    piper_voice = _get_voice(voice)
    chunks = list(piper_voice.synthesize(text))
    if not chunks:
        return np.array([], dtype=np.float32), 22050

    sample_rate = chunks[0].sample_rate
    audio = np.concatenate([c.audio_float_array for c in chunks])
    return audio, sample_rate
