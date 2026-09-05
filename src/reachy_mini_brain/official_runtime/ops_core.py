"""Operational control primitives for the official-runtime live path.

The functions in this module are intentionally UI-agnostic: they return
structured results and do not print. The dev CLI, a future app, and tests can
all call the same action layer.
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .env import PROJECT_ROOT, clean_gstreamer_environment, load_project_env
from .visitor_trigger_profiles import (
    DEFAULT_VISITOR_TRIGGER_PROFILE,
    LEGACY_VISITOR_TRIGGER_PROFILE,
    resolve_visitor_trigger_profile,
)


LIVE_PATTERN = "reachy_mini_brain.official_runtime.live_app"
SUPERVISOR_PATTERN = "reachy_mini_brain.official_runtime.session_supervisor"
BACKEND_PATTERN = "speech-to-speech --mode realtime"
RUNNER_STOP_GRACE_S = 30.0
SUPERVISOR_STOP_GRACE_S = 60.0
DEFAULT_PREFLIGHT_WAV = (
    "audio-response-resp_db3304df3e804556b0aaa7ed7990048f-"
    "official-live-20260623-122844-01-pcm16.wav"
)
DEFAULT_POLICY_PREFLIGHT_GREETING = "Welcome to the clinic. How can I help you today?"
DEFAULT_HERMES_HEALTH_URL = "http://127.0.0.1:8642/health"
DEFAULT_PROVIDER_HEALTH_URL = "https://openrouter.ai/api/v1/key"
DEFAULT_S2S_SERVICE_LABEL = "com.reachy.reception.s2s"
DEFAULT_HERMES_SERVICE_LABEL = "com.reachy.reception.hermes"
REQUIRED_S2S_PROCESS_TYPE = "Interactive"
LAUNCHD_STOP_TIMEOUT_S = 15.0


class OpsError(RuntimeError):
    """Base error for ops failures."""


class AuthorizationError(OpsError):
    """Raised when a physical action is requested without authorization."""


@dataclass(frozen=True)
class OpsConfig:
    """Configuration for m1max/robot ops actions."""

    repo_path: Path
    robot_host: str
    robot_port: int
    s2s_host: str
    s2s_port: int
    live_duration_s: float
    policy_preflight_duration_s: float
    policy_preflight_timeout_s: float
    policy_preflight_gap_s: float
    policy_preflight_greeting: str
    preflight_between_probes_gap_s: float
    log_dir: Path
    state_dir: Path
    preflight_wav: Path
    stop_backend_on_exit: bool
    conversation_cues: bool
    capture_vision: bool
    record_audio: bool
    record_video: bool
    python_bin: Path
    backend_start_timeout_s: float
    keep_awake: bool
    profile_owned_context: bool = False
    agent_profile_id: str = ""
    s2s_cli_mode: str = "legacy"
    visitor_trigger_profile: str = DEFAULT_VISITOR_TRIGGER_PROFILE
    vision_pipelines_config: Path | None = None
    rerun_mode: str = "off"
    rerun_grpc_url: str = "rerun+http://127.0.0.1:9876/proxy"
    rerun_image_fps: float = 5.0
    rerun_jpeg_quality: int = 80
    rerun_queue_size: int = 3
    media_heartbeat_interval_s: float = 1.0
    media_startup_grace_s: float = 180.0
    media_heartbeat_stale_s: float = 5.0
    media_source_stale_s: float = 8.0
    event_loop_stale_s: float = 8.0
    vision_runtime: str = "serial-v1"
    broker_capture_fps: float = 15.0
    broker_recorder_queue_size: int = 30
    broker_gesture_queue_size: int = 30
    broker_policy_idle_s: float = 0.1
    gesture_running_mode: str = "image"
    wave_detection_mode: str = "open_palm"
    extended_health: bool = False
    hermes_health_url: str = DEFAULT_HERMES_HEALTH_URL
    provider_health_url: str = DEFAULT_PROVIDER_HEALTH_URL
    provider_api_key_env: str = "OPENROUTER_API_KEY"
    external_health_timeout_s: float = 5.0
    artifact_disk_min_free_gb: float = 20.0
    recording_retention_days: int = 30
    backend_trace_dir: Path | None = None
    require_managed_services: bool = False
    s2s_service_label: str = DEFAULT_S2S_SERVICE_LABEL
    hermes_service_label: str = DEFAULT_HERMES_SERVICE_LABEL

    @classmethod
    def from_env(cls) -> "OpsConfig":
        load_project_env()
        repo_path = Path(os.environ.get("REACHY_REPO", str(PROJECT_ROOT))).expanduser()
        log_dir = Path(os.environ.get("LOG_DIR", str(repo_path / "artifacts" / "logs"))).expanduser()
        state_dir = Path(os.environ.get("OPS_STATE_DIR", str(repo_path / "artifacts" / "ops"))).expanduser()
        preflight_wav = Path(
            os.environ.get(
                "PREFLIGHT_WAV",
                str(
                    repo_path
                    / "artifacts"
                    / "official-runtime-live"
                    / "audio"
                    / "playable"
                    / DEFAULT_PREFLIGHT_WAV
                ),
            )
        ).expanduser()
        python_bin = _default_python_bin(repo_path=repo_path)
        return cls(
            repo_path=repo_path,
            robot_host=os.environ.get("ROBOT_HOST", "192.168.1.165"),
            robot_port=int(os.environ.get("ROBOT_PORT", "8000")),
            s2s_host=os.environ.get("S2S_HOST", "127.0.0.1"),
            s2s_port=int(os.environ.get("S2S_PORT", "8765")),
            live_duration_s=float(os.environ.get("LIVE_DURATION", "900")),
            policy_preflight_duration_s=float(os.environ.get("POLICY_PREFLIGHT_DURATION", "90")),
            policy_preflight_timeout_s=float(os.environ.get("POLICY_PREFLIGHT_TIMEOUT", "30")),
            policy_preflight_gap_s=float(os.environ.get("POLICY_PREFLIGHT_GAP", "3")),
            policy_preflight_greeting=os.environ.get("POLICY_PREFLIGHT_GREETING", DEFAULT_POLICY_PREFLIGHT_GREETING),
            preflight_between_probes_gap_s=float(os.environ.get("PREFLIGHT_BETWEEN_PROBES_GAP", "3")),
            log_dir=log_dir,
            state_dir=state_dir,
            preflight_wav=preflight_wav,
            stop_backend_on_exit=_env_bool("STOP_BACKEND_ON_EXIT", default=False),
            conversation_cues=_env_bool("CONVERSATION_CUES", default=True),
            capture_vision=_env_bool("CAPTURE_VISION", default=True),
            record_audio=_env_bool("RECORD_AUDIO", default=True),
            record_video=_env_bool("RECORD_VIDEO", default=False),
            python_bin=python_bin,
            backend_start_timeout_s=float(os.environ.get("BACKEND_START_TIMEOUT", "45")),
            keep_awake=_env_bool("OPS_KEEP_AWAKE", default=True),
            profile_owned_context=_env_bool("S2S_RESPONSES_CONVERSATION", default=False),
            agent_profile_id=os.environ.get("RECEPTION_AGENT_PROFILE_ID", "").strip(),
            s2s_cli_mode=os.environ.get("S2S_CLI_MODE", "legacy"),
            visitor_trigger_profile=resolve_visitor_trigger_profile(
                os.environ.get("RECEPTION_VISITOR_TRIGGER_PROFILE", DEFAULT_VISITOR_TRIGGER_PROFILE)
            ).name,
            vision_pipelines_config=(
                Path(os.environ["RECEPTION_VISION_PIPELINES_CONFIG"]).expanduser()
                if os.environ.get("RECEPTION_VISION_PIPELINES_CONFIG")
                else None
            ),
            rerun_mode=os.environ.get("RECEPTION_RERUN_MODE", "off"),
            rerun_grpc_url=os.environ.get(
                "RECEPTION_RERUN_GRPC_URL",
                "rerun+http://127.0.0.1:9876/proxy",
            ),
            rerun_image_fps=float(os.environ.get("RECEPTION_RERUN_IMAGE_FPS", "5")),
            rerun_jpeg_quality=int(os.environ.get("RECEPTION_RERUN_JPEG_QUALITY", "80")),
            rerun_queue_size=int(os.environ.get("RECEPTION_RERUN_QUEUE_SIZE", "3")),
            media_heartbeat_interval_s=float(os.environ.get("MEDIA_HEARTBEAT_INTERVAL_S", "1")),
            media_startup_grace_s=float(os.environ.get("MEDIA_STARTUP_GRACE_S", "180")),
            media_heartbeat_stale_s=float(os.environ.get("MEDIA_HEARTBEAT_STALE_S", "5")),
            media_source_stale_s=float(os.environ.get("MEDIA_SOURCE_STALE_S", "8")),
            event_loop_stale_s=float(os.environ.get("EVENT_LOOP_STALE_S", "8")),
            vision_runtime=os.environ.get("RECEPTION_VISION_RUNTIME", "serial-v1"),
            broker_capture_fps=float(os.environ.get("RECEPTION_BROKER_CAPTURE_FPS", "15")),
            broker_recorder_queue_size=int(
                os.environ.get("RECEPTION_BROKER_RECORDER_QUEUE_SIZE", "30")
            ),
            broker_gesture_queue_size=int(
                os.environ.get("RECEPTION_BROKER_GESTURE_QUEUE_SIZE", "30")
            ),
            broker_policy_idle_s=float(
                os.environ.get("RECEPTION_BROKER_POLICY_IDLE_S", "0.1")
            ),
            gesture_running_mode=os.environ.get("RECEPTION_GESTURE_RUNNING_MODE", "image"),
            wave_detection_mode=os.environ.get("RECEPTION_WAVE_DETECTION_MODE", "open_palm"),
            extended_health=_env_bool("OPS_EXTENDED_HEALTH", default=False),
            hermes_health_url=os.environ.get(
                "HERMES_HEALTH_URL", DEFAULT_HERMES_HEALTH_URL
            ),
            provider_health_url=os.environ.get(
                "PROVIDER_HEALTH_URL", DEFAULT_PROVIDER_HEALTH_URL
            ),
            provider_api_key_env=os.environ.get(
                "PROVIDER_HEALTH_API_KEY_ENV", "OPENROUTER_API_KEY"
            ),
            external_health_timeout_s=float(
                os.environ.get("EXTERNAL_HEALTH_TIMEOUT_S", "5")
            ),
            artifact_disk_min_free_gb=float(
                os.environ.get("ARTIFACT_DISK_MIN_FREE_GB", "20")
            ),
            recording_retention_days=int(
                os.environ.get("RECORDING_RETENTION_DAYS", "30")
            ),
            backend_trace_dir=(
                Path(os.environ["S2S_EVENT_TRACE_DIR"]).expanduser()
                if os.environ.get("S2S_EVENT_TRACE_DIR") else None
            ),
            require_managed_services=_env_bool(
                "OPS_REQUIRE_MANAGED_SERVICES", default=False
            ),
            s2s_service_label=os.environ.get(
                "S2S_SERVICE_LABEL", DEFAULT_S2S_SERVICE_LABEL
            ),
            hermes_service_label=os.environ.get(
                "HERMES_SERVICE_LABEL", DEFAULT_HERMES_SERVICE_LABEL
            ),
        )

    @property
    def robot_api(self) -> str:
        return f"http://{self.robot_host}:{self.robot_port}"

    @property
    def runner_state_path(self) -> Path:
        return self.state_dir / "runner-state.json"

    @property
    def latest_run_path(self) -> Path:
        return self.state_dir / "latest-run.json"

    @property
    def artifact_root(self) -> Path:
        return self.repo_path / "artifacts" / "official-runtime-live"

    @property
    def supervisor_dir(self) -> Path:
        return self.state_dir / "supervisors"

    @property
    def heartbeat_dir(self) -> Path:
        return self.state_dir / "heartbeats"

    @property
    def terminal_status_dir(self) -> Path:
        return self.state_dir / "runs"


@dataclass(frozen=True)
class Verification:
    kind: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "status": self.status, "details": _jsonable(self.details)}


@dataclass(frozen=True)
class HumanQualityGate:
    required: bool
    prompt: str

    def to_dict(self) -> dict[str, Any]:
        return {"required": self.required, "prompt": self.prompt}


@dataclass(frozen=True)
class ActionResult:
    action: str
    status: str = "ok"
    safety: str = "safe"
    authorization_required: bool = False
    authorized: bool = False
    changed: bool = False
    machine_verification: tuple[Verification, ...] = ()
    human_quality_gate: HumanQualityGate | None = None
    data: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "status": self.status,
            "safety": self.safety,
            "authorization_required": self.authorization_required,
            "authorized": self.authorized,
            "changed": self.changed,
            "machine_verification": [item.to_dict() for item in self.machine_verification],
            "data": _jsonable(self.data),
            "errors": list(self.errors),
        }
        if self.human_quality_gate is not None:
            payload["human_quality_gate"] = self.human_quality_gate.to_dict()
        return payload


@dataclass(frozen=True)
class RunnerState:
    pid: int
    run_id: str
    log_path: Path
    artifact_root: Path
    started_at: str
    requested_config: dict[str, Any]
    command: tuple[str, ...]
    runner_pid: int | None = None
    heartbeat_path: Path | None = None
    terminal_status_path: Path | None = None
    supervisor_spec_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "run_id": self.run_id,
            "log_path": str(self.log_path),
            "artifact_root": str(self.artifact_root),
            "started_at": self.started_at,
            "requested_config": _jsonable(self.requested_config),
            "command": list(self.command),
            "runner_pid": self.runner_pid,
            "heartbeat_path": str(self.heartbeat_path) if self.heartbeat_path is not None else None,
            "terminal_status_path": (
                str(self.terminal_status_path) if self.terminal_status_path is not None else None
            ),
            "supervisor_spec_path": (
                str(self.supervisor_spec_path) if self.supervisor_spec_path is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunnerState":
        return cls(
            pid=int(data["pid"]),
            run_id=str(data["run_id"]),
            log_path=Path(data["log_path"]),
            artifact_root=Path(data["artifact_root"]),
            started_at=str(data["started_at"]),
            requested_config=dict(data.get("requested_config") or {}),
            command=tuple(str(item) for item in data.get("command") or ()),
            runner_pid=(int(data["runner_pid"]) if data.get("runner_pid") is not None else None),
            heartbeat_path=(Path(data["heartbeat_path"]) if data.get("heartbeat_path") else None),
            terminal_status_path=(
                Path(data["terminal_status_path"]) if data.get("terminal_status_path") else None
            ),
            supervisor_spec_path=(
                Path(data["supervisor_spec_path"]) if data.get("supervisor_spec_path") else None
            ),
        )

    @property
    def manifest_path(self) -> Path:
        return self.artifact_root / "runs" / f"run-{self.run_id}.json"


def backend_status(config: OpsConfig) -> ActionResult:
    port_live = _port_open(config.s2s_host, config.s2s_port)
    pids = _find_pids(_backend_pattern(config))
    service = launchd_service_status(config.s2s_service_label)
    process_type = launchd_service_process_type(
        config.s2s_service_label,
        expected=REQUIRED_S2S_PROCESS_TYPE,
    )
    status = "ok" if port_live else "stopped"
    if pids and not port_live:
        status = "degraded"
    if config.require_managed_services and service["status"] != "loaded":
        status = "degraded"
    if config.require_managed_services and process_type["status"] != "ok":
        status = "degraded"
    errors: list[str] = []
    if config.require_managed_services and service["status"] != "loaded":
        errors.append(f"required launchd service is not loaded: {config.s2s_service_label}")
    if config.require_managed_services and process_type["status"] != "ok":
        errors.append(
            "required launchd ProcessType mismatch: "
            f"expected {REQUIRED_S2S_PROCESS_TYPE}, got {process_type.get('actual') or 'missing'}"
        )
    return ActionResult(
        action="backend.status",
        status=status,
        machine_verification=(
            Verification("tcp_port", "ok" if port_live else "failed", {"host": config.s2s_host, "port": config.s2s_port}),
            Verification("process", "ok" if pids else "not_found", {"pids": pids}),
            Verification(
                "managed_service",
                service["status"],
                {"label": config.s2s_service_label},
            ),
            Verification(
                "managed_process_type",
                process_type["status"] if config.require_managed_services else "not_required",
                process_type,
            ),
        ),
        data={
            "host": config.s2s_host,
            "port": config.s2s_port,
            "port_live": port_live,
            "pids": pids,
            "managed_service": service,
            "managed_process_type": process_type,
        },
        errors=tuple(errors),
    )


def start_backend(config: OpsConfig) -> ActionResult:
    status_before = backend_status(config)
    if status_before.data["port_live"]:
        if config.require_managed_services and status_before.data["managed_service"]["status"] != "loaded":
            return ActionResult(
                action="backend.start",
                status="failed",
                changed=False,
                machine_verification=status_before.machine_verification,
                data=status_before.data,
                errors=(
                    "backend port is held by an unmanaged process; stop it before enabling the production service",
                ),
            )
        if (
            config.require_managed_services
            and status_before.data["managed_process_type"]["status"] != "ok"
        ):
            return ActionResult(
                action="backend.start",
                status="failed",
                changed=False,
                machine_verification=status_before.machine_verification,
                data=status_before.data,
                errors=status_before.errors,
            )
        return ActionResult(
            action="backend.start",
            status="ok",
            changed=False,
            machine_verification=status_before.machine_verification,
            data={**status_before.data, "already_running": True},
        )

    path_errors = _validate_backend_launch_paths(config)
    if path_errors:
        return ActionResult(action="backend.start", status="failed", errors=tuple(path_errors))

    service_plist = _launch_agent_path(config.s2s_service_label)
    if config.require_managed_services:
        service_start = _start_launchd_service(config.s2s_service_label, service_plist)
        if service_start is None:
            return ActionResult(
                action="backend.start",
                status="failed",
                changed=False,
                errors=(f"required launchd service is unavailable: {config.s2s_service_label}",),
            )
        deadline = time.monotonic() + config.backend_start_timeout_s
        while time.monotonic() < deadline:
            if _port_open(config.s2s_host, config.s2s_port):
                current = backend_status(config)
                return ActionResult(
                    action="backend.start",
                    status="ok",
                    changed=True,
                    machine_verification=current.machine_verification,
                    data={**current.data, "managed_start": service_start},
                )
            time.sleep(1)
        return ActionResult(
            action="backend.start",
            status="failed",
            changed=True,
            data={"managed_start": service_start},
            errors=("managed backend did not become ready before timeout",),
        )

    config.log_dir.mkdir(parents=True, exist_ok=True)
    logfile = config.log_dir / f"s2s-backend-live-{_timestamp()}.log"
    env = _base_env(config)
    env.update(
        {
            "REACHY_REPO": str(config.repo_path),
            "ENV_FILE": str(config.repo_path / ".env"),
            "S2S_HOST": config.s2s_host,
            "S2S_PORT": str(config.s2s_port),
        }
    )
    command = [str(config.repo_path / "scripts" / "m1max" / "run_s2s_backend.sh")]
    proc, caffeinate_pid = _launch_background(command, cwd=config.repo_path, env=env, logfile=logfile, keep_awake=config.keep_awake)

    deadline = time.monotonic() + config.backend_start_timeout_s
    while time.monotonic() < deadline:
        if _port_open(config.s2s_host, config.s2s_port):
            return ActionResult(
                action="backend.start",
                status="ok",
                changed=True,
                machine_verification=(
                    Verification("tcp_port", "ok", {"host": config.s2s_host, "port": config.s2s_port}),
                    Verification("process", "ok", {"pid": proc.pid, "caffeinate_pid": caffeinate_pid}),
                ),
                data={"pid": proc.pid, "caffeinate_pid": caffeinate_pid, "log_path": logfile, "command": command},
            )
        if proc.poll() is not None:
            return ActionResult(
                action="backend.start",
                status="failed",
                changed=True,
                machine_verification=(Verification("process", "failed", {"pid": proc.pid, "returncode": proc.returncode}),),
                data={"pid": proc.pid, "caffeinate_pid": caffeinate_pid, "log_path": logfile, "command": command},
                errors=("backend exited before the websocket port became ready",),
            )
        time.sleep(1)

    return ActionResult(
        action="backend.start",
        status="failed",
        changed=True,
        machine_verification=(Verification("tcp_port", "failed", {"host": config.s2s_host, "port": config.s2s_port}),),
        data={"pid": proc.pid, "caffeinate_pid": caffeinate_pid, "log_path": logfile, "command": command},
        errors=("backend did not become ready before timeout",),
    )


def stop_backend(config: OpsConfig) -> ActionResult:
    service = launchd_service_status(config.s2s_service_label)
    if config.require_managed_services and service["status"] == "loaded":
        requested = _find_pids(_backend_pattern(config))
        completed = _launchctl("bootout", _launchd_target(config.s2s_service_label))
        if completed.returncode != 0:
            return ActionResult(
                action="backend.stop",
                status="failed",
                changed=False,
                errors=(completed.stderr.strip() or "launchctl bootout failed",),
            )
        stopped_state = _wait_for_managed_backend_stopped(
            config,
            timeout_s=LAUNCHD_STOP_TIMEOUT_S,
        )
        remaining = stopped_state["pids"]
        stopped = [pid for pid in requested if pid not in remaining]
        stop_ok = (
            stopped_state["service_status"] != "loaded"
            and not stopped_state["port_live"]
            and not remaining
        )
        return ActionResult(
            action="backend.stop",
            status="ok" if stop_ok else "failed",
            changed=True,
            machine_verification=(
                Verification(
                    "managed_service_unloaded",
                    "ok" if stopped_state["service_status"] != "loaded" else "failed",
                    {
                        "label": config.s2s_service_label,
                        "status": stopped_state["service_status"],
                    },
                ),
                Verification(
                    "tcp_port_closed",
                    "ok" if not stopped_state["port_live"] else "failed",
                    {"host": config.s2s_host, "port": config.s2s_port},
                ),
                Verification(
                    "process_terminated",
                    "ok" if not remaining else "failed",
                    {"remaining_pids": remaining},
                ),
            ),
            data={"requested_pids": requested, "stopped_pids": stopped, **stopped_state},
            errors=(
                ()
                if stop_ok
                else ("managed backend did not fully stop before timeout",)
            ),
        )
    pids = _find_pids(_backend_pattern(config))
    stopped = _terminate_pids(pids)
    return ActionResult(
        action="backend.stop",
        status="ok",
        changed=bool(stopped),
        machine_verification=(Verification("process_terminated", "ok", {"pids": stopped}),),
        data={"requested_pids": pids, "stopped_pids": stopped},
    )


def restart_backend(config: OpsConfig) -> list[ActionResult]:
    stopped = stop_backend(config)
    if stopped.status != "ok":
        return [stopped]
    return [stopped, start_backend(config)]


def launchd_service_status(label: str) -> dict[str, Any]:
    """Return read-only launchd state without treating unsupported hosts as loaded."""

    if sys.platform != "darwin" or shutil.which("launchctl") is None:
        return {"label": label, "status": "unsupported"}
    completed = _launchctl("print", _launchd_target(label))
    if completed.returncode != 0:
        return {"label": label, "status": "not_loaded"}
    state = "unknown"
    pid: int | None = None
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            state = stripped.split("=", 1)[1].strip()
        elif stripped.startswith("pid = "):
            try:
                pid = int(stripped.split("=", 1)[1].strip())
            except ValueError:
                pid = None
    return {"label": label, "status": "loaded", "state": state, "pid": pid}


def launchd_service_process_type(label: str, *, expected: str) -> dict[str, Any]:
    """Read the installed LaunchAgent scheduling class used by production status."""

    plist_path = _launch_agent_path(label)
    result: dict[str, Any] = {
        "label": label,
        "path": str(plist_path),
        "expected": expected,
        "actual": None,
    }
    if not plist_path.is_file():
        return {**result, "status": "missing"}
    try:
        with plist_path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        return {**result, "status": "invalid", "error": str(exc)}
    actual = payload.get("ProcessType")
    return {
        **result,
        "status": "ok" if actual == expected else "mismatch",
        "actual": actual,
    }


def external_services_status(config: OpsConfig) -> ActionResult:
    """Check Hermes and provider authentication without making a model request."""

    if not config.extended_health:
        return ActionResult(
            action="external-services.status",
            status="not_enabled",
            data={"enabled": False},
        )

    errors: list[str] = []
    checks: list[Verification] = []
    data: dict[str, Any] = {"enabled": True}

    data["route"] = "hermes" if config.profile_owned_context else "direct"
    if config.profile_owned_context:
        hermes = _json_health_request(
            config.hermes_health_url,
            timeout_s=config.external_health_timeout_s,
        )
        data["hermes"] = hermes
        hermes_ok = hermes.get("ok") is True and hermes.get("body", {}).get("status") == "ok"
        checks.append(
            Verification(
                "hermes",
                "ok" if hermes_ok else "failed",
                {"url": config.hermes_health_url, "http_status": hermes.get("http_status")},
            )
        )
        if not hermes_ok:
            errors.append(f"Hermes health failed: {hermes.get('error') or hermes.get('http_status')}")
    else:
        data["hermes"] = {"status": "not_required", "reason": "direct_provider_route"}

    provider_key = os.environ.get(config.provider_api_key_env, "")
    if not provider_key:
        provider = {"ok": False, "error": f"missing {config.provider_api_key_env}"}
    else:
        provider_result = _json_health_request(
            config.provider_health_url,
            timeout_s=config.external_health_timeout_s,
            bearer_token=provider_key,
        )
        provider = {
            key: value
            for key, value in provider_result.items()
            if key in {"ok", "http_status", "error"}
        }
    data["provider"] = provider
    provider_ok = provider.get("ok") is True
    checks.append(
        Verification(
            "provider_auth",
            "ok" if provider_ok else "failed",
            {
                "url": config.provider_health_url,
                "http_status": provider.get("http_status"),
                "key_env": config.provider_api_key_env,
            },
        )
    )
    if not provider_ok:
        errors.append(f"provider authentication failed: {provider.get('error') or provider.get('http_status')}")

    if config.profile_owned_context:
        hermes_service = launchd_service_status(config.hermes_service_label)
        data["hermes_service"] = hermes_service
        service_ok = hermes_service["status"] == "loaded"
        checks.append(
            Verification(
                "hermes_managed_service",
                "ok" if service_ok else hermes_service["status"],
                {"label": config.hermes_service_label},
            )
        )
        if config.require_managed_services and not service_ok:
            errors.append(f"required launchd service is not loaded: {config.hermes_service_label}")
    else:
        data["hermes_service"] = {"status": "not_required", "reason": "direct_provider_route"}

    return ActionResult(
        action="external-services.status",
        status="ok" if not errors else "degraded",
        machine_verification=tuple(checks),
        data=data,
        errors=tuple(errors),
    )


def storage_status(config: OpsConfig) -> ActionResult:
    target = config.artifact_root
    existing_target = target if target.exists() else config.repo_path
    usage = shutil.disk_usage(existing_target)
    free_gb = usage.free / (1024**3)
    threshold_gb = config.artifact_disk_min_free_gb
    disk_ok = free_gb >= threshold_gb
    retention = recording_retention_report(
        config.artifact_root,
        retention_days=config.recording_retention_days,
        trace_root=config.backend_trace_dir,
        path_limit=0,
    )
    errors = () if disk_ok else (f"artifact disk free space is below {threshold_gb:g} GiB",)
    return ActionResult(
        action="storage.status",
        status="ok" if disk_ok else "degraded",
        machine_verification=(
            Verification(
                "disk_headroom",
                "ok" if disk_ok else "failed",
                {
                    "path": str(existing_target),
                    "free_gb": round(free_gb, 2),
                    "minimum_free_gb": threshold_gb,
                },
            ),
            Verification(
                "recording_retention",
                "due" if retention["due_file_count"] else "ok",
                {
                    "retention_days": retention["retention_days"],
                    "due_file_count": retention["due_file_count"],
                    "due_bytes": retention["due_bytes"],
                },
            ),
        ),
        data={
            "disk": {
                "path": str(existing_target),
                "total_gb": round(usage.total / (1024**3), 2),
                "used_gb": round(usage.used / (1024**3), 2),
                "free_gb": round(free_gb, 2),
                "minimum_free_gb": threshold_gb,
            },
            "recording_retention": retention,
        },
        errors=errors,
    )


def recording_retention_report(
    artifact_root: Path,
    *,
    retention_days: int,
    now_ts: float | None = None,
    path_limit: int = 50,
    trace_root: Path | None = None,
) -> dict[str, Any]:
    """Report old audio/video and backend traces; never remove or modify them."""

    if retention_days < 1:
        raise OpsError("recording retention days must be at least 1")
    now = time.time() if now_ts is None else now_ts
    cutoff = now - retention_days * 86400
    due: list[tuple[float, Path, int]] = []
    scanned = 0
    trace_root = trace_root or artifact_root.parent / "s2s-backend-trace"
    directories = [artifact_root / "audio", artifact_root / "video", trace_root]
    seen: set[Path] = set()
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_symlink() or not path.is_file() or path.resolve() in seen:
                continue
            seen.add(path.resolve())
            scanned += 1
            stat = path.stat()
            if stat.st_mtime < cutoff:
                due.append((stat.st_mtime, path, stat.st_size))
    due.sort(key=lambda item: item[0])
    due_bytes = sum(item[2] for item in due)
    listed = [
        {
            "path": str(path.relative_to(artifact_root)) if path.is_relative_to(artifact_root) else str(path.absolute()),
            "modified_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            "bytes": size,
        }
        for mtime, path, size in due[:path_limit]
    ]
    return {
        "artifact_root": str(artifact_root),
        "backend_trace_root": str(trace_root),
        "retention_days": retention_days,
        "cutoff": datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(),
        "scanned_file_count": scanned,
        "due_file_count": len(due),
        "due_bytes": due_bytes,
        "listed_file_count": len(listed),
        "paths_truncated": len(due) > len(listed),
        "due_files": listed,
        "deletion_performed": False,
    }


def robot_status(config: OpsConfig) -> ActionResult:
    checks: dict[str, Any] = {}
    errors: list[str] = []
    for label, path in (
        ("daemon", "/api/daemon/status"),
        ("media", "/api/media/status"),
        ("motors", "/api/motors/status"),
        ("running_moves", "/api/move/running"),
        ("volume", "/api/volume/current"),
    ):
        try:
            checks[label] = _robot_get(config, path)
        except OpsError as exc:
            checks[label] = None
            errors.append(f"{label}: {exc}")
    return ActionResult(
        action="robot.status",
        status="ok" if not errors else "degraded",
        safety="read_only_robot",
        machine_verification=(
            Verification("robot_api", "ok" if not errors else "degraded", {"base_url": config.robot_api}),
        ),
        data=checks,
        errors=tuple(errors),
    )


def wake_robot(config: OpsConfig, *, authorized: bool, sleep_fn=time.sleep) -> ActionResult:
    _require_physical_authorization("robot.wake", authorized)
    _robot_post(config, "/api/daemon/start?wake_up=false")
    _robot_post(config, "/api/media/acquire")
    _robot_post(config, "/api/motors/set_mode/enabled")
    _robot_post(config, "/api/move/play/wake_up")
    sleep_fn(3)
    status = robot_status(config)
    return ActionResult(
        action="robot.wake",
        status="ok" if status.status == "ok" else "degraded",
        safety="physical",
        authorization_required=True,
        authorized=True,
        changed=True,
        machine_verification=status.machine_verification,
        data=status.data,
        errors=status.errors,
    )


def sleep_robot(config: OpsConfig, *, authorized: bool, sleep_fn=time.sleep) -> ActionResult:
    _require_physical_authorization("robot.sleep", authorized)
    stop_running_moves(config)
    _robot_post(config, "/api/media/release", tolerate_errors=True)
    _robot_post(config, "/api/move/play/goto_sleep", tolerate_errors=True)
    sleep_fn(3)
    stop_running_moves(config)
    _robot_post(config, "/api/motors/set_mode/disabled", tolerate_errors=True)
    status = robot_status(config)
    return ActionResult(
        action="robot.sleep",
        status="ok" if status.status == "ok" else "degraded",
        safety="physical",
        authorization_required=True,
        authorized=True,
        changed=True,
        machine_verification=status.machine_verification,
        data=status.data,
        errors=status.errors,
    )


def finalize_robot_after_run(
    config: OpsConfig,
    *,
    attempts: int = 2,
    request_timeout_s: float = 2.0,
    sleep_fn=time.sleep,
) -> ActionResult:
    """Best-effort, bounded robot cleanup owned by the session supervisor."""

    attempts = max(1, attempts)
    all_attempts: list[dict[str, Any]] = []
    final_errors: list[str] = []
    for attempt in range(1, attempts + 1):
        errors: list[str] = []
        stopped_moves: list[str] = []
        try:
            moves = _robot_get(config, "/api/move/running", timeout_s=request_timeout_s)
        except OpsError as exc:
            moves = []
            errors.append(f"running_moves: {exc}")
        for move in moves if isinstance(moves, list) else []:
            uuid = move.get("uuid") if isinstance(move, dict) else None
            if not uuid:
                continue
            try:
                _robot_post(
                    config,
                    "/api/move/stop",
                    json_body={"uuid": uuid},
                    timeout_s=request_timeout_s,
                )
                stopped_moves.append(uuid)
            except OpsError as exc:
                errors.append(f"stop_move:{uuid}: {exc}")
        goto_sleep_requested = False
        for label, path in (
            ("media_release", "/api/media/release"),
            ("goto_sleep", "/api/move/play/goto_sleep"),
        ):
            try:
                _robot_post(config, path, timeout_s=request_timeout_s)
                if label == "goto_sleep":
                    goto_sleep_requested = True
            except OpsError as exc:
                errors.append(f"{label}: {exc}")
        if goto_sleep_requested:
            sleep_fn(3.0)
        try:
            _robot_post(config, "/api/motors/set_mode/disabled", timeout_s=request_timeout_s)
        except OpsError as exc:
            errors.append(f"motors_disable: {exc}")
        all_attempts.append(
            {"attempt": attempt, "stopped_moves": stopped_moves, "errors": errors}
        )
        if not errors:
            final_errors = []
            break
        final_errors = errors
        if attempt < attempts:
            sleep_fn(float(attempt))
    status = "ok" if not final_errors else "degraded"
    return ActionResult(
        action="robot.finalize_after_run",
        status=status,
        safety="physical",
        authorization_required=False,
        authorized=True,
        changed=True,
        machine_verification=(
            Verification("cleanup_requests", status, {"attempts": all_attempts}),
        ),
        data={"attempts": all_attempts},
        errors=tuple(final_errors),
    )


def stop_running_moves(config: OpsConfig) -> ActionResult:
    try:
        moves = _robot_get(config, "/api/move/running")
    except OpsError as exc:
        return ActionResult(action="robot.stop_running_moves", status="degraded", errors=(str(exc),))
    stopped: list[str] = []
    for move in moves if isinstance(moves, list) else []:
        uuid = move.get("uuid") if isinstance(move, dict) else None
        if not uuid:
            continue
        try:
            _robot_post(config, "/api/move/stop", json_body={"uuid": uuid})
            stopped.append(uuid)
        except OpsError:
            continue
    return ActionResult(
        action="robot.stop_running_moves",
        status="ok",
        safety="physical",
        changed=bool(stopped),
        machine_verification=(Verification("moves_stopped", "ok", {"uuids": stopped}),),
        data={"stopped": stopped},
    )


def runner_status(config: OpsConfig) -> ActionResult:
    state = load_runner_state(config)
    live_pids = _find_pids(LIVE_PATTERN)
    supervisor_pids = _find_pids(SUPERVISOR_PATTERN)
    latest = load_latest_run(config)
    data: dict[str, Any] = {
        "live_pids": live_pids,
        "supervisor_pids": supervisor_pids,
        "state_file": config.runner_state_path,
    }
    checks: list[Verification] = [Verification("process_scan", "ok" if live_pids else "not_found", {"pids": live_pids})]
    status = "stopped"
    errors: list[str] = []
    if state is not None:
        alive = _pid_alive(state.pid)
        manifest_exists = state.manifest_path.exists()
        data["state"] = state.to_dict()
        data["pid_alive"] = alive
        data["manifest_exists"] = manifest_exists
        heartbeat: dict[str, Any] | None = None
        if state.heartbeat_path is not None:
            heartbeat = _load_json_file(state.heartbeat_path)
            data["heartbeat"] = heartbeat
        if state.terminal_status_path is not None:
            data["terminal_status"] = _load_json_file(state.terminal_status_path)
        checks.append(Verification("runner_state_pid", "ok" if alive else "stale", {"pid": state.pid}))
        checks.append(Verification("run_manifest", "ok" if manifest_exists else "missing", {"path": state.manifest_path}))
        if alive:
            health_fault = _active_runner_health_fault(config, state, heartbeat)
            if health_fault is None:
                status = "running"
            else:
                status = "faulting"
                data["health_fault"] = health_fault
                errors.append(f"runner media health fault: {health_fault}")
        else:
            status = "stale_state"
            errors.append("runner state file points to a non-running PID")
    elif live_pids:
        status = "unmanaged_running"
        errors.append("live runner process exists without an ops state file")
    elif latest is not None and latest.get("terminal_status_path"):
        terminal_status = _load_json_file(Path(latest["terminal_status_path"]))
        data["terminal_status"] = terminal_status
        if terminal_status is not None and terminal_status.get("status") != "complete":
            status = "stopped_faulted"
            errors.append(
                "latest run ended with terminal status "
                f"{terminal_status.get('status')}: {terminal_status.get('reason')}"
            )
    return ActionResult(
        action="runner.status",
        status=status,
        safety="read_only_process",
        machine_verification=tuple(checks),
        data=data,
        errors=tuple(errors),
    )


def start_runner(
    config: OpsConfig,
    *,
    authorized: bool,
    run_id: str | None = None,
    duration_s: float | None = None,
    perception: bool = True,
    gestures: bool = True,
    audio_gate: bool = True,
    ready_cue: bool = True,
    warmup_video: bool = True,
    conversation_cues: bool | None = None,
    capture_vision: bool | None = None,
    record_audio: bool | None = None,
    record_video: bool | None = None,
    vision_pipelines_config: Path | None = None,
    rerun_mode: str | None = None,
) -> ActionResult:
    _require_physical_authorization("runner.start", authorized)
    existing = runner_status(config)
    if existing.status in {"running", "faulting"}:
        return ActionResult(
            action="runner.start",
            status="failed",
            safety="physical",
            authorization_required=True,
            authorized=True,
            errors=("runner is already running",),
            data=existing.data,
        )

    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    path_errors = _validate_live_launch_paths(config)
    resolved_vision_config = vision_pipelines_config or config.vision_pipelines_config
    resolved_rerun_mode = rerun_mode or config.rerun_mode
    if resolved_vision_config is not None and not resolved_vision_config.is_file():
        path_errors.append(f"missing vision pipeline config: {resolved_vision_config}")
    if path_errors:
        return ActionResult(
            action="runner.start",
            status="failed",
            safety="physical",
            authorization_required=True,
            authorized=True,
            errors=tuple(path_errors),
        )
    actual_run_id = run_id or f"official-live-{_timestamp()}"
    logfile = config.log_dir / f"{actual_run_id}.log"
    heartbeat_path = config.heartbeat_dir / f"{actual_run_id}.json"
    terminal_status_path = config.terminal_status_dir / f"{actual_run_id}.json"
    supervisor_spec_path = config.supervisor_dir / f"{actual_run_id}.json"
    started_at = datetime.now().isoformat(timespec="seconds")
    command, env = build_live_command(
        config,
        run_id=actual_run_id,
        duration_s=duration_s or config.live_duration_s,
        perception=perception,
        gestures=gestures,
        audio_gate=audio_gate,
        ready_cue=ready_cue,
        warmup_video=warmup_video,
        conversation_cues=config.conversation_cues if conversation_cues is None else conversation_cues,
        capture_vision=config.capture_vision if capture_vision is None else capture_vision,
        record_audio=config.record_audio if record_audio is None else record_audio,
        record_video=config.record_video if record_video is None else record_video,
        vision_pipelines_config=resolved_vision_config,
        rerun_mode=resolved_rerun_mode,
        scripted_policy_flow="none",
        heartbeat_path=heartbeat_path,
        heartbeat_interval_s=config.media_heartbeat_interval_s,
    )
    supervisor_spec = {
        "schema_version": 1,
        "run_id": actual_run_id,
        "started_at": started_at,
        "command": command,
        "cwd": str(config.repo_path),
        "log_path": str(logfile),
        "state_path": str(config.runner_state_path),
        "heartbeat_path": str(heartbeat_path),
        "terminal_status_path": str(terminal_status_path),
        "manifest_path": str(config.artifact_root / "runs" / f"run-{actual_run_id}.json"),
        "robot_host": config.robot_host,
        "robot_port": config.robot_port,
        "runner_stop_grace_s": RUNNER_STOP_GRACE_S,
        "poll_interval_s": 1.0,
        "cleanup_attempts": 2,
        "cleanup_request_timeout_s": 2.0,
        "thresholds": {
            "startup_grace_s": config.media_startup_grace_s,
            "heartbeat_stale_s": config.media_heartbeat_stale_s,
            "source_stale_s": config.media_source_stale_s,
            "event_loop_stale_s": config.event_loop_stale_s,
        },
    }
    _atomic_write_json(supervisor_spec_path, supervisor_spec)
    supervisor_command = [
        str(config.python_bin),
        "-m",
        "reachy_mini_brain.official_runtime.session_supervisor",
        "--spec",
        str(supervisor_spec_path),
    ]
    proc, caffeinate_pid = _launch_background(
        supervisor_command,
        cwd=config.repo_path,
        env=env,
        logfile=logfile,
        keep_awake=config.keep_awake,
    )
    state = RunnerState(
        pid=proc.pid,
        run_id=actual_run_id,
        log_path=logfile,
        artifact_root=config.artifact_root,
        started_at=started_at,
        requested_config={
            "duration_s": duration_s or config.live_duration_s,
            "perception": perception,
            "gestures": gestures,
            "audio_gate": audio_gate,
            "ready_cue": ready_cue,
            "warmup_video": warmup_video,
            "conversation_cues": config.conversation_cues if conversation_cues is None else conversation_cues,
            "capture_vision": config.capture_vision if capture_vision is None else capture_vision,
            "record_audio": config.record_audio if record_audio is None else record_audio,
            "record_video": config.record_video if record_video is None else record_video,
            "profile_owned_context": config.profile_owned_context,
            "agent_profile_id": config.agent_profile_id or None,
            "visitor_trigger_profile": config.visitor_trigger_profile,
            "vision_pipelines_config": (
                str(resolved_vision_config) if resolved_vision_config is not None else None
            ),
            "rerun_mode": resolved_rerun_mode,
            "rerun_image_fps": config.rerun_image_fps,
            "rerun_jpeg_quality": config.rerun_jpeg_quality,
            "rerun_queue_size": config.rerun_queue_size,
            "vision_runtime": config.vision_runtime,
            "broker_capture_fps": config.broker_capture_fps,
            "broker_recorder_queue_size": config.broker_recorder_queue_size,
            "broker_gesture_queue_size": config.broker_gesture_queue_size,
            "broker_policy_idle_s": config.broker_policy_idle_s,
            "gesture_running_mode": config.gesture_running_mode,
            "wave_detection_mode": config.wave_detection_mode,
            "keep_awake": config.keep_awake,
            "caffeinate_pid": caffeinate_pid,
        },
        command=tuple(command),
        heartbeat_path=heartbeat_path,
        terminal_status_path=terminal_status_path,
        supervisor_spec_path=supervisor_spec_path,
    )
    save_runner_state(config, state)
    save_latest_run(config, state)
    return ActionResult(
        action="runner.start",
        status="ok",
        safety="physical",
        authorization_required=True,
        authorized=True,
        changed=True,
        machine_verification=(
            Verification(
                "supervisor_started",
                "ok",
                {"pid": proc.pid, "caffeinate_pid": caffeinate_pid},
            ),
        ),
        data={**state.to_dict(), "caffeinate_pid": caffeinate_pid},
    )


def stop_runner(config: OpsConfig, *, authorized: bool, include_unmanaged: bool = False) -> ActionResult:
    _require_physical_authorization("runner.stop", authorized)
    state = load_runner_state(config)
    pids: list[int] = []
    if state is not None and _pid_alive(state.pid):
        pids.append(state.pid)
    supervised = state is not None and state.supervisor_spec_path is not None
    if include_unmanaged and not supervised:
        for pid in _find_pids(LIVE_PATTERN):
            if pid not in pids:
                pids.append(pid)
    stop_grace_s = SUPERVISOR_STOP_GRACE_S if supervised else RUNNER_STOP_GRACE_S
    stopped = _terminate_pids(pids, grace_s=stop_grace_s)
    terminal_status = (
        _load_json_file(state.terminal_status_path)
        if state is not None and state.terminal_status_path is not None
        else None
    )
    supervised_cleanup = bool(
        supervised
        and terminal_status is not None
        and isinstance(terminal_status.get("cleanup"), dict)
    )
    if config.runner_state_path.exists() and (state is None or not _pid_alive(state.pid)):
        config.runner_state_path.unlink()
    return ActionResult(
        action="runner.stop",
        status="ok",
        safety="physical",
        authorization_required=True,
        authorized=True,
        changed=bool(stopped),
        machine_verification=(Verification("process_terminated", "ok", {"pids": stopped}),),
        data={
            "requested_pids": pids,
            "stopped_pids": stopped,
            "supervised_cleanup": supervised_cleanup,
            "terminal_status": terminal_status,
        },
    )


def shutdown(config: OpsConfig, *, authorized: bool) -> list[ActionResult]:
    _require_physical_authorization("shutdown", authorized)
    stopped = stop_runner(config, authorized=True, include_unmanaged=True)
    if stopped.data.get("supervised_cleanup"):
        return [stopped]
    return [stopped, sleep_robot(config, authorized=True)]


def preflight_backend_health(config: OpsConfig) -> ActionResult:
    current = backend_status(config)
    if current.status == "ok":
        return current
    return start_backend(config)


def preflight_robot_state(config: OpsConfig) -> ActionResult:
    return robot_status(config)


def preflight_audio_playback(config: OpsConfig, *, authorized: bool) -> ActionResult:
    _require_physical_authorization("preflight.audio_playback", authorized)
    path_errors = _validate_audio_playback_launch_paths(config)
    if path_errors:
        return ActionResult(
            action="preflight.audio_playback",
            status="failed",
            safety="physical",
            authorization_required=True,
            authorized=True,
            errors=tuple(path_errors),
        )
    if not config.preflight_wav.exists():
        return ActionResult(
            action="preflight.audio_playback",
            status="failed",
            safety="physical",
            authorization_required=True,
            authorized=True,
            errors=(f"missing preflight WAV: {config.preflight_wav}",),
        )
    stop_runner(config, authorized=True, include_unmanaged=True)
    sleep_robot(config, authorized=True)
    wake_robot(config, authorized=True)
    actual_run_id = f"official-audio-preflight-{_timestamp()}"
    command, env = build_audio_playback_command(config, config.preflight_wav, run_id=actual_run_id)
    completed = subprocess.run(command, cwd=config.repo_path, env=env, check=False)
    sleep_robot(config, authorized=True)
    status = "ok" if completed.returncode == 0 else "failed"
    return ActionResult(
        action="preflight.audio_playback",
        status=status,
        safety="physical",
        authorization_required=True,
        authorized=True,
        changed=True,
        machine_verification=(
            Verification("process_completed", status, {"returncode": completed.returncode, "command": command}),
        ),
        human_quality_gate=HumanQualityGate(
            required=True,
            prompt=(
                "Accept only if the known-good WAV sounded smooth. If it was choppy, "
                "do not start live conversation."
            ),
        ),
        data={"wav": config.preflight_wav, "run_id": actual_run_id},
    )


def preflight_policy(
    config: OpsConfig,
    *,
    authorized: bool,
    flow: str,
    run_id: str | None = None,
) -> ActionResult:
    _require_physical_authorization(f"preflight.policy_{flow}", authorized)
    if flow not in {"goodbye", "greet", "goodbye-greet"}:
        raise ValueError(f"unsupported policy preflight flow: {flow}")
    path_errors = _validate_live_launch_paths(config)
    if path_errors:
        return ActionResult(
            action=f"preflight.policy_{flow}",
            status="failed",
            safety="physical",
            authorization_required=True,
            authorized=True,
            errors=tuple(path_errors),
        )
    start_backend(config)
    stop_runner(config, authorized=True, include_unmanaged=True)
    wake_robot(config, authorized=True)
    actual_run_id = run_id or f"official-policy-preflight-{flow.replace('-', '_')}-{_timestamp()}"
    command, env = build_live_command(
        config,
        run_id=actual_run_id,
        duration_s=config.policy_preflight_duration_s,
        perception=False,
        gestures=False,
        audio_gate=False,
        ready_cue=True,
        warmup_video=False,
        conversation_cues=False,
        capture_vision=False,
        record_audio=True,
        record_video=False,
        scripted_policy_flow=flow,
        scripted_policy_gap_s=config.policy_preflight_gap_s,
        scripted_policy_timeout_s=config.policy_preflight_timeout_s,
        scripted_policy_greeting=config.policy_preflight_greeting if "greet" in flow else None,
        visitor_trigger_profile=LEGACY_VISITOR_TRIGGER_PROFILE,
    )
    config.log_dir.mkdir(parents=True, exist_ok=True)
    logfile = config.log_dir / f"{actual_run_id}.log"
    with logfile.open("wb") as out:
        completed = subprocess.run(command, cwd=config.repo_path, env=env, stdout=out, stderr=subprocess.STDOUT, check=False)
    sleep_robot(config, authorized=True)
    status = "ok" if completed.returncode == 0 else "failed"
    save_latest_run(
        config,
        RunnerState(
            pid=0,
            run_id=actual_run_id,
            log_path=logfile,
            artifact_root=config.artifact_root,
            started_at=datetime.now().isoformat(timespec="seconds"),
            requested_config={
                "scripted_policy_flow": flow,
                "profile_owned_context": config.profile_owned_context,
            },
            command=tuple(command),
        ),
    )
    return ActionResult(
        action=f"preflight.policy_{flow}",
        status=status,
        safety="physical",
        authorization_required=True,
        authorized=True,
        changed=True,
        machine_verification=(
            Verification("process_completed", status, {"returncode": completed.returncode, "run_id": actual_run_id}),
        ),
        human_quality_gate=HumanQualityGate(
            required=False,
            prompt="Recommended before live testing: confirm the policy speech sounded acceptable.",
        ),
        data={"run_id": actual_run_id, "log_path": logfile, "flow": flow},
    )


def full_preflight(config: OpsConfig, *, authorized: bool, sleep_fn=time.sleep) -> list[ActionResult]:
    _require_physical_authorization("preflight", authorized)
    results = [preflight_audio_playback(config, authorized=True)]
    sleep_fn(config.preflight_between_probes_gap_s)
    results.append(preflight_policy(config, authorized=True, flow="goodbye"))
    sleep_fn(config.policy_preflight_gap_s)
    results.append(preflight_policy(config, authorized=True, flow="greet"))
    return results


def start_session(config: OpsConfig, *, authorized: bool) -> list[ActionResult]:
    _require_physical_authorization("session.start", authorized)
    return [
        stop_runner(config, authorized=True, include_unmanaged=True),
        start_backend(config),
        wake_robot(config, authorized=True),
        start_runner(config, authorized=True),
    ]


def start_session_with_options(
    config: OpsConfig,
    *,
    authorized: bool,
    record_audio: bool | None = None,
    record_video: bool | None = None,
    capture_vision: bool | None = None,
    vision_pipelines_config: Path | None = None,
    rerun_mode: str | None = None,
) -> list[ActionResult]:
    _require_physical_authorization("session.start", authorized)
    return [
        stop_runner(config, authorized=True, include_unmanaged=True),
        start_backend(config),
        wake_robot(config, authorized=True),
        start_runner(
            config,
            authorized=True,
            record_audio=record_audio,
            record_video=record_video,
            capture_vision=capture_vision,
            vision_pipelines_config=vision_pipelines_config,
            rerun_mode=rerun_mode,
        ),
    ]


def stop_session(config: OpsConfig, *, authorized: bool) -> list[ActionResult]:
    _require_physical_authorization("session.stop", authorized)
    stopped = stop_runner(config, authorized=True, include_unmanaged=True)
    if stopped.data.get("supervised_cleanup"):
        return [stopped]
    return [stopped, sleep_robot(config, authorized=True)]


def aggregate_status(config: OpsConfig, *, include_robot: bool = False) -> ActionResult:
    backend = backend_status(config)
    runner = runner_status(config)
    external = external_services_status(config)
    storage = storage_status(config)
    latest = load_latest_run(config)
    data: dict[str, Any] = {
        "backend": backend.to_dict(),
        "runner": runner.to_dict(),
        "external_services": external.to_dict(),
        "storage": storage.to_dict(),
        "latest_run": latest,
    }
    checks = [
        Verification("backend", backend.status, backend.data),
        Verification("runner", runner.status, runner.data),
        Verification("external_services", external.status, external.data),
        Verification("storage", storage.status, storage.data),
    ]
    errors = list(backend.errors) + list(runner.errors) + list(external.errors) + list(storage.errors)
    if include_robot:
        robot = robot_status(config)
        data["robot"] = robot.to_dict()
        checks.append(Verification("robot", robot.status, robot.data))
        errors.extend(robot.errors)
    status = "ok" if not errors else "degraded"
    return ActionResult(
        action="status",
        status=status,
        safety="read_only",
        machine_verification=tuple(checks),
        data=data,
        errors=tuple(errors),
    )


def build_audio_playback_command(config: OpsConfig, wav_path: Path, *, run_id: str | None = None) -> tuple[list[str], dict[str, str]]:
    env = _base_env(config)
    env.update(
        {
            "HF_REALTIME_WS_URL": f"ws://{config.s2s_host}:{config.s2s_port}/v1/realtime",
            "REACHY_HOST": config.robot_host,
        }
    )
    actual_run_id = run_id or f"official-audio-preflight-{_timestamp()}"
    return [
        str(config.repo_path / "scripts" / "m1max" / "run_official_runtime_live.sh"),
        "--run-id",
        actual_run_id,
        "--artifact-root",
        str(config.artifact_root),
        "--duration",
        "30",
        "--robot-host",
        config.robot_host,
        "--warmup-audio",
        "--no-warmup-video",
        "--record-audio",
        "--no-record-video",
        "--no-capture-vision",
        "--no-perception",
        "--no-gestures",
        "--no-audio-gate",
        "--no-ready-cue",
        "--no-conversation-cues",
        "--visitor-trigger-profile",
        LEGACY_VISITOR_TRIGGER_PROFILE,
        "--scripted-playback-wav",
        str(wav_path),
        "--scripted-playback-post-roll-s",
        "3.0",
    ], env


def build_live_command(
    config: OpsConfig,
    *,
    run_id: str,
    duration_s: float,
    perception: bool,
    gestures: bool,
    audio_gate: bool,
    ready_cue: bool,
    warmup_video: bool,
    conversation_cues: bool,
    capture_vision: bool,
    record_audio: bool,
    record_video: bool,
    scripted_policy_flow: str = "none",
    scripted_policy_gap_s: float | None = None,
    scripted_policy_timeout_s: float | None = None,
    scripted_policy_greeting: str | None = None,
    vision_pipelines_config: Path | None = None,
    rerun_mode: str | None = None,
    heartbeat_path: Path | None = None,
    heartbeat_interval_s: float = 1.0,
    visitor_trigger_profile: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    env = _base_env(config)
    env.update(
        {
            "HF_REALTIME_CONNECTION_MODE": "local",
            "HF_REALTIME_WS_URL": f"ws://{config.s2s_host}:{config.s2s_port}/v1/realtime",
            "REACHY_HOST": config.robot_host,
        }
    )
    resolved_visitor_trigger_profile = visitor_trigger_profile or config.visitor_trigger_profile
    command = [
        str(config.python_bin),
        "-m",
        "reachy_mini_brain.official_runtime.live_app",
        "--backend",
        "s2s-local",
        "--hf-realtime-ws-url",
        f"ws://{config.s2s_host}:{config.s2s_port}/v1/realtime",
        "--run-id",
        run_id,
        "--duration",
        str(duration_s),
        "--robot-host",
        config.robot_host,
        "--ready-cue" if ready_cue else "--no-ready-cue",
        "--warmup-video" if warmup_video else "--no-warmup-video",
        "--perception" if perception else "--no-perception",
        "--gestures" if gestures else "--no-gestures",
        "--audio-gate" if audio_gate else "--no-audio-gate",
        "--conversation-cues" if conversation_cues else "--no-conversation-cues",
        "--record-audio" if record_audio else "--no-record-audio",
        "--record-video" if record_video else "--no-record-video",
        "--capture-vision" if capture_vision else "--no-capture-vision",
        "--visitor-trigger-profile",
        resolved_visitor_trigger_profile,
        "--vision-runtime",
        config.vision_runtime,
        "--broker-capture-fps",
        str(config.broker_capture_fps),
        "--broker-recorder-queue-size",
        str(config.broker_recorder_queue_size),
        "--broker-gesture-queue-size",
        str(config.broker_gesture_queue_size),
        "--broker-policy-idle-s",
        str(config.broker_policy_idle_s),
        "--gesture-running-mode",
        config.gesture_running_mode,
        "--wave-detection-mode",
        config.wave_detection_mode,
        "--rerun-mode",
        rerun_mode or config.rerun_mode,
        "--rerun-grpc-url",
        config.rerun_grpc_url,
        "--rerun-image-fps",
        str(config.rerun_image_fps),
        "--rerun-jpeg-quality",
        str(config.rerun_jpeg_quality),
        "--rerun-queue-size",
        str(config.rerun_queue_size),
    ]
    if heartbeat_path is not None:
        command.extend(
            [
                "--heartbeat-path",
                str(heartbeat_path),
                "--heartbeat-interval-s",
                str(heartbeat_interval_s),
            ]
        )
    resolved_vision_config = vision_pipelines_config or config.vision_pipelines_config
    if resolved_vision_config is not None:
        command.extend(["--vision-pipelines-config", str(resolved_vision_config)])
    if config.profile_owned_context:
        command.append("--profile-owned-context")
    if config.agent_profile_id:
        if config.profile_owned_context:
            raise OpsError(
                "RECEPTION_AGENT_PROFILE_ID cannot be combined with "
                "S2S_RESPONSES_CONVERSATION=1"
            )
        command.extend(["--agent-profile-id", config.agent_profile_id])
    if scripted_policy_flow != "none":
        command.extend(["--scripted-policy-flow", scripted_policy_flow])
        if scripted_policy_gap_s is not None:
            command.extend(["--scripted-policy-gap-s", str(scripted_policy_gap_s)])
        if scripted_policy_timeout_s is not None:
            command.extend(["--scripted-policy-timeout-s", str(scripted_policy_timeout_s)])
        if scripted_policy_greeting is not None:
            command.extend(["--scripted-policy-greeting", scripted_policy_greeting])
    return command, env


def save_runner_state(config: OpsConfig, state: RunnerState) -> None:
    _atomic_write_json(config.runner_state_path, state.to_dict())


def load_runner_state(config: OpsConfig) -> RunnerState | None:
    if not config.runner_state_path.exists():
        return None
    try:
        return RunnerState.from_dict(json.loads(config.runner_state_path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise OpsError(f"invalid runner state file {config.runner_state_path}: {exc}") from exc


def save_latest_run(config: OpsConfig, state: RunnerState) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": state.run_id,
        "artifact_root": str(state.artifact_root),
        "manifest_path": str(state.manifest_path),
        "log_path": str(state.log_path),
        "terminal_status_path": (
            str(state.terminal_status_path) if state.terminal_status_path is not None else None
        ),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write_json(config.latest_run_path, payload)


def load_latest_run(config: OpsConfig) -> dict[str, Any] | None:
    if not config.latest_run_path.exists():
        return None
    try:
        return json.loads(config.latest_run_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpsError(f"invalid latest-run file {config.latest_run_path}: {exc}") from exc


def _load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(_jsonable(payload), indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _active_runner_health_fault(
    config: OpsConfig,
    state: RunnerState,
    heartbeat: dict[str, Any] | None,
) -> str | None:
    from .session_supervisor import HealthThresholds, evaluate_heartbeat

    now_monotonic = time.monotonic()
    if heartbeat is not None and isinstance(heartbeat.get("started_monotonic"), (int, float)):
        started_monotonic = float(heartbeat["started_monotonic"])
    else:
        try:
            started_wall = datetime.fromisoformat(state.started_at).timestamp()
        except ValueError:
            started_wall = time.time()
        started_monotonic = now_monotonic - max(0.0, time.time() - started_wall)
    return evaluate_heartbeat(
        heartbeat,
        now_monotonic=now_monotonic,
        supervisor_started_monotonic=started_monotonic,
        thresholds=HealthThresholds(
            startup_grace_s=config.media_startup_grace_s,
            heartbeat_stale_s=config.media_heartbeat_stale_s,
            source_stale_s=config.media_source_stale_s,
            event_loop_stale_s=config.event_loop_stale_s,
        ),
    )


def _launch_background(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    logfile: Path,
    keep_awake: bool,
) -> tuple[subprocess.Popen[bytes], int | None]:
    """Launch a long-running process so it survives SSH shell exit.

    The process is started in a new session, with stdout/stderr redirected to a
    file. On macOS, a separate `caffeinate -w <pid>` watcher prevents system
    sleep while that process remains alive without replacing the real child PID
    we store in RunnerState.
    """

    with logfile.open("ab") as out:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    caffeinate_pid = _start_caffeinate_watcher(proc.pid) if keep_awake else None
    return proc, caffeinate_pid


def _start_caffeinate_watcher(pid: int) -> int | None:
    executable = shutil.which("caffeinate")
    if executable is None:
        return None
    try:
        watcher = subprocess.Popen(
            [executable, "-dimsu", "-w", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    return watcher.pid


def _validate_backend_launch_paths(config: OpsConfig) -> list[str]:
    script = config.repo_path / "scripts" / "m1max" / "run_s2s_backend.sh"
    errors = _validate_repo_path(config)
    if not script.exists():
        errors.append(f"missing backend launch script: {script}")
    return errors


def _validate_live_launch_paths(config: OpsConfig) -> list[str]:
    errors = _validate_repo_path(config)
    errors.extend(_validate_python_path(config))
    return errors


def _validate_audio_playback_launch_paths(config: OpsConfig) -> list[str]:
    errors = _validate_repo_path(config)
    errors.extend(_validate_python_path(config))
    script = config.repo_path / "scripts" / "m1max" / "run_official_runtime_live.sh"
    if not script.exists():
        errors.append(f"missing live runner script: {script}")
    return errors


def _validate_repo_path(config: OpsConfig) -> list[str]:
    if not config.repo_path.exists():
        return [f"missing repo path: {config.repo_path}"]
    return []


def _validate_python_path(config: OpsConfig) -> list[str]:
    if not config.python_bin.exists():
        return [f"missing Python executable: {config.python_bin}"]
    return []


def _base_env(config: OpsConfig) -> dict[str, str]:
    env = clean_gstreamer_environment()
    python_paths = [str(config.repo_path / "src")]
    gi_path = _gstreamer_python_path_for_python(config.python_bin)
    if gi_path is not None:
        python_paths.append(str(gi_path))
    env["PYTHONPATH"] = ":".join(python_paths)
    env["REACHY_REPO"] = str(config.repo_path)
    env["ENV_FILE"] = str(config.repo_path / ".env")
    return env


def _gstreamer_python_path_for_python(python_bin: Path) -> Path | None:
    venv_root = python_bin.expanduser().parent.parent
    return _gstreamer_python_path_for_venv(venv_root)


def _gstreamer_python_path_for_repo(repo_path: Path) -> Path | None:
    return _gstreamer_python_path_for_venv(repo_path.expanduser() / ".venv")


def _gstreamer_python_path_for_venv(venv_root: Path) -> Path | None:
    for candidate in venv_root.glob("lib/python*/site-packages/gstreamer_python/lib/python*/site-packages"):
        if candidate.is_dir():
            return candidate
    return None


def _default_python_bin(*, repo_path: Path) -> Path:
    configured = os.environ.get("OFFICIAL_RUNTIME_PYTHON")
    if configured:
        return Path(configured).expanduser()
    repo_python = repo_path / ".venv" / "bin" / "python"
    if repo_python.exists():
        return repo_python
    return Path(sys.executable)


def _robot_get(config: OpsConfig, path: str, *, timeout_s: float = 8.0) -> Any:
    return _robot_request(config, "GET", path, timeout_s=timeout_s)


def _robot_post(
    config: OpsConfig,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    tolerate_errors: bool = False,
    timeout_s: float = 8.0,
) -> Any:
    try:
        return _robot_request(config, "POST", path, json_body=json_body, timeout_s=timeout_s)
    except OpsError:
        if tolerate_errors:
            return None
        raise


def _robot_request(
    config: OpsConfig,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout_s: float = 8.0,
) -> Any:
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    request = Request(f"{config.robot_api}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=timeout_s) as response:
            body = response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise OpsError(f"{method} {path}: HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OpsError(f"{method} {path}: {exc}") from exc
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace")


def _json_health_request(
    url: str,
    *,
    timeout_s: float,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    request = Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if bearer_token:
        request.add_header("Authorization", f"Bearer {bearer_token}")
    try:
        with urlopen(request, timeout=timeout_s) as response:
            status = int(getattr(response, "status", 200))
            body_bytes = response.read()
    except HTTPError as exc:
        return {"ok": False, "http_status": exc.code, "error": f"HTTP {exc.code}"}
    except (URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "http_status": None, "error": str(exc)}
    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        return {"ok": False, "http_status": status, "error": "response was not JSON"}
    return {"ok": 200 <= status < 300, "http_status": status, "body": body}


def _launchd_target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def _launch_agent_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _start_launchd_service(label: str, plist_path: Path) -> dict[str, Any] | None:
    if sys.platform != "darwin" or shutil.which("launchctl") is None:
        return None
    status = launchd_service_status(label)
    if status["status"] != "loaded":
        if not plist_path.is_file():
            return None
        bootstrap = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
        if bootstrap.returncode != 0:
            raise OpsError(
                f"could not bootstrap {label}: {bootstrap.stderr.strip() or bootstrap.stdout.strip()}"
            )
    kickstart = _launchctl("kickstart", "-k", _launchd_target(label))
    if kickstart.returncode != 0:
        raise OpsError(
            f"could not kickstart {label}: {kickstart.stderr.strip() or kickstart.stdout.strip()}"
        )
    return {"label": label, "mode": "launchd", "plist": str(plist_path)}


def _backend_pattern(config: OpsConfig) -> str:
    if config.s2s_cli_mode == "serve":
        return rf"speech-to-speech serve.*--port {config.s2s_port}( |$)"
    return BACKEND_PATTERN


def _wait_for_managed_backend_stopped(
    config: OpsConfig,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        service = launchd_service_status(config.s2s_service_label)
        port_live = _port_open(config.s2s_host, config.s2s_port)
        pids = _find_pids(_backend_pattern(config))
        if service["status"] != "loaded" and not port_live and not pids:
            return {
                "service_status": service["status"],
                "port_live": False,
                "pids": pids,
            }
        if time.monotonic() >= deadline:
            return {
                "service_status": service["status"],
                "port_live": port_live,
                "pids": pids,
            }
        time.sleep(0.1)


def _require_physical_authorization(action: str, authorized: bool) -> None:
    if not authorized:
        raise AuthorizationError(
            f"{action} is a physical robot action. Re-run with --confirm-physical after the user approves it."
        )


def _find_pids(pattern: str) -> list[int]:
    completed = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    if completed.returncode not in {0, 1}:
        return []
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != os.getpid():
            pids.append(pid)
    return pids


def _terminate_pids(pids: list[int], *, grace_s: float = 2.0) -> list[int]:
    requested = [pid for pid in pids if pid > 0]
    for pid in requested:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in requested):
            break
        time.sleep(0.1)
    for pid in requested:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return requested


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _port_open(host: str, port: int, *, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value
