"""Small, non-clinical status snapshots shared with the native control UI."""

from __future__ import annotations

import copy
import threading
from typing import Any


class ReceptionStatus:
    def __init__(self, options: dict[str, Any]) -> None:
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._health: dict[str, Any] = {}
        self._configuration = {
            "profile": options["agent_profile_id"], "tools": options["agent_tools"],
            "vision_policy": options["visitor_trigger_profile"],
            "vision_runtime": options.get("vision_runtime", "broker-v1"),
            "duration_s": options.get("duration"),
            "record_audio": options.get("record_audio", True),
            "record_video": options.get("record_video", False),
            "capture_vision": options.get("capture_vision", True),
        }

    def begin(self, session_id: str) -> None:
        with self._lock:
            self._session_id = session_id
            self._health = {}

    def update(self, health: dict[str, Any] | None, *, fault: str | None = None) -> None:
        if health is None:
            return
        # Never forward raw exceptions, profile text, endpoint URLs or paths.
        public = {"runtime_phase": health.get("phase"), "fault": fault.split(":", 1)[0] if fault else None}
        for name in ("audio", "video"):
            source = health.get(name) or {}
            public[name] = {key: source.get(key) for key in ("expected", "sequence", "age_s")}
        public["event_loop_age_s"] = health.get("event_loop_age_s")
        with self._lock:
            self._health = public

    def snapshot(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return {
                "configuration": dict(self._configuration),
                "health": copy.deepcopy(self._health) if session_id == self._session_id else {},
            }
