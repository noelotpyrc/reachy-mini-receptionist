"""LiveKit backend handler boundary for the isolated official-style runtime.

This module intentionally keeps LiveKit SDK details behind a bridge protocol.
The handler can be tested today with a fake bridge, then backed by a real
LiveKit room/agent bridge later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .events import EventSink, InMemoryEventSink, RuntimeEvent
from .stream_runtime import AudioFrame, HandlerOutput, RealtimeHandler


@dataclass(frozen=True, slots=True)
class LiveKitBackendConfig:
    """Configuration needed by a real LiveKit bridge."""

    url: str = ""
    api_key: str = ""
    api_secret: str = ""
    token: str = ""
    room_name: str = "reachy-mini-offline"
    participant_name: str = "reachy-mini"
    agent_name: str = "reachy-mini-receptionist"
    dispatch_agent: bool = True
    input_sample_rate: int = 16_000
    output_sample_rate: int = 24_000
    track_name: str = "reachy-mini-input"
    output_frame_size_ms: int = 20
    input_queue_size_ms: int = 1000
    connect_timeout_s: float = 15.0
    agent_ready_timeout_s: float = 20.0
    suppress_silent_output: bool = False
    silent_output_peak_threshold: int = 4


class LiveKitBridge(Protocol):
    """Minimal bridge contract used by LiveKitRealtimeHandler."""

    async def start(self) -> None:
        """Connect resources and start backend processing."""

    async def stop(self) -> None:
        """Stop backend processing and release resources."""

    async def send_audio(self, frame: AudioFrame) -> None:
        """Send one input audio frame to the backend."""

    async def next_output(self) -> HandlerOutput:
        """Return the next backend output, or None when no output is ready."""


class LiveKitRealtimeHandler(RealtimeHandler):
    """Official-style handler that delegates realtime work to a LiveKit bridge."""

    def __init__(
        self,
        *,
        config: LiveKitBackendConfig | None = None,
        bridge: LiveKitBridge | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        self.config = config or LiveKitBackendConfig()
        self.bridge = bridge
        self.event_sink = event_sink or InMemoryEventSink()
        self.started = False

    async def start_up(self) -> None:
        if self.bridge is None:
            raise RuntimeError(
                "LiveKitRealtimeHandler requires a LiveKitBridge. "
                "The real LiveKit room bridge has not been implemented yet."
            )
        self._emit(
            "livekit.handler.starting",
            room_name=self.config.room_name,
            participant_name=self.config.participant_name,
        )
        await self.bridge.start()
        self.started = True
        self._emit("livekit.handler.started")

    async def shutdown(self) -> None:
        if self.bridge is None:
            return
        try:
            await self.bridge.stop()
        finally:
            self.started = False
            self._emit("livekit.handler.stopped")

    async def receive(self, frame: AudioFrame) -> None:
        self._ensure_started()
        self._emit("livekit.audio.sent", **_frame_metadata(frame))
        await self.bridge.send_audio(frame)  # type: ignore[union-attr]

    async def emit(self) -> HandlerOutput:
        self._ensure_started()
        item = await self.bridge.next_output()  # type: ignore[union-attr]
        if item is None:
            return None
        if _is_audio_frame(item):
            self._emit("livekit.output.audio", **_frame_metadata(item))
        else:
            self._emit("livekit.output.event", item=item)
        return item

    def _ensure_started(self) -> None:
        if not self.started:
            raise RuntimeError("LiveKitRealtimeHandler is not started")

    def _emit(self, kind: str, **data: Any) -> None:
        self.event_sink.emit(RuntimeEvent(kind=kind, source="official_runtime.livekit", data=data))


def _is_audio_frame(item: object) -> bool:
    if not isinstance(item, tuple) or len(item) != 2:
        return False
    sample_rate, audio = item
    return isinstance(sample_rate, int) and isinstance(audio, np.ndarray)


def _frame_metadata(frame: AudioFrame) -> dict[str, Any]:
    sample_rate, audio = frame
    samples = int(audio.shape[0]) if audio.ndim else int(audio.size)
    duration_s = samples / float(sample_rate) if sample_rate else 0.0
    return {
        "sample_rate": sample_rate,
        "samples": samples,
        "duration_s": duration_s,
        "dtype": str(audio.dtype),
    }
