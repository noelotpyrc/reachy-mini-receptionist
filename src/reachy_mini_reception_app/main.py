"""Official native lifecycle with control-only communication to m1max."""

from __future__ import annotations

import asyncio
import signal
import threading
import time
from typing import Any

from reachy_mini import ReachyMini
from reachy_mini.apps.app import ReachyMiniApp

from .client import control_session
from .settings import load_settings


class ReceptionApp(ReachyMiniApp):
    custom_app_url = "http://0.0.0.0:7860/"

    def __init__(self, running_on_wireless: bool = False) -> None:
        # Validate credentials and TLS before the framework initializes robot IO.
        self.config = load_settings()
        super().__init__(running_on_wireless=running_on_wireless)
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {"phase": "stopped", "config_id": self.config.config_id,
                                       "robot_id": self.config.robot_id}
        self._status_at: float | None = None
        if self.settings_app is not None:
            self.settings_app.get("/api/reception/status")(self.status)

    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return {**self._status, "control_age_s": (
                round(time.monotonic() - self._status_at, 2) if self._status_at is not None else None
            )}

    def _on_status(self, status: dict[str, Any]) -> None:
        with self._status_lock:
            self._status.update(status)
            self._status_at = time.monotonic()

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        # Use the framework lifecycle; never call no_media/release_media, start
        # local playback/capture, or launch the old runner through OPS.
        try:
            asyncio.run(control_session(
                url=self.config.url, token=self.config.token,
                config_id=self.config.config_id, robot_id=self.config.robot_id,
                tls=self.config.tls,
                stop_event=stop_event, on_status=self._on_status,
            ))
            if self.status().get("phase") == "faulted":
                raise RuntimeError("Reception service ended with a fault; operator review required")
        finally:
            stop_event.set()


def main() -> None:
    app = ReceptionApp()
    signal.signal(signal.SIGINT, lambda *_: app.stop())
    signal.signal(signal.SIGTERM, lambda *_: app.stop())
    app.wrapped_run()


if __name__ == "__main__":
    main()
