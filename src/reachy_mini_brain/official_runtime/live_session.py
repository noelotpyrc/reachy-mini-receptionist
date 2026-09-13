"""Lifecycle hooks for embedding the live runtime without owning process signals."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .liveness import RuntimeLiveness


class SessionStopped(Exception):
    """A requested stop reached a startup checkpoint before processing began."""


class LiveSessionControl:
    """One session's cross-thread stop flag, health, and cleanup registrations.

    The service owns the flag; callbacks and resources belong to the runtime loop.
    Blocking SDK startup must finish before the session can release ownership.
    """

    def __init__(self, on_ready: Callable[[], None]) -> None:
        self.stop_requested = threading.Event()
        self.on_ready = on_ready
        self.liveness: RuntimeLiveness | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.recorder: Any = None
        self._stop_callbacks: list[Callable[[], None]] = []
        self._cleanup: list[Callable] = []
        self._closing = False
        self._stop_dispatched = False
        self.cleanup_faults: list[str] = []

    def bind(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        self.checkpoint()

    def checkpoint(self) -> None:
        if self.stop_requested.is_set():
            raise SessionStopped()

    def request_stop(self) -> None:
        self.stop_requested.set()
        loop = self.loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._stop_on_loop)

    def _stop_on_loop(self) -> None:
        if self._closing or self._stop_dispatched:
            return
        self._stop_dispatched = True
        if self.liveness is not None:
            self.liveness.set_phase("stopping")
        for callback in self._stop_callbacks:
            try:
                callback()
            except Exception as exc:
                self.cleanup_faults.append(f"Stop callback failed: {exc!r}")
        if self.task is not None and not self.task.done():
            self.task.cancel()

    def on_stop(self, callback: Callable[[], None]) -> None:
        self._stop_callbacks.append(callback)

    def on_cleanup(self, callback: Callable) -> None:
        self._cleanup.append(callback)

    def begin_cleanup(self) -> None:
        self._closing = True

    async def blocking(self, callback: Callable, *args: Any) -> Any:
        """Do not orphan SDK/model initialization if Stop cancels its await."""
        self.checkpoint()
        task = asyncio.create_task(asyncio.to_thread(callback, *args))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
        self.checkpoint()
        return result

    def ready(self) -> None:
        self.checkpoint()
        self.on_ready()

    async def close(self) -> None:
        self.begin_cleanup()
        errors: list[Exception] = []
        for callback in reversed(self._cleanup):
            try:
                result = callback()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                errors.append(exc)
        self._cleanup.clear()
        errors.extend(RuntimeError(message) for message in self.cleanup_faults)
        if errors:
            raise ExceptionGroup("Reception session cleanup failed", errors)


async def cancel_task(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@contextlib.contextmanager
def runtime_lease():
    """Coordinate CLI and service runtimes on this host; never unlink lock files."""
    import fcntl

    path = Path(os.environ.get(
        "RECEPTION_RUNTIME_LOCK_PATH",
        str(Path.home() / ".local/state/reachy-reception/runtime.lock"),
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another reception runtime owns this host") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
