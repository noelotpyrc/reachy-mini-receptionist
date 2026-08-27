from __future__ import annotations

import threading
import time

import numpy as np

from reachy_mini_brain.official_runtime.vision_broker_runtime import (
    BrokerVisionRuntime,
    VisionConsumerSpec,
)


def test_runtime_fans_canonical_frames_to_fifo_consumers():
    frames = iter(np.full((2, 3, 3), value, dtype=np.uint8) for value in range(4))
    recorder_packets = []
    gesture_packets = []

    runtime = BrokerVisionRuntime(
        frame_source=lambda: next(frames, None),
        capture_fps=200.0,
        consumers=(
            VisionConsumerSpec("recorder", recorder_packets.append, "fifo", 8),
            VisionConsumerSpec("gesture", gesture_packets.append, "fifo", 8),
        ),
    )
    runtime.start()
    deadline = time.monotonic() + 1.0
    while len(recorder_packets) < 4 and time.monotonic() < deadline:
        time.sleep(0.005)
    snapshot = runtime.close()

    assert [packet.frame_index for packet in recorder_packets] == [0, 1, 2, 3]
    assert [packet.frame_index for packet in gesture_packets] == [0, 1, 2, 3]
    assert all(left is right for left, right in zip(recorder_packets, gesture_packets, strict=True))
    assert snapshot["consumers"]["recorder"]["dropped_frames"] == 0
    assert snapshot["consumers"]["gesture"]["dropped_frames"] == 0


def test_runtime_latest_consumer_skips_stale_frames_without_fifo_loss():
    next_value = 0
    value_lock = threading.Lock()
    recorder_ids = []
    policy_ids = []

    def frame_source():
        nonlocal next_value
        with value_lock:
            value = next_value
            next_value += 1
        return np.full((2, 3, 3), value % 255, dtype=np.uint8)

    def slow_policy(packet):
        policy_ids.append(packet.frame_index)
        time.sleep(0.02)

    runtime = BrokerVisionRuntime(
        frame_source=frame_source,
        capture_fps=200.0,
        consumers=(
            VisionConsumerSpec("recorder", lambda packet: recorder_ids.append(packet.frame_index), "fifo", 64),
            VisionConsumerSpec("policy", slow_policy, "latest", 1),
        ),
    )
    runtime.start()
    time.sleep(0.12)
    snapshot = runtime.close()

    assert recorder_ids == list(range(len(recorder_ids)))
    assert policy_ids == sorted(policy_ids)
    assert len(policy_ids) < len(recorder_ids)
    assert snapshot["consumers"]["recorder"]["dropped_frames"] == 0
    assert snapshot["consumers"]["policy"]["dropped_frames"] > 0


def test_runtime_surfaces_consumer_failure_and_stops_capture():
    health = []
    release_frame = threading.Event()

    def fail(_packet):
        raise RuntimeError("consumer exploded")

    runtime = BrokerVisionRuntime(
        frame_source=lambda: (
            np.zeros((2, 3, 3), dtype=np.uint8) if release_frame.is_set() else None
        ),
        capture_fps=100.0,
        consumers=(VisionConsumerSpec("gesture", fail, "fifo", 2),),
        health_callback=lambda event, data: health.append((event, data)),
    )
    runtime.start()
    release_frame.set()
    deadline = time.monotonic() + 1.0
    while runtime.failure is None and time.monotonic() < deadline:
        time.sleep(0.005)
    snapshot = runtime.close()

    assert isinstance(runtime.failure, RuntimeError)
    assert snapshot["capture"]["failed"] is True
    assert snapshot["consumers"]["gesture"]["failed_frames"] == 1
    assert any(event == "consumer_failed" for event, _ in health)


def test_runtime_does_not_capture_until_all_consumers_are_initialized():
    allow_consumer_start = threading.Event()
    start_completed = threading.Event()
    source_calls = 0

    def frame_source():
        nonlocal source_calls
        source_calls += 1
        return np.zeros((2, 3, 3), dtype=np.uint8)

    runtime = BrokerVisionRuntime(
        frame_source=frame_source,
        capture_fps=100.0,
        consumers=(
            VisionConsumerSpec(
                "gesture",
                lambda packet: None,
                "fifo",
                2,
                start_callback=lambda: allow_consumer_start.wait(1.0),
            ),
        ),
    )
    starter = threading.Thread(
        target=lambda: (runtime.start(), start_completed.set()),
    )
    starter.start()

    time.sleep(0.03)
    assert source_calls == 0
    assert start_completed.is_set() is False

    allow_consumer_start.set()
    assert start_completed.wait(1.0)
    deadline = time.monotonic() + 1.0
    while source_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    runtime.close()
    starter.join()
    assert source_calls > 0
