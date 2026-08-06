"""Single-owner background publisher for live Rerun vision events."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .live_detection import DetectionLayerObservation, FramePacket
from .rerun_vision import RerunVisionRenderer
from .vision_observation import VisionObservation


RERUN_MODES = ("off", "grpc", "file", "grpc+file")


@dataclass(frozen=True)
class LiveRerunStats:
    submitted_events: int
    rendered_events: int
    dropped_events: int
    queue_high_water: int


RendererFactory = Callable[..., RerunVisionRenderer]
HealthCallback = Callable[[str, Mapping[str, Any]], None]


class LiveRerunPublisher:
    """Serialize all Rerun SDK calls on one bounded background worker."""

    def __init__(
        self,
        *,
        mode: str,
        recording_id: str,
        grpc_url: str | None = None,
        save_path: str | Path | None = None,
        jpeg_quality: int = 80,
        image_fps: float = 5.0,
        queue_size: int = 3,
        health_callback: HealthCallback | None = None,
        renderer_factory: RendererFactory = RerunVisionRenderer,
    ) -> None:
        if mode not in RERUN_MODES or mode == "off":
            raise ValueError(f"live Rerun publisher requires mode in {RERUN_MODES[1:]}")
        if "grpc" in mode and not grpc_url:
            raise ValueError(f"Rerun mode {mode!r} requires grpc_url")
        if "file" in mode and save_path is None:
            raise ValueError(f"Rerun mode {mode!r} requires save_path")
        if queue_size <= 0:
            raise ValueError("Rerun queue_size must be positive")
        if image_fps <= 0:
            raise ValueError("Rerun image_fps must be positive")
        self.mode = mode
        self.recording_id = recording_id
        self.grpc_url = grpc_url if "grpc" in mode else None
        self.save_path = Path(save_path) if "file" in mode and save_path is not None else None
        self.jpeg_quality = jpeg_quality
        self.image_fps = image_fps
        self.health_callback = health_callback
        self.renderer_factory = renderer_factory
        self.queue: queue.Queue[tuple[str, Any] | None] = queue.Queue(maxsize=queue_size)
        self.thread = threading.Thread(target=self._run, name="live-rerun-publisher", daemon=True)
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._startup_error: BaseException | None = None
        self._submitted = 0
        self._rendered = 0
        self._dropped = 0
        self._queue_high_water = 0
        self._closed = False
        self._last_frame_ts: float | None = None

    def start(self, *, timeout_s: float = 15.0) -> None:
        self.thread.start()
        if not self._ready.wait(timeout_s):
            raise RuntimeError("timed out starting live Rerun publisher")
        if self._startup_error is not None:
            raise RuntimeError("failed to start live Rerun publisher") from self._startup_error

    def submit_frame(self, packet: FramePacket) -> None:
        with self._lock:
            interval = 1.0 / self.image_fps
            if self._last_frame_ts is not None and packet.frame_ts - self._last_frame_ts < interval - 1e-6:
                return
            self._last_frame_ts = packet.frame_ts
        self._submit(("frame", packet))

    def submit_visitor_observation(self, observation: VisionObservation) -> None:
        self._submit(("visitor", observation))

    def submit_detection_layer(self, observation: DetectionLayerObservation) -> None:
        self._submit(("layer", observation))

    def close(self) -> LiveRerunStats:
        with self._lock:
            if self._closed:
                return self.stats()
            self._closed = True
        self._put_stop()
        self.thread.join()
        return self.stats()

    def stats(self) -> LiveRerunStats:
        with self._lock:
            return LiveRerunStats(
                submitted_events=self._submitted,
                rendered_events=self._rendered,
                dropped_events=self._dropped,
                queue_high_water=self._queue_high_water,
            )

    def _submit(self, event: tuple[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
            self._submitted += 1
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                self._dropped += 1
            self.queue.put_nowait(event)
        with self._lock:
            self._queue_high_water = max(self._queue_high_water, self.queue.qsize())

    def _put_stop(self) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(None)

    def _run(self) -> None:
        renderer: RerunVisionRenderer | None = None
        try:
            renderer = self.renderer_factory(
                save_path=self.save_path,
                grpc_url=self.grpc_url,
                recording_id=self.recording_id,
                jpeg_quality=self.jpeg_quality,
                mode="live",
            )
        except BaseException as exc:  # noqa: BLE001
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        self._health("worker_ready", {"mode": self.mode})
        try:
            while True:
                event = self.queue.get()
                if event is None:
                    return
                kind, payload = event
                if kind == "frame":
                    renderer.render_frame(
                        frame_index=payload.frame_index,
                        frame_ts=payload.frame_ts,
                        image_bgr=payload.frame_bgr,
                    )
                elif kind == "visitor":
                    renderer.render(payload)
                else:
                    renderer.render_detection_layer(payload)
                with self._lock:
                    self._rendered += 1
        except Exception as exc:  # noqa: BLE001
            self._health("worker_failed", {"error": repr(exc)})
        finally:
            renderer.close()
            self._health("worker_closed", asdict_stats(self.stats()))

    def _health(self, event: str, data: Mapping[str, Any]) -> None:
        if self.health_callback is not None:
            self.health_callback(event, data)


def asdict_stats(stats: LiveRerunStats) -> dict[str, int]:
    return {
        "submitted_events": stats.submitted_events,
        "rendered_events": stats.rendered_events,
        "dropped_events": stats.dropped_events,
        "queue_high_water": stats.queue_high_water,
    }
