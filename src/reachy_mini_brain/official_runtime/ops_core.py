"""Operational control primitives for the official-runtime live path.

The functions in this module are intentionally UI-agnostic: they return
structured results and do not print. The dev CLI, a future app, and tests can
all call the same action layer.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .env import PROJECT_ROOT, load_project_env


LIVE_PATTERN = "reachy_mini_brain.official_runtime.live_app"
BACKEND_PATTERN = "speech-to-speech --mode realtime"
DEFAULT_PREFLIGHT_WAV = (
    "audio-response-resp_db3304df3e804556b0aaa7ed7990048f-"
    "official-live-20260623-122844-01-pcm16.wav"
)
DEFAULT_POLICY_PREFLIGHT_GREETING = "Welcome to the clinic. How can I help you today?"


class OpsError(RuntimeError):
    """Base error for ops failures."""


class AuthorizationError(OpsError):
    """Raised when a physical action is requested without authorization."""


@dataclass(frozen=True)
class OpsConfig:
    """Configuration for m1max/robot ops actions."""

    repo_path: Path
    official_app_repo: Path
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
    python_bin: Path
    backend_start_timeout_s: float
    keep_awake: bool

    @classmethod
    def from_env(cls) -> "OpsConfig":
        load_project_env()
        repo_path = Path(os.environ.get("REACHY_REPO", str(PROJECT_ROOT))).expanduser()
        official_app_repo = Path(
            os.environ.get("OFFICIAL_APP_REPO", "/Users/leon/projects/reachy_mini_conversation_app")
        ).expanduser()
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
        python_bin = _default_python_bin(repo_path=repo_path, official_app_repo=official_app_repo)
        return cls(
            repo_path=repo_path,
            official_app_repo=official_app_repo,
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
            python_bin=python_bin,
            backend_start_timeout_s=float(os.environ.get("BACKEND_START_TIMEOUT", "45")),
            keep_awake=_env_bool("OPS_KEEP_AWAKE", default=True),
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "run_id": self.run_id,
            "log_path": str(self.log_path),
            "artifact_root": str(self.artifact_root),
            "started_at": self.started_at,
            "requested_config": _jsonable(self.requested_config),
            "command": list(self.command),
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
        )

    @property
    def manifest_path(self) -> Path:
        return self.artifact_root / "runs" / f"run-{self.run_id}.json"


def backend_status(config: OpsConfig) -> ActionResult:
    port_live = _port_open(config.s2s_host, config.s2s_port)
    pids = _find_pids(BACKEND_PATTERN)
    status = "ok" if port_live else "stopped"
    if pids and not port_live:
        status = "degraded"
    return ActionResult(
        action="backend.status",
        status=status,
        machine_verification=(
            Verification("tcp_port", "ok" if port_live else "failed", {"host": config.s2s_host, "port": config.s2s_port}),
            Verification("process", "ok" if pids else "not_found", {"pids": pids}),
        ),
        data={"host": config.s2s_host, "port": config.s2s_port, "port_live": port_live, "pids": pids},
    )


def start_backend(config: OpsConfig) -> ActionResult:
    status_before = backend_status(config)
    if status_before.data["port_live"]:
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
    pids = _find_pids(BACKEND_PATTERN)
    stopped = _terminate_pids(pids)
    return ActionResult(
        action="backend.stop",
        status="ok",
        changed=bool(stopped),
        machine_verification=(Verification("process_terminated", "ok", {"pids": stopped}),),
        data={"requested_pids": pids, "stopped_pids": stopped},
    )


def restart_backend(config: OpsConfig) -> list[ActionResult]:
    return [stop_backend(config), start_backend(config)]


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
    data: dict[str, Any] = {"live_pids": live_pids, "state_file": config.runner_state_path}
    checks: list[Verification] = [Verification("process_scan", "ok" if live_pids else "not_found", {"pids": live_pids})]
    status = "stopped"
    errors: list[str] = []
    if state is not None:
        alive = _pid_alive(state.pid)
        manifest_exists = state.manifest_path.exists()
        data["state"] = state.to_dict()
        data["pid_alive"] = alive
        data["manifest_exists"] = manifest_exists
        checks.append(Verification("runner_state_pid", "ok" if alive else "stale", {"pid": state.pid}))
        checks.append(Verification("run_manifest", "ok" if manifest_exists else "missing", {"path": state.manifest_path}))
        if alive:
            status = "running"
        else:
            status = "stale_state"
            errors.append("runner state file points to a non-running PID")
    elif live_pids:
        status = "unmanaged_running"
        errors.append("live runner process exists without an ops state file")
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
) -> ActionResult:
    _require_physical_authorization("runner.start", authorized)
    existing = runner_status(config)
    if existing.status == "running":
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
        scripted_policy_flow="none",
    )
    proc, caffeinate_pid = _launch_background(command, cwd=config.repo_path, env=env, logfile=logfile, keep_awake=config.keep_awake)
    state = RunnerState(
        pid=proc.pid,
        run_id=actual_run_id,
        log_path=logfile,
        artifact_root=config.artifact_root,
        started_at=datetime.now().isoformat(timespec="seconds"),
        requested_config={
            "duration_s": duration_s or config.live_duration_s,
            "perception": perception,
            "gestures": gestures,
            "audio_gate": audio_gate,
            "ready_cue": ready_cue,
            "warmup_video": warmup_video,
            "conversation_cues": config.conversation_cues if conversation_cues is None else conversation_cues,
            "capture_vision": config.capture_vision if capture_vision is None else capture_vision,
            "keep_awake": config.keep_awake,
            "caffeinate_pid": caffeinate_pid,
        },
        command=tuple(command),
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
        machine_verification=(Verification("process_started", "ok", {"pid": proc.pid, "caffeinate_pid": caffeinate_pid}),),
        data={**state.to_dict(), "caffeinate_pid": caffeinate_pid},
    )


def stop_runner(config: OpsConfig, *, authorized: bool, include_unmanaged: bool = False) -> ActionResult:
    _require_physical_authorization("runner.stop", authorized)
    state = load_runner_state(config)
    pids: list[int] = []
    if state is not None and _pid_alive(state.pid):
        pids.append(state.pid)
    if include_unmanaged:
        for pid in _find_pids(LIVE_PATTERN):
            if pid not in pids:
                pids.append(pid)
    stopped = _terminate_pids(pids)
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
        data={"requested_pids": pids, "stopped_pids": stopped},
    )


def shutdown(config: OpsConfig, *, authorized: bool) -> list[ActionResult]:
    _require_physical_authorization("shutdown", authorized)
    return [
        stop_runner(config, authorized=True, include_unmanaged=True),
        sleep_robot(config, authorized=True),
    ]


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
        scripted_policy_flow=flow,
        scripted_policy_gap_s=config.policy_preflight_gap_s,
        scripted_policy_timeout_s=config.policy_preflight_timeout_s,
        scripted_policy_greeting=config.policy_preflight_greeting if "greet" in flow else None,
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
            requested_config={"scripted_policy_flow": flow},
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


def stop_session(config: OpsConfig, *, authorized: bool) -> list[ActionResult]:
    _require_physical_authorization("session.stop", authorized)
    return [
        stop_runner(config, authorized=True, include_unmanaged=True),
        sleep_robot(config, authorized=True),
    ]


def aggregate_status(config: OpsConfig, *, include_robot: bool = False) -> ActionResult:
    backend = backend_status(config)
    runner = runner_status(config)
    latest = load_latest_run(config)
    data: dict[str, Any] = {
        "backend": backend.to_dict(),
        "runner": runner.to_dict(),
        "latest_run": latest,
    }
    checks = [
        Verification("backend", backend.status, backend.data),
        Verification("runner", runner.status, runner.data),
    ]
    errors = list(backend.errors) + list(runner.errors)
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
            "REACHY_MINI_CONVERSATION_APP_SRC": str(config.official_app_repo / "src"),
            "HF_REALTIME_CONNECTION_MODE": "local",
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
    scripted_policy_flow: str = "none",
    scripted_policy_gap_s: float | None = None,
    scripted_policy_timeout_s: float | None = None,
    scripted_policy_greeting: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    env = _base_env(config)
    env.update(
        {
            "REACHY_MINI_CONVERSATION_APP_SRC": str(config.official_app_repo / "src"),
            "HF_REALTIME_CONNECTION_MODE": "local",
            "HF_REALTIME_WS_URL": f"ws://{config.s2s_host}:{config.s2s_port}/v1/realtime",
            "REACHY_HOST": config.robot_host,
        }
    )
    command = [
        str(config.python_bin),
        "-m",
        "reachy_mini_brain.official_runtime.live_app",
        "--backend",
        "hf-official",
        "--hf-connection-mode",
        "local",
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
        "--capture-vision" if capture_vision else "--no-capture-vision",
    ]
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
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.runner_state_path.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")


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
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    config.latest_run_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_latest_run(config: OpsConfig) -> dict[str, Any] | None:
    if not config.latest_run_path.exists():
        return None
    try:
        return json.loads(config.latest_run_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpsError(f"invalid latest-run file {config.latest_run_path}: {exc}") from exc


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
    official_src = config.official_app_repo / "src"
    if not official_src.exists():
        errors.append(f"missing official app source directory: {official_src}")
    return errors


def _validate_audio_playback_launch_paths(config: OpsConfig) -> list[str]:
    errors = _validate_repo_path(config)
    errors.extend(_validate_python_path(config))
    script = config.repo_path / "scripts" / "m1max" / "run_official_runtime_live.sh"
    if not script.exists():
        errors.append(f"missing live runner script: {script}")
    official_src = config.official_app_repo / "src"
    if not official_src.exists():
        errors.append(f"missing official app source directory: {official_src}")
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
    env = os.environ.copy()
    env.pop("GI_TYPELIB_PATH", None)
    env.pop("GST_PLUGIN_PATH", None)
    env.pop("GST_PLUGIN_SCANNER_1_0", None)
    python_paths = [str(config.repo_path / "src")]
    gi_path = _gstreamer_python_path_for_python(config.python_bin)
    if gi_path is None:
        gi_path = _gstreamer_python_path_for_repo(config.official_app_repo)
    if gi_path is not None:
        python_paths.append(str(gi_path))
    env["PYTHONPATH"] = ":".join(python_paths)
    env["REACHY_REPO"] = str(config.repo_path)
    env["OFFICIAL_APP_REPO"] = str(config.official_app_repo)
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


def _default_python_bin(*, repo_path: Path, official_app_repo: Path) -> Path:
    configured = os.environ.get("OFFICIAL_RUNTIME_PYTHON")
    if configured:
        return Path(configured).expanduser()
    repo_python = repo_path / ".venv" / "bin" / "python"
    if repo_python.exists():
        return repo_python
    official_python = official_app_repo / ".venv" / "bin" / "python"
    if official_python.exists():
        return official_python
    return Path(sys.executable)


def _robot_get(config: OpsConfig, path: str) -> Any:
    return _robot_request(config, "GET", path)


def _robot_post(
    config: OpsConfig,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    tolerate_errors: bool = False,
) -> Any:
    try:
        return _robot_request(config, "POST", path, json_body=json_body)
    except OpsError:
        if tolerate_errors:
            return None
        raise


def _robot_request(config: OpsConfig, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    request = Request(f"{config.robot_api}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=8) as response:
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
