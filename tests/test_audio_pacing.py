import numpy as np
import pytest

from reachy_mini_brain.audio import push_audio_realtime
from reachy_mini_brain.audio_pacing import (
    ROBOT_AUDIO_SAMPLE_RATE,
    WEBRTC_AUDIO_FRAME_MS,
    audio_frame_samples,
)


def test_audio_frame_samples_derives_chunk_size_from_rate_and_duration():
    assert audio_frame_samples() == 320
    assert audio_frame_samples(24_000) == 480
    assert audio_frame_samples(ROBOT_AUDIO_SAMPLE_RATE, frame_duration_ms=WEBRTC_AUDIO_FRAME_MS) == 320


def test_push_audio_realtime_uses_actual_chunk_duration():
    audio = np.zeros(800, dtype=np.float32)
    pushed: list[int] = []
    sleeps: list[float] = []
    now = [100.0]

    def push(chunk):
        pushed.append(len(chunk))

    def clock():
        return now[0]

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    push_audio_realtime(
        push,
        audio,
        sample_rate=ROBOT_AUDIO_SAMPLE_RATE,
        clock=clock,
        sleep=sleep,
    )

    assert pushed == [320, 320, 160]
    assert sleeps == pytest.approx([0.02, 0.02, 0.01])


def test_push_audio_realtime_derives_chunk_size_for_nondefault_sample_rate():
    audio = np.zeros(1_200, dtype=np.float32)
    pushed: list[int] = []
    sleeps: list[float] = []
    now = [100.0]

    def push(chunk):
        pushed.append(len(chunk))

    def clock():
        return now[0]

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    push_audio_realtime(
        push,
        audio,
        sample_rate=24_000,
        clock=clock,
        sleep=sleep,
    )

    assert pushed == [480, 480, 240]
    assert sleeps == pytest.approx([0.02, 0.02, 0.01])
