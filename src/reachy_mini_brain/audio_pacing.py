"""Shared audio pacing constants for Reachy Mini WebRTC speaker output."""

from __future__ import annotations

ROBOT_AUDIO_SAMPLE_RATE = 16_000
WEBRTC_AUDIO_FRAME_MS = 20


def audio_frame_samples(
    sample_rate: int = ROBOT_AUDIO_SAMPLE_RATE,
    *,
    frame_duration_ms: int = WEBRTC_AUDIO_FRAME_MS,
) -> int:
    """Return sample count for one realtime audio frame."""

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if frame_duration_ms <= 0:
        raise ValueError("frame_duration_ms must be positive")
    return max(1, round(sample_rate * frame_duration_ms / 1000))
