"""In-process reception sessions behind the native control connection.

No CLI invocation, child runner, daemon-wide media release, or backend restart.
The dedicated worker loop keeps synchronous model/SDK work off the control loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .official_runtime.live_session import LiveSessionControl
from .official_runtime.session_supervisor import HealthThresholds, evaluate_heartbeat
from .reception_status import ReceptionStatus

LOGGER = logging.getLogger(__name__)

BOOL_OPTIONS = {
    "warmup_audio", "warmup_video", "record_audio", "record_video", "capture_vision",
    "perception", "gestures", "audio_gate", "ready_cue", "conversation_cues",
}
PATH_OPTIONS = {"artifact_root", "agent_profile_public_dir", "agent_profile_private_dir",
                "vision_pipelines_config", "policy_audio_cache_dir"}
CHOICE_OPTIONS = {
    "agent_tools": {"none", "time-web"},
    "agent_profile_format": {"overlay", "hermes"},
    "vision_runtime": {"serial-v1", "broker-v1"},
    "gesture_running_mode": {"image", "video"},
    "wave_detection_mode": {"open_palm"},
    "rerun_mode": {"off", "file"},
}
STRING_OPTIONS = {"robot_host", "hf_realtime_ws_url", "agent_profile_id", "hf_voice",
                  "visitor_trigger_profile"}
NUMBER_OPTIONS = {"duration", "broker_capture_fps", "broker_policy_idle_s",
                  "broker_recorder_queue_size", "broker_gesture_queue_size"}


def load_runtime_options(path: Path) -> dict[str, Any]:
    """Read explicit, server-owned settings; no secrets/config arrive over control."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema_version", "options"}:
        raise ValueError("Expected schema_version and options")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("Unsupported runtime configuration version")
    supplied = document["options"]
    allowed = BOOL_OPTIONS | PATH_OPTIONS | CHOICE_OPTIONS.keys() | STRING_OPTIONS | NUMBER_OPTIONS
    if not isinstance(supplied, dict) or set(supplied) - allowed:
        raise ValueError("Unsupported runtime option")
    required = {"robot_host", "artifact_root", "hf_realtime_ws_url", "agent_profile_id",
                "agent_profile_format", "agent_tools", "visitor_trigger_profile"}
    if not required <= supplied.keys():
        raise ValueError("Runtime configuration must name robot, artifacts, backend, profile format, tools and vision policy")
    options = {
        "duration": None, "perception": True, "gestures": True, "warmup_video": True,
        "audio_gate": True, "conversation_cues": True, "capture_vision": True,
        "record_audio": True, "record_video": False, "vision_runtime": "broker-v1",
        **supplied,
    }
    for key, value in options.items():
        if key in BOOL_OPTIONS and type(value) is not bool:
            raise ValueError(f"{key} must be a boolean")
        if key in STRING_OPTIONS and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{key} must be a non-empty string")
        if key in CHOICE_OPTIONS and (not isinstance(value, str) or value not in CHOICE_OPTIONS[key]):
            raise ValueError(f"Unsupported {key}")
        if key in PATH_OPTIONS:
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError(f"{key} must be an absolute path")
            options[key] = Path(value)
        if key in NUMBER_OPTIONS:
            if key == "duration" and value is None:
                continue
            if type(value) not in {int, float} or not math.isfinite(value):
                raise ValueError(f"{key} must be finite")
            if value < 0 or (key != "broker_policy_idle_s" and value == 0):
                raise ValueError(f"Invalid {key}")
            if key.endswith("queue_size") and type(value) is not int:
                raise ValueError(f"{key} must be an integer")
    endpoint = urlsplit(options["hf_realtime_ws_url"])
    if endpoint.scheme not in {"ws", "wss"} or not endpoint.hostname or endpoint.username or endpoint.password:
        raise ValueError("hf_realtime_ws_url must be a WebSocket URL without embedded credentials")
    return options


def _check_legacy_owner() -> None:
    # Older frozen CLI releases predate the shared advisory lease. Never stop them.
    from .official_runtime.ops_core import LIVE_PATTERN, _find_pids

    if _find_pids(LIVE_PATTERN):
        raise RuntimeError("An existing CLI receptionist is running; stop it before native Start")


def make_reception_runtime(
    options: dict[str, Any], *, thresholds: HealthThresholds | None = None,
    cleanup_timeout: float = 5.0, poll_interval: float = 0.25,
    session_entry: Callable | None = None,
    check_owner: Callable[[], None] = _check_legacy_owner,
    status: ReceptionStatus | None = None,
) -> Callable:
    if cleanup_timeout <= 0 or poll_interval <= 0:
        raise ValueError("Positive cleanup and polling intervals required")
    thresholds = thresholds or HealthThresholds()
    options = dict(options)

    async def run(stop: asyncio.Event, ready: Callable[[], None], session_id: str) -> None:
        if stop.is_set():
            return
        from reachy_mini_reception_app.protocol import identifier

        run_id = f"native-{identifier(session_id)}"
        if status is not None:
            status.begin(session_id)
        artifact_root = Path(options["artifact_root"])
        if (artifact_root / "runs" / f"run-{run_id}.json").exists():
            raise RuntimeError("Refusing to reuse an existing native run ID")
        loop = asyncio.get_running_loop()
        control = LiveSessionControl(lambda: loop.call_soon_threadsafe(ready))
        completed: concurrent.futures.Future[None] = concurrent.futures.Future()
        done = asyncio.wrap_future(completed)
        # Consume eventual errors even when cleanup timeout has already faulted the service.
        done.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)
        started = time.monotonic()

        def worker() -> None:
            try:
                if control.stop_requested.is_set():
                    completed.set_result(None)
                    return
                check_owner()
                entry = session_entry
                if entry is None:
                    from .official_runtime.live_app import run_live_session

                    entry = run_live_session
                asyncio.run(entry(run_id=run_id, control=control, **options))
            except BaseException as exc:
                completed.set_exception(exc)
            else:
                completed.set_result(None)

        thread = threading.Thread(target=worker, name=f"reception-{session_id}", daemon=True)
        LOGGER.info("service runtime starting run_id=%s", run_id)
        thread.start()
        stop_waiter = asyncio.create_task(stop.wait())
        fault = None
        try:
            while not done.done() and not stop.is_set():
                await asyncio.wait({done, stop_waiter}, timeout=poll_interval,
                                   return_when=asyncio.FIRST_COMPLETED)
                if done.done() or stop.is_set():
                    break
                health = control.liveness.snapshot() if control.liveness is not None else None
                fault = evaluate_heartbeat(
                    health, now_monotonic=time.monotonic(),
                    supervisor_started_monotonic=started, thresholds=thresholds,
                )
                if status is not None:
                    status.update(health, fault=fault)
                if fault is not None:
                    LOGGER.error("service runtime liveness fault run_id=%s fault=%s", run_id, fault)
                    break
        finally:
            stop_waiter.cancel()
            try:
                await stop_waiter
            except asyncio.CancelledError:
                pass
            if not done.done():
                control.request_stop()
                finished, _ = await asyncio.wait({done}, timeout=cleanup_timeout)
                if not finished:
                    # Python cannot safely kill a blocked native call or worker thread.
                    # The controller must latch fault and refuse another physical session.
                    raise RuntimeError("Runtime cleanup incomplete; worker may still own robot resources")
            try:
                await asyncio.shield(done)
            finally:
                if status is not None:
                    status.update(control.liveness.snapshot() if control.liveness else None, fault=fault)
            LOGGER.info("service runtime closed run_id=%s", run_id)
        if fault is not None:
            raise RuntimeError(f"Reception liveness fault: {fault}")

    return run
