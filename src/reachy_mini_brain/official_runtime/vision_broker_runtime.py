"""Threaded execution runtime for fan-out vision frame consumers."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from numpy.typing import NDArray
import numpy as np

from .frame_broker import FrameBroker, FrameSubscription, SubscriptionMode
from .live_detection import FramePacket


FrameSource = Callable[[], NDArray[np.uint8] | None]
FrameCallback = Callable[[FramePacket], None]
StartCallback = Callable[[], None]
HealthCallback = Callable[[str, Mapping[str, Any]], None]


@dataclass(frozen=True)
class VisionConsumerSpec:
    name: str
    callback: FrameCallback
    mode: SubscriptionMode
    capacity: int
    idle_after_s: float = 0.0
    start_callback: StartCallback | None = None


@dataclass
class _ConsumerCounters:
    completed_frames: int = 0
    failed_frames: int = 0
    first_completed_ts: float | None = None
    last_completed_ts: float | None = None
    last_source_frame_id: int | None = None
    last_source_frame_ts: float | None = None
    total_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    latency_samples_ms: list[float] = field(default_factory=list, repr=False)

    def snapshot(self) -> dict[str, Any]:
        payload = {
            "completed_frames": self.completed_frames,
            "failed_frames": self.failed_frames,
            "first_completed_ts": self.first_completed_ts,
            "last_completed_ts": self.last_completed_ts,
            "last_source_frame_id": self.last_source_frame_id,
            "last_source_frame_ts": self.last_source_frame_ts,
        }
        if self.completed_frames:
            payload["mean_latency_ms"] = round(
                self.total_latency_ms / self.completed_frames,
                3,
            )
        else:
            payload["mean_latency_ms"] = None
        elapsed = (
            self.last_completed_ts - self.first_completed_ts
            if self.first_completed_ts is not None and self.last_completed_ts is not None
            else 0.0
        )
        payload["effective_fps"] = round((self.completed_frames - 1) / elapsed, 3) if elapsed > 0 else None
        payload["total_latency_ms"] = round(self.total_latency_ms, 3)
        payload["max_latency_ms"] = round(self.max_latency_ms, 3)
        payload["p50_latency_ms"] = _percentile(self.latency_samples_ms, 0.50)
        payload["p95_latency_ms"] = _percentile(self.latency_samples_ms, 0.95)
        payload["latency_sample_window"] = len(self.latency_samples_ms)
        return payload


class BrokerVisionRuntime:
    """Own one canonical capture producer and independent consumer workers."""

    def __init__(
        self,
        *,
        frame_source: FrameSource,
        capture_fps: float,
        consumers: tuple[VisionConsumerSpec, ...],
        health_callback: HealthCallback | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if capture_fps <= 0:
            raise ValueError("capture_fps must be positive")
        if not consumers:
            raise ValueError("at least one vision consumer is required")
        names = [consumer.name for consumer in consumers]
        if len(names) != len(set(names)):
            raise ValueError("vision consumer names must be unique")
        if any(consumer.idle_after_s < 0 for consumer in consumers):
            raise ValueError("consumer idle_after_s cannot be negative")

        self.frame_source = frame_source
        self.capture_fps = float(capture_fps)
        self.consumers = consumers
        self.health_callback = health_callback
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self.broker = FrameBroker()
        self._subscriptions = {
            consumer.name: self.broker.subscribe(
                consumer.name,
                mode=consumer.mode,
                capacity=consumer.capacity,
            )
            for consumer in consumers
        }
        self._stop = threading.Event()
        self._producer_ready = threading.Event()
        self._consumer_ready = {consumer.name: threading.Event() for consumer in consumers}
        self._lock = threading.RLock()
        self._counters = {consumer.name: _ConsumerCounters() for consumer in consumers}
        self._failure: BaseException | None = None
        self._started = False
        self._closed = False
        self._capture_attempts = 0
        self._published_frames = 0
        self._empty_captures = 0
        self._first_published_ts: float | None = None
        self._last_published_ts: float | None = None
        self._producer = threading.Thread(
            target=self._run_producer,
            name="vision-frame-producer",
            daemon=True,
        )
        self._workers = {
            consumer.name: threading.Thread(
                target=self._run_consumer,
                args=(consumer, self._subscriptions[consumer.name]),
                name=f"vision-consumer-{consumer.name}",
                daemon=True,
            )
            for consumer in consumers
        }

    @property
    def failure(self) -> BaseException | None:
        with self._lock:
            return self._failure

    def start(self, *, timeout_s: float = 20.0) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("broker vision runtime already started")
            self._started = True
        for worker in self._workers.values():
            worker.start()
        deadline = self.monotonic() + timeout_s
        for ready in self._consumer_ready.values():
            remaining = max(0.0, deadline - self.monotonic())
            if not ready.wait(remaining):
                self.close()
                raise RuntimeError("timed out starting broker vision runtime")
            if self.failure is not None:
                failure = self.failure
                self.close()
                raise RuntimeError("failed to start broker vision runtime") from failure
        self._producer.start()
        remaining = max(0.0, deadline - self.monotonic())
        if not self._producer_ready.wait(remaining):
            self.close()
            raise RuntimeError("timed out starting broker vision runtime")
        if self.failure is not None:
            failure = self.failure
            self.close()
            raise RuntimeError("failed to start broker vision runtime") from failure
        self._health("runtime_ready", {"capture_fps": self.capture_fps})

    def close(self, *, timeout_s: float = 20.0) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                return self.snapshot()
            self._closed = True
        self._stop.set()
        self.broker.close()
        if self._producer.is_alive():
            self._producer.join(timeout=timeout_s)
        deadline = self.monotonic() + timeout_s
        for worker in self._workers.values():
            if not worker.is_alive():
                continue
            worker.join(timeout=max(0.0, deadline - self.monotonic()))
        alive = [name for name, worker in self._workers.items() if worker.is_alive()]
        if self._producer.is_alive() or alive:
            self._health(
                "close_timeout",
                {"producer_alive": self._producer.is_alive(), "consumer_threads": alive},
            )
        snapshot = self.snapshot()
        self._health("runtime_closed", snapshot)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            elapsed = (
                self._last_published_ts - self._first_published_ts
                if self._first_published_ts is not None and self._last_published_ts is not None
                else 0.0
            )
            capture = {
                "configured_fps": self.capture_fps,
                "capture_attempts": self._capture_attempts,
                "published_frames": self._published_frames,
                "empty_captures": self._empty_captures,
                "first_published_ts": self._first_published_ts,
                "last_published_ts": self._last_published_ts,
                "effective_fps": (
                    round((self._published_frames - 1) / elapsed, 3)
                    if elapsed > 0
                    else None
                ),
                "failed": self._failure is not None,
                "failure": repr(self._failure) if self._failure is not None else None,
            }
            counters = {
                name: counter.snapshot()
                for name, counter in self._counters.items()
            }
        broker = self.broker.snapshot()
        subscriptions = broker["subscriptions"]
        return {
            "capture": capture,
            "consumers": {
                name: {**subscriptions[name], **counters[name]}
                for name in counters
            },
        }

    def _run_producer(self) -> None:
        self._producer_ready.set()
        period = 1.0 / self.capture_fps
        deadline = self.monotonic()
        frame_index = 0
        try:
            while not self._stop.is_set():
                now = self.monotonic()
                if now < deadline:
                    self._stop.wait(deadline - now)
                    continue
                with self._lock:
                    self._capture_attempts += 1
                frame = self.frame_source()
                if frame is None:
                    with self._lock:
                        self._empty_captures += 1
                else:
                    frame_ts = self.wall_clock()
                    packet = FramePacket(
                        frame_index=frame_index,
                        frame_ts=frame_ts,
                        frame_bgr=frame.copy(),
                    )
                    self.broker.publish(packet)
                    with self._lock:
                        self._published_frames += 1
                        if self._first_published_ts is None:
                            self._first_published_ts = frame_ts
                        self._last_published_ts = frame_ts
                    frame_index += 1
                deadline += period
                now = self.monotonic()
                while deadline <= now:
                    deadline += period
        except BaseException as exc:  # noqa: BLE001
            self._fail("producer_failed", exc, {})
        finally:
            self.broker.close()

    def _run_consumer(
        self,
        spec: VisionConsumerSpec,
        subscription: FrameSubscription,
    ) -> None:
        try:
            if spec.start_callback is not None:
                spec.start_callback()
            self._consumer_ready[spec.name].set()
            while True:
                packet = subscription.get(timeout=0.1)
                if packet is None:
                    if subscription.drained:
                        return
                    continue
                started = self.monotonic()
                spec.callback(packet)
                completed = self.monotonic()
                completed_wall = self.wall_clock()
                latency_ms = (completed - started) * 1000.0
                with self._lock:
                    counter = self._counters[spec.name]
                    counter.completed_frames += 1
                    if counter.first_completed_ts is None:
                        counter.first_completed_ts = completed_wall
                    counter.last_completed_ts = completed_wall
                    counter.last_source_frame_id = packet.frame_index
                    counter.last_source_frame_ts = packet.frame_ts
                    counter.total_latency_ms += latency_ms
                    counter.max_latency_ms = max(counter.max_latency_ms, latency_ms)
                    counter.latency_samples_ms.append(latency_ms)
                    if len(counter.latency_samples_ms) > 512:
                        del counter.latency_samples_ms[0]
                if spec.idle_after_s > 0 and not self._stop.is_set():
                    self._stop.wait(spec.idle_after_s)
        except BaseException as exc:  # noqa: BLE001
            with self._lock:
                self._counters[spec.name].failed_frames += 1
            self._consumer_ready[spec.name].set()
            self._fail("consumer_failed", exc, {"consumer": spec.name})

    def _fail(self, event: str, exc: BaseException, data: Mapping[str, Any]) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = exc
        self._health(event, {**dict(data), "error": repr(exc)})
        self._stop.set()
        self.broker.close()

    def _health(self, event: str, data: Mapping[str, Any]) -> None:
        if self.health_callback is not None:
            self.health_callback(event, data)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return round(ordered[index], 3)
