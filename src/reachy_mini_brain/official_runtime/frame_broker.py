"""Fan-out delivery of canonical camera frames to independent consumers."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass
from typing import Literal

from .live_detection import FramePacket


SubscriptionMode = Literal["fifo", "latest"]


@dataclass(frozen=True)
class FrameSubscriptionSnapshot:
    name: str
    mode: SubscriptionMode
    capacity: int
    published_frames: int
    selected_frames: int
    dropped_frames: int
    queue_depth: int
    last_published_frame_id: int | None
    last_selected_frame_id: int | None
    last_dropped_frame_id: int | None
    closed: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class FrameSubscription:
    """One consumer's bounded inbox within a fan-out frame broker."""

    def __init__(self, name: str, *, mode: SubscriptionMode, capacity: int) -> None:
        if not name:
            raise ValueError("subscription name cannot be empty")
        if mode not in ("fifo", "latest"):
            raise ValueError(f"unknown subscription mode: {mode!r}")
        if capacity < 1:
            raise ValueError("subscription capacity must be positive")
        if mode == "latest" and capacity != 1:
            raise ValueError("latest-frame subscriptions must have capacity 1")

        self.name = name
        self.mode = mode
        self.capacity = capacity
        self._frames: deque[FramePacket] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._published_frames = 0
        self._selected_frames = 0
        self._dropped_frames = 0
        self._last_published_frame_id: int | None = None
        self._last_selected_frame_id: int | None = None
        self._last_dropped_frame_id: int | None = None

    def publish(self, packet: FramePacket) -> bool:
        """Publish without blocking the producer; drop the oldest packet on overflow."""

        with self._condition:
            if self._closed:
                return False
            self._published_frames += 1
            self._last_published_frame_id = packet.frame_index
            while len(self._frames) >= self.capacity:
                dropped = self._frames.popleft()
                self._dropped_frames += 1
                self._last_dropped_frame_id = dropped.frame_index
            self._frames.append(packet)
            self._condition.notify()
            return True

    def get(self, timeout: float | None = None) -> FramePacket | None:
        """Return the next packet from this inbox, or ``None`` on timeout/closed drain."""

        with self._condition:
            self._condition.wait_for(lambda: bool(self._frames) or self._closed, timeout=timeout)
            if not self._frames:
                return None
            packet = self._frames.popleft()
            self._selected_frames += 1
            self._last_selected_frame_id = packet.frame_index
            return packet

    def close(self) -> None:
        """Reject future publication and wake waiting consumers.

        Existing FIFO contents remain available so a recorder can drain cleanly.
        """

        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def drained(self) -> bool:
        with self._condition:
            return self._closed and not self._frames

    def snapshot(self) -> FrameSubscriptionSnapshot:
        with self._condition:
            return FrameSubscriptionSnapshot(
                name=self.name,
                mode=self.mode,
                capacity=self.capacity,
                published_frames=self._published_frames,
                selected_frames=self._selected_frames,
                dropped_frames=self._dropped_frames,
                queue_depth=len(self._frames),
                last_published_frame_id=self._last_published_frame_id,
                last_selected_frame_id=self._last_selected_frame_id,
                last_dropped_frame_id=self._last_dropped_frame_id,
                closed=self._closed,
            )


class FrameBroker:
    """Publish each canonical frame to every registered consumer inbox."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscriptions: dict[str, FrameSubscription] = {}
        self._closed = False
        self._published_frames = 0
        self._last_published_frame_id: int | None = None

    def subscribe(
        self,
        name: str,
        *,
        mode: SubscriptionMode,
        capacity: int = 1,
    ) -> FrameSubscription:
        with self._lock:
            if self._closed:
                raise RuntimeError("frame broker is closed")
            if name in self._subscriptions:
                raise ValueError(f"duplicate frame subscription: {name!r}")
            subscription = FrameSubscription(name, mode=mode, capacity=capacity)
            self._subscriptions[name] = subscription
            return subscription

    def publish(self, packet: FramePacket) -> int:
        with self._lock:
            if self._closed:
                return 0
            subscriptions = tuple(self._subscriptions.values())
            self._published_frames += 1
            self._last_published_frame_id = packet.frame_index
        return sum(subscription.publish(packet) for subscription in subscriptions)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subscriptions = tuple(self._subscriptions.values())
        for subscription in subscriptions:
            subscription.close()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            subscriptions = tuple(self._subscriptions.values())
            broker = {
                "published_frames": self._published_frames,
                "last_published_frame_id": self._last_published_frame_id,
                "closed": self._closed,
            }
        return {
            "broker": broker,
            "subscriptions": {
                subscription.name: subscription.snapshot().to_dict()
                for subscription in subscriptions
            },
        }
