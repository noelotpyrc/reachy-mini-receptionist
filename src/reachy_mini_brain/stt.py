"""Legacy speech-to-text wrapper using faster-whisper.

Status: legacy/manual-audio helper. The accepted product path uses the m1max S2S
backend STT. This module is still imported by the old daemon and the manual
``audio listen`` CLI; do not remove it until those CLI decisions are explicit.

Pure functions — no SDK dependency. Takes a WAV file or numpy array,
returns a transcript string.
"""

from __future__ import annotations

import os

import numpy as np


# Cache the model so repeated calls in the same process don't reload.
_model_cache: dict[str, object] = {}


def _get_model(model_size: str = "base"):
    """Get or create a cached WhisperModel."""
    if model_size not in _model_cache:
        from faster_whisper import WhisperModel

        try:
            cpu_threads = int(os.environ.get("REACHY_STT_CPU_THREADS", "0"))
        except ValueError:
            cpu_threads = 0
        _model_cache[model_size] = WhisperModel(
            model_size, compute_type="int8", cpu_threads=cpu_threads,
        )
    return _model_cache[model_size]


def transcribe(wav_path: str, model_size: str = "base", language: str | None = None) -> str:
    """Transcribe a WAV file to text.

    Args:
        wav_path: Path to a WAV/MP3/FLAC audio file.
        model_size: Whisper model size (tiny, base, small, medium, large-v3).
        language: Language code (e.g. "en"). None = auto-detect.

    Returns:
        Transcript as a single string.
    """
    model = _get_model(model_size)
    segments, _info = model.transcribe(
        wav_path,
        beam_size=5,
        language=language,
        vad_filter=True,
    )
    return " ".join(seg.text for seg in segments).strip()


def transcribe_array(
    samples: np.ndarray,
    sample_rate: int = 16000,
    model_size: str = "base",
    language: str | None = None,
) -> str:
    """Transcribe a numpy float32 audio array to text.

    The audio is saved to a temp WAV file, then transcribed.

    Args:
        samples: Float32 audio array (mono).
        sample_rate: Sample rate in Hz.
        model_size: Whisper model size.
        language: Language code or None for auto-detect.

    Returns:
        Transcript as a single string (empty if audio is silence/noise).
    """
    import tempfile

    import soundfile as sf

    # Ensure mono
    if samples.ndim > 1:
        samples = samples.mean(axis=1)

    # Skip transcription if audio is mostly silence (avoids Whisper hallucination)
    rms = float(np.sqrt(np.mean(samples**2)))
    if rms < 0.005:
        return ""

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as f:
        sf.write(f.name, samples, sample_rate)
        return transcribe(f.name, model_size=model_size, language=language)
