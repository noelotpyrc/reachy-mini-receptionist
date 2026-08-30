"""Low-overhead source liveness reporting for supervised live runs."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)


class RuntimeLiveness:
    """Track source activity without coupling health to artifact recording."""

    def __init__(
        self,
        *,
        run_id: str,
        audio_expected: bool,
        video_expected: bool,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.run_id = run_id
        self.audio_expected = audio_expected
        self.video_expected = video_expected
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        now = monotonic()
        self._started_monotonic = now
        self._phase = "starting"
        self._phase_monotonic = now
        self._ready_monotonic: float | None = None
        self._event_loop_monotonic: float | None = None
        self._audio_monotonic: float | None = None
        self._video_monotonic: float | None = None
        self._audio_sequence = 0
        self._video_sequence = 0
        self._fault: str | None = None

    def set_phase(self, phase: str) -> None:
        now = self._monotonic()
        with self._lock:
            self._phase = phase
            self._phase_monotonic = now
            if phase == "ready" and self._ready_monotonic is None:
                self._ready_monotonic = now

    def pulse_event_loop(self) -> None:
        with self._lock:
            self._event_loop_monotonic = self._monotonic()

    def audio_frame(self) -> None:
        with self._lock:
            self._audio_monotonic = self._monotonic()
            self._audio_sequence += 1

    def video_frame(self) -> None:
        with self._lock:
            self._video_monotonic = self._monotonic()
            self._video_sequence += 1

    def set_fault(self, fault: str) -> None:
        with self._lock:
            self._fault = fault

    def snapshot(self) -> dict[str, Any]:
        now = self._monotonic()
        with self._lock:
            return {
                "schema_version": 1,
                "run_id": self.run_id,
                "pid": os.getpid(),
                "updated_at": self._wall_clock(),
                "updated_monotonic": now,
                "started_monotonic": self._started_monotonic,
                "phase": self._phase,
                "phase_monotonic": self._phase_monotonic,
                "ready_monotonic": self._ready_monotonic,
                "event_loop_monotonic": self._event_loop_monotonic,
                "event_loop_age_s": _age(now, self._event_loop_monotonic),
                "audio": {
                    "expected": self.audio_expected,
                    "sequence": self._audio_sequence,
                    "last_frame_monotonic": self._audio_monotonic,
                    "age_s": _age(now, self._audio_monotonic),
                },
                "video": {
                    "expected": self.video_expected,
                    "sequence": self._video_sequence,
                    "last_frame_monotonic": self._video_monotonic,
                    "age_s": _age(now, self._video_monotonic),
                },
                "fault": self._fault,
            }


class HeartbeatWriter:
    """Publish liveness snapshots from a small daemon thread."""

    def __init__(
        self,
        path: Path,
        liveness: RuntimeLiveness,
        *,
        interval_s: float = 1.0,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.path = path
        self.liveness = liveness
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._write_error_count = 0
        self._consecutive_write_errors = 0
        self._last_write_error: str | None = None
        self._last_write_error_at: float | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._write()
        self._thread = threading.Thread(
            target=self._run,
            name="official-runtime-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.interval_s * 2))
        self._write()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._write()

    def _write(self) -> bool:
        snapshot = self.liveness.snapshot()
        snapshot["heartbeat_writer"] = self._writer_status()
        payload = json.dumps(snapshot, indent=2) + "\n"
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            self._record_write_error(exc)
            return False
        with self._status_lock:
            self._consecutive_write_errors = 0
        return True

    def _writer_status(self) -> dict[str, Any]:
        with self._status_lock:
            return {
                "write_error_count": self._write_error_count,
                "last_write_error": self._last_write_error,
                "last_write_error_at": self._last_write_error_at,
            }

    def _record_write_error(self, exc: OSError) -> None:
        with self._status_lock:
            self._write_error_count += 1
            self._consecutive_write_errors += 1
            error_count = self._write_error_count
            consecutive = self._consecutive_write_errors
            self._last_write_error = repr(exc)
            self._last_write_error_at = time.time()
        if consecutive == 1 or consecutive % 10 == 0:
            logger.warning(
                "heartbeat write failed (%d total, %d consecutive): %r",
                error_count,
                consecutive,
                exc,
            )


async def pulse_event_loop(liveness: RuntimeLiveness, *, interval_s: float = 0.5) -> None:
    """Pulse only when the asyncio event loop is able to schedule work."""

    import asyncio

    while True:
        liveness.pulse_event_loop()
        await asyncio.sleep(interval_s)


def _age(now: float, timestamp: float | None) -> float | None:
    return None if timestamp is None else max(0.0, now - timestamp)
