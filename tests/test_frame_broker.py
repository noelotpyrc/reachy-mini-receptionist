from __future__ import annotations

import numpy as np
import pytest

from reachy_mini_brain.official_runtime.frame_broker import FrameBroker, FrameSubscription
from reachy_mini_brain.official_runtime.live_detection import FramePacket


def _packet(frame_id: int) -> FramePacket:
    return FramePacket(
        frame_index=frame_id,
        frame_ts=100.0 + frame_id / 15.0,
        frame_bgr=np.full((2, 3, 3), frame_id, dtype=np.uint8),
    )


def test_broker_fans_same_packet_out_to_independent_inboxes():
    broker = FrameBroker()
    recorder = broker.subscribe("recorder", mode="fifo", capacity=4)
    gesture = broker.subscribe("gesture", mode="fifo", capacity=4)
    packet = _packet(7)

    assert broker.publish(packet) == 2
    assert gesture.get(timeout=0) is packet
    assert recorder.snapshot().queue_depth == 1
    assert recorder.get(timeout=0) is packet


def test_fifo_subscription_preserves_order_and_reports_oldest_drop():
    subscription = FrameSubscription("recorder", mode="fifo", capacity=2)

    subscription.publish(_packet(1))
    subscription.publish(_packet(2))
    subscription.publish(_packet(3))

    assert subscription.get(timeout=0).frame_index == 2
    assert subscription.get(timeout=0).frame_index == 3
    snapshot = subscription.snapshot()
    assert snapshot.published_frames == 3
    assert snapshot.selected_frames == 2
    assert snapshot.dropped_frames == 1
    assert snapshot.last_dropped_frame_id == 1


def test_latest_subscription_replaces_stale_frame():
    subscription = FrameSubscription("policy", mode="latest", capacity=1)

    subscription.publish(_packet(10))
    subscription.publish(_packet(11))
    subscription.publish(_packet(12))

    assert subscription.get(timeout=0).frame_index == 12
    snapshot = subscription.snapshot()
    assert snapshot.dropped_frames == 2
    assert snapshot.last_dropped_frame_id == 11


def test_closed_subscription_drains_existing_frames_and_rejects_new_publication():
    subscription = FrameSubscription("recorder", mode="fifo", capacity=2)
    subscription.publish(_packet(1))
    subscription.close()

    assert subscription.publish(_packet(2)) is False
    assert subscription.get(timeout=0).frame_index == 1
    assert subscription.get(timeout=0) is None
    assert subscription.drained is True


def test_broker_rejects_invalid_or_duplicate_subscriptions():
    broker = FrameBroker()
    broker.subscribe("gesture", mode="fifo", capacity=2)

    with pytest.raises(ValueError, match="duplicate"):
        broker.subscribe("gesture", mode="fifo", capacity=2)
    with pytest.raises(ValueError, match="capacity 1"):
        broker.subscribe("latest", mode="latest", capacity=2)

    broker.close()
    with pytest.raises(RuntimeError, match="closed"):
        broker.subscribe("recorder", mode="fifo", capacity=2)
