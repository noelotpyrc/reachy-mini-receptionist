"""Official-style realtime audio stream runtime.

The official app's core audio shape is:

    audio source -> handler.receive(frame)
    handler.emit() -> audio sink

This module keeps that shape but avoids importing the official app or robot
runtime yet. It is meant for offline tests and adapter spikes first.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeAlias

import numpy as np
from numpy.typing import NDArray

from .events import EventSink, InMemoryEventSink, RuntimeEvent


AudioFrame: TypeAlias = tuple[int, NDArray[np.int16]]
HandlerOutput: TypeAlias = AudioFrame | dict[str, Any] | None


class RealtimeHandler(Protocol):
    """Small subset of the official ConversationHandler contract."""

    async def start_up(self) -> None:
        """Start handler resources."""

    async def shutdown(self) -> None:
        """Release handler resources."""

    async def receive(self, frame: AudioFrame) -> None:
        """Receive one input audio frame."""

    async def emit(self) -> HandlerOutput:
        """Emit the next audio frame or metadata item."""


class AudioSource(Protocol):
    """Produces input audio frames."""

    async def read(self) -> AudioFrame | None:
        """Return a frame, or None when the source is exhausted."""


class AudioSink(Protocol):
    """Consumes output audio frames."""

    async def write(self, frame: AudioFrame) -> None:
        """Write one output audio frame."""


class RuntimeObserver(Protocol):
    """Optional side observer for data-harness taps and runtime gates."""

    def should_forward_audio(self) -> bool:
        """Return whether input audio should be forwarded to the backend."""

    def record_input_audio_frame(self, sample_rate: int, audio: NDArray[Any], *, forwarded: bool = True) -> None:
        """Record one captured input audio frame."""

    def record_output_audio_frame(
        self,
        sample_rate: int,
        audio: NDArray[Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record one backend output audio frame."""

    def record_output_message(self, message: dict[str, Any]) -> None:
        """Record one non-audio backend message."""


class CompositeRuntimeObserver:
    """Fan out runtime observer hooks and combine audio gate decisions."""

    def __init__(self, *observers: object) -> None:
        self.observers = [observer for observer in observers if observer is not None]

    def should_forward_audio(self) -> bool:
        for observer in self.observers:
            fn = getattr(observer, "should_forward_audio", None)
            if callable(fn) and not bool(fn()):
                return False
        return True

    def record_input_audio_frame(self, sample_rate: int, audio: NDArray[Any], *, forwarded: bool = True) -> None:
        self._call_all("record_input_audio_frame", sample_rate, audio, forwarded=forwarded)

    def record_output_audio_frame(
        self,
        sample_rate: int,
        audio: NDArray[Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._call_all("record_output_audio_frame", sample_rate, audio, metadata=metadata)

    def record_output_message(self, message: dict[str, Any]) -> None:
        self._call_all("record_output_message", message)

    def _call_all(self, name: str, *args: Any, **kwargs: Any) -> None:
        errors: list[str] = []
        for observer in self.observers:
            fn = getattr(observer, name, None)
            if not callable(fn):
                continue
            try:
                fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(observer).__name__}.{name}: {exc!r}")
        if errors:
            raise RuntimeError("; ".join(errors))


class OfficialStyleStreamRuntime:
    """Pump audio through an official-style realtime handler."""

    def __init__(
        self,
        *,
        handler: RealtimeHandler,
        audio_source: AudioSource,
        audio_sink: AudioSink,
        event_sink: EventSink | None = None,
        runtime_observer: RuntimeObserver | None = None,
        on_ready: Callable[[], Awaitable[None]] | None = None,
        emit_timeout: float = 0.05,
        drain_idle_polls: int = 3,
        playback_done_idle_polls: int = 5,
    ) -> None:
        self.handler = handler
        self.audio_source = audio_source
        self.audio_sink = audio_sink
        self.event_sink = event_sink or InMemoryEventSink()
        self.runtime_observer = runtime_observer
        self.on_ready = on_ready
        self.emit_timeout = emit_timeout
        self.drain_idle_polls = drain_idle_polls
        self.playback_done_idle_polls = max(1, playback_done_idle_polls)
        self._stop_event = asyncio.Event()
        self._input_done = asyncio.Event()
        self._assistant_audio_active = False

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        self._stop_event.clear()
        self._input_done.clear()
        self._assistant_audio_active = False
        self._emit("runtime.started")
        output_task: asyncio.Task[None] | None = None
        handler_started = False
        try:
            await self.handler.start_up()
            handler_started = True
            self._emit("runtime.handler_started")
            if self.on_ready is not None:
                await self.on_ready()
            self._emit("runtime.input_starting")
            output_task = asyncio.create_task(self._output_loop(), name="runtime-output")
            await self._input_loop()
            self._input_done.set()
            await output_task
            await self._drain_audio_sink()
        except Exception as exc:
            self._emit("runtime.failed", error=repr(exc))
            self.stop()
            if output_task is not None and not output_task.done():
                output_task.cancel()
                try:
                    await output_task
                except asyncio.CancelledError:
                    pass
            raise
        finally:
            self.stop()
            await self._close_audio_sink()
            if handler_started:
                await self.handler.shutdown()
            self._emit("runtime.stopped")

    async def _input_loop(self) -> None:
        frames = 0
        samples = 0
        forwarded_frames = 0
        forwarded_samples = 0
        sample_rate = 0
        while not self._stop_event.is_set():
            frame = await self.audio_source.read()
            if frame is None:
                break
            forwarded = self._should_forward_audio()
            self._record_input_audio_frame(frame, forwarded=forwarded)
            self._emit("audio.input_frame", forwarded=forwarded, **_frame_metadata(frame))
            if forwarded:
                await self.handler.receive(frame)
                forwarded_frames += 1
                forwarded_samples += _mono_sample_count(frame[1])
            sample_rate, audio = frame
            frames += 1
            samples += int(audio.shape[0]) if audio.ndim else int(audio.size)
        duration_s = samples / float(sample_rate) if sample_rate else 0.0
        self._emit(
            "audio.input_done",
            frames=frames,
            samples=samples,
            forwarded_frames=forwarded_frames,
            forwarded_samples=forwarded_samples,
            sample_rate=sample_rate,
            duration_s=duration_s,
        )

    async def _output_loop(self) -> None:
        idle_after_input_done = 0
        idle_after_audio = 0
        while not self._stop_event.is_set():
            try:
                item = await asyncio.wait_for(
                    self.handler.emit(),
                    timeout=self.emit_timeout,
                )
            except asyncio.TimeoutError:
                if await self._mark_audio_idle(idle_after_audio):
                    idle_after_audio += 1
                else:
                    idle_after_audio = 0
                if self._input_done.is_set():
                    idle_after_input_done += 1
                    if idle_after_input_done >= self.drain_idle_polls:
                        break
                continue

            if item is None:
                if await self._mark_audio_idle(idle_after_audio):
                    idle_after_audio += 1
                else:
                    idle_after_audio = 0
                if self._input_done.is_set():
                    idle_after_input_done += 1
                    if idle_after_input_done >= self.drain_idle_polls:
                        break
                await asyncio.sleep(0)
                continue

            idle_after_input_done = 0
            if _is_audio_frame(item):
                idle_after_audio = 0
                frame = _audio_frame_from_output(item)
                metadata = _audio_metadata_from_output(item)
                if not self._assistant_audio_active:
                    self._assistant_audio_active = True
                    self._emit("assistant.audio.started", metadata=metadata, **_frame_metadata(frame))
                self._record_output_audio_frame(frame, metadata=metadata)
                self._emit("audio.output_frame", metadata=metadata, **_frame_metadata(frame))
                await self.audio_sink.write(frame)
                continue

            if isinstance(item, dict):
                self._record_output_message(item)
                if _is_final_user_transcript_message(item):
                    await self._finish_assistant_audio(reason="user_transcript")
                    self._emit(
                        "assistant.thinking.started",
                        text=_message_text(item),
                        trigger="handler.output",
                    )
            self._emit("handler.output", item=item)
        await self._finish_assistant_audio(reason="output_loop_finished")

    async def _mark_audio_idle(self, idle_polls: int) -> bool:
        if not self._assistant_audio_active:
            return False
        if idle_polls + 1 < self.playback_done_idle_polls:
            return True
        await self._finish_assistant_audio(reason="output_idle")
        return False

    async def _finish_assistant_audio(self, *, reason: str) -> None:
        if not self._assistant_audio_active:
            return
        await self._drain_audio_sink()
        self._assistant_audio_active = False
        self._emit("assistant.audio.done", reason=reason)

    def _emit(self, kind: str, **data: Any) -> None:
        self.event_sink.emit(
            RuntimeEvent(kind=kind, source="official_runtime.stream", data=data)
        )

    def _should_forward_audio(self) -> bool:
        observer = self.runtime_observer
        fn = getattr(observer, "should_forward_audio", None)
        if not callable(fn):
            return True
        try:
            return bool(fn())
        except Exception as exc:  # noqa: BLE001
            self._emit("runtime.observer_failed", method="should_forward_audio", error=repr(exc))
            return True

    def _record_input_audio_frame(self, frame: AudioFrame, *, forwarded: bool) -> None:
        observer = self.runtime_observer
        fn = getattr(observer, "record_input_audio_frame", None)
        if not callable(fn):
            return
        sample_rate, audio = frame
        try:
            fn(sample_rate, audio, forwarded=forwarded)
        except Exception as exc:  # noqa: BLE001
            self._emit("runtime.observer_failed", method="record_input_audio_frame", error=repr(exc))

    def _record_output_audio_frame(self, frame: AudioFrame, *, metadata: dict[str, Any]) -> None:
        observer = self.runtime_observer
        fn = getattr(observer, "record_output_audio_frame", None)
        if not callable(fn):
            return
        sample_rate, audio = frame
        try:
            fn(sample_rate, audio, metadata=metadata)
        except Exception as exc:  # noqa: BLE001
            self._emit("runtime.observer_failed", method="record_output_audio_frame", error=repr(exc))

    def _record_output_message(self, message: dict[str, Any]) -> None:
        observer = self.runtime_observer
        fn = getattr(observer, "record_output_message", None)
        if not callable(fn):
            return
        try:
            fn(message)
        except Exception as exc:  # noqa: BLE001
            self._emit("runtime.observer_failed", method="record_output_message", error=repr(exc))

    async def _drain_audio_sink(self) -> None:
        drain = getattr(self.audio_sink, "drain", None)
        if not callable(drain):
            return
        result = drain()
        if inspect.isawaitable(result):
            await result

    async def _close_audio_sink(self) -> None:
        close = getattr(self.audio_sink, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result


def _is_audio_frame(item: object) -> bool:
    if not isinstance(item, tuple) or len(item) < 2:
        return False
    sample_rate, audio = item[:2]
    return isinstance(sample_rate, int) and isinstance(audio, np.ndarray)


def _audio_frame_from_output(item: object) -> AudioFrame:
    if not _is_audio_frame(item):
        raise TypeError(f"not an audio frame: {item!r}")
    sample_rate, audio = item[:2]  # type: ignore[index]
    return sample_rate, audio


def _audio_metadata_from_output(item: object) -> dict[str, Any]:
    if isinstance(item, tuple) and len(item) >= 3 and isinstance(item[2], dict):
        return dict(item[2])
    return {}


def _frame_metadata(frame: AudioFrame) -> dict[str, Any]:
    sample_rate, audio = frame
    samples = _mono_sample_count(audio)
    duration_s = samples / float(sample_rate) if sample_rate else 0.0
    return {
        "sample_rate": sample_rate,
        "samples": samples,
        "duration_s": duration_s,
        "dtype": str(audio.dtype),
    }


def _mono_sample_count(audio: NDArray[np.int16]) -> int:
    if not audio.ndim:
        return int(audio.size)
    if audio.ndim == 1:
        return int(audio.shape[0])
    if audio.ndim == 2 and 1 in audio.shape:
        return int(max(audio.shape))
    return int(audio.shape[0])


def _is_final_user_transcript_message(item: dict[str, Any]) -> bool:
    if item.get("final") is False:
        return False
    role = item.get("role")
    if role not in (None, "", "user", "transcript", "user_transcript"):
        return False
    text = _message_text(item)
    return bool(text)


def _message_text(item: dict[str, Any]) -> str:
    text = item.get("transcript")
    if text is None:
        text = item.get("text")
    if text is None:
        text = item.get("content")
    return text.strip() if isinstance(text, str) else ""
