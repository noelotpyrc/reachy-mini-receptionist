"""Movement primitives for the official-runtime reception UX."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import numpy as np

from .events import EventSink, RuntimeEvent


try:
    from reachy_mini.motion.move import Move
except Exception:  # noqa: BLE001
    class Move:  # type: ignore[no-redef]
        """Fallback base for tests when the SDK move class is unavailable."""


class AntennaPulseMove(Move):  # type: ignore[misc]
    """Small antenna-only pulse used for deterministic reception cues."""

    def __init__(
        self,
        *,
        high: tuple[float, float] = (0.35, -0.35),
        low: tuple[float, float] = (-0.17, 0.17),
        duration: float = 1.0,
    ) -> None:
        self.high = np.array(high, dtype=np.float64)
        self.low = np.array(low, dtype=np.float64)
        self._duration = duration

    @property
    def duration(self) -> float:
        return self._duration

    def evaluate(self, t: float):  # type: ignore[no-untyped-def]
        phase = min(max(t / max(self._duration, 0.001), 0.0), 1.0)
        if phase < 0.5:
            local_t = phase * 2.0
            antennas = self.low * (1.0 - local_t) + self.high * local_t
        else:
            local_t = (phase - 0.5) * 2.0
            antennas = self.high * (1.0 - local_t) + self.low * local_t
        return None, antennas, None


class PlaybackMovementGate:
    """Track assistant playback and suppress/resume nonessential movement.

    The gate can be used both as a runtime observer and as an event sink. It
    does not know robot hardware details; instead it calls an optional callback
    and best-effort movement-manager methods when available.
    """

    CLEAR_EVENT_KINDS = {
        "response.output_audio.done",
        "response.done",
        "response.cancelled",
        "response.failed",
        "realtime.response.output_audio.done",
        "realtime.response.done",
        "realtime.response.cancelled",
        "realtime.response.failed",
        "livekit.handler.stopped",
        "runtime.stopped",
    }

    def __init__(
        self,
        *,
        movement_manager: Any | None = None,
        on_change: Callable[[bool, str], None] | None = None,
        suppress_idle_motion: bool = True,
    ) -> None:
        self.movement_manager = movement_manager
        self.on_change = on_change
        self.suppress_idle_motion = suppress_idle_motion
        self.playback_active = False

    def record_output_audio_frame(
        self,
        sample_rate: int,
        audio: np.ndarray,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._set_active(True, "assistant_audio")

    def record_realtime_event(self, kind: str, **data: Any) -> None:
        if kind in self.CLEAR_EVENT_KINDS:
            self.clear(kind)

    def emit(self, event: RuntimeEvent) -> None:
        event_type = event.data.get("event_type")
        if event.kind in self.CLEAR_EVENT_KINDS:
            self.clear(event.kind)
        elif isinstance(event_type, str) and event_type in self.CLEAR_EVENT_KINDS:
            self.clear(event_type)

    def clear(self, reason: str = "clear") -> None:
        self._set_active(False, reason)

    def _set_active(self, active: bool, reason: str) -> None:
        if self.playback_active == active:
            return
        self.playback_active = active
        self._apply_to_movement_manager(active)
        if self.on_change is not None:
            self.on_change(active, reason)

    def _apply_to_movement_manager(self, active: bool) -> None:
        manager = self.movement_manager
        if manager is None:
            return
        set_playback_active = getattr(manager, "set_playback_active", None)
        if callable(set_playback_active):
            set_playback_active(active)
        if self.suppress_idle_motion:
            set_idle_breathing_enabled = getattr(manager, "set_idle_breathing_enabled", None)
            if callable(set_idle_breathing_enabled):
                set_idle_breathing_enabled(not active)


class AntennaCueController:
    """Own one cancellable antenna-only cue loop.

    The controller is deliberately small and backend-agnostic. Policies decide
    when a cue should start or stop; this object only performs the movement and
    guarantees a rest command on stop/cancel.
    """

    def __init__(
        self,
        *,
        set_antennas: Callable[[tuple[float, float]], Any],
        event_sink: EventSink | None = None,
        high: tuple[float, float] = (18.0, 18.0),
        rest: tuple[float, float] = (-15.0, -15.0),
        high_s: float = 0.22,
        rest_s: float = 0.38,
    ) -> None:
        self.set_antennas = set_antennas
        self.event_sink = event_sink
        self.high = high
        self.rest = rest
        self.high_s = max(0.01, float(high_s))
        self.rest_s = max(0.01, float(rest_s))
        self._task: asyncio.Task[None] | None = None
        self._cue = "idle"

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, *, cue: str = "thinking") -> bool:
        if self.active:
            return True
        self._cue = cue
        self._task = asyncio.create_task(self._run_loop(cue), name=f"antenna-cue-{cue}")
        self._emit("started", cue=cue)
        return True

    async def stop(self, *, reason: str = "stop") -> bool:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._set(self.rest, cue=self._cue, phase="rest", reason=reason)
        self._emit("stopped", cue=self._cue, reason=reason)
        return True

    async def _run_loop(self, cue: str) -> None:
        try:
            while True:
                await self._set(self.high, cue=cue, phase="high")
                await asyncio.sleep(self.high_s)
                await self._set(self.rest, cue=cue, phase="rest")
                await asyncio.sleep(self.rest_s)
        except asyncio.CancelledError:
            raise
        finally:
            await self._set(self.rest, cue=cue, phase="rest", reason="cancel")

    async def _set(
        self,
        antennas: tuple[float, float],
        *,
        cue: str,
        phase: str,
        reason: str | None = None,
    ) -> None:
        result = self.set_antennas(antennas)
        if inspect.isawaitable(result):
            await result
        self._emit("position", cue=cue, phase=phase, antennas=antennas, reason=reason)

    def _emit(self, event_phase: str, **data: Any) -> None:
        if self.event_sink is None:
            return
        self.event_sink.emit(
            RuntimeEvent(
                kind="runtime.antenna_cue",
                source="official_runtime.moves",
                data={"event_phase": event_phase, **{key: value for key, value in data.items() if value is not None}},
            )
        )


def queue_antenna_pulse(context: Any, *, movement_manager: Any | None = None) -> bool:
    """Capability helper that queues an antenna pulse on a movement manager."""

    state = getattr(context, "state", {})
    manager = movement_manager or getattr(context, "movement_manager", None) or state.get("movement_manager")
    if manager is None:
        return False
    queue_move = getattr(manager, "queue_move", None)
    if not callable(queue_move):
        return False
    queue_move(AntennaPulseMove())
    return True
