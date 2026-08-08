"""Detached lifecycle supervisor for one official-runtime live child."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HealthThresholds:
    startup_grace_s: float = 120.0
    heartbeat_stale_s: float = 5.0
    source_stale_s: float = 8.0
    event_loop_stale_s: float = 8.0


def evaluate_heartbeat(
    heartbeat: dict[str, Any] | None,
    *,
    now_monotonic: float,
    supervisor_started_monotonic: float,
    thresholds: HealthThresholds,
) -> str | None:
    """Return a terminal health fault or ``None`` while the child is healthy."""

    startup_age = now_monotonic - supervisor_started_monotonic
    if heartbeat is None:
        if startup_age > thresholds.startup_grace_s:
            return "heartbeat_missing"
        return None

    updated = _number(heartbeat.get("updated_monotonic"))
    if updated is None:
        return "heartbeat_invalid"
    if now_monotonic - updated > thresholds.heartbeat_stale_s:
        return "heartbeat_stale"

    phase = str(heartbeat.get("phase") or "")
    if phase == "failed":
        return f"runtime_failed:{heartbeat.get('fault') or 'unknown'}"
    if phase in {"stopping", "stopped"}:
        return None
    if phase != "ready":
        if startup_age > thresholds.startup_grace_s:
            return f"startup_stalled:{phase or 'unknown'}"
        return None

    loop_age = _number(heartbeat.get("event_loop_age_s"))
    if loop_age is None or loop_age > thresholds.event_loop_stale_s:
        return "event_loop_stale"

    for source_name in ("audio", "video"):
        source = heartbeat.get(source_name)
        if not isinstance(source, dict) or not source.get("expected"):
            continue
        sequence = source.get("sequence")
        age = _number(source.get("age_s"))
        if not isinstance(sequence, int) or sequence <= 0:
            ready_at = _number(heartbeat.get("ready_monotonic"))
            if ready_at is None or now_monotonic - ready_at > thresholds.source_stale_s:
                return f"{source_name}_never_started"
            continue
        if age is None or age > thresholds.source_stale_s:
            return f"{source_name}_stale"
    return None


def supervise(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    started_monotonic = time.monotonic()
    stop_requested = False
    stop_signal: int | None = None

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal stop_requested, stop_signal
        stop_requested = True
        stop_signal = signum

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    log_path = Path(spec["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("ab") as output:
            child = subprocess.Popen(
                [str(item) for item in spec["command"]],
                cwd=spec["cwd"],
                env=os.environ.copy(),
                stdout=output,
                stderr=subprocess.STDOUT,
            )
    except Exception as exc:
        cleanup_status = _cleanup_robot(spec)
        _atomic_write_json(
            Path(spec["terminal_status_path"]),
            {
                "schema_version": 1,
                "run_id": spec["run_id"],
                "status": (
                    "cleanup_incomplete" if cleanup_status["status"] != "ok" else "failed"
                ),
                "reason": "child_launch_failed",
                "fault": repr(exc),
                "supervisor_pid": os.getpid(),
                "runner_pid": None,
                "runner_returncode": None,
                "forced_kill": False,
                "started_at": spec["started_at"],
                "ended_at": datetime.now().isoformat(timespec="seconds"),
                "heartbeat": _load_json(Path(spec["heartbeat_path"])),
                "artifacts": inspect_artifacts(Path(spec["manifest_path"])),
                "cleanup": cleanup_status,
            },
        )
        _retire_active_state(Path(spec["state_path"]), supervisor_pid=os.getpid())
        return 1
    _update_active_state(Path(spec["state_path"]), supervisor_pid=os.getpid(), runner_pid=child.pid)

    thresholds = HealthThresholds(**dict(spec.get("thresholds") or {}))
    poll_interval_s = float(spec.get("poll_interval_s", 1.0))
    heartbeat_path = Path(spec["heartbeat_path"])
    reason = "completed"
    fault: str | None = None

    while child.poll() is None:
        if stop_requested:
            reason = "requested_stop"
            break
        heartbeat = _load_json(heartbeat_path)
        fault = evaluate_heartbeat(
            heartbeat,
            now_monotonic=time.monotonic(),
            supervisor_started_monotonic=started_monotonic,
            thresholds=thresholds,
        )
        if fault is not None:
            reason = "media_liveness_fault"
            break
        time.sleep(poll_interval_s)

    forced_kill = False
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=float(spec.get("runner_stop_grace_s", 30.0)))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            forced_kill = True
    returncode = child.returncode
    if reason == "completed" and returncode not in (0, None):
        reason = "child_failed"

    artifact_status = inspect_artifacts(Path(spec["manifest_path"]))
    cleanup_status = _cleanup_robot(spec)
    terminal_status = "complete"
    if reason in {"media_liveness_fault", "child_failed"} or forced_kill:
        terminal_status = "failed"
    elif artifact_status["status"] != "closed":
        terminal_status = "interrupted"
    if cleanup_status["status"] != "ok":
        terminal_status = "cleanup_incomplete"

    terminal = {
        "schema_version": 1,
        "run_id": spec["run_id"],
        "status": terminal_status,
        "reason": reason,
        "fault": fault,
        "stop_signal": stop_signal,
        "supervisor_pid": os.getpid(),
        "runner_pid": child.pid,
        "runner_returncode": returncode,
        "forced_kill": forced_kill,
        "started_at": spec["started_at"],
        "ended_at": datetime.now().isoformat(timespec="seconds"),
        "heartbeat": _load_json(heartbeat_path),
        "artifacts": artifact_status,
        "cleanup": cleanup_status,
    }
    _atomic_write_json(Path(spec["terminal_status_path"]), terminal)
    _retire_active_state(Path(spec["state_path"]), supervisor_pid=os.getpid())
    return 0 if terminal_status == "complete" else 1


def inspect_artifacts(manifest_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    if manifest is None:
        return {"status": "missing", "manifest_path": str(manifest_path)}
    open_artifacts: list[str] = []
    artifacts = manifest.get("artifacts")
    if isinstance(artifacts, dict):
        for records in artifacts.values():
            if not isinstance(records, list):
                continue
            for record in records:
                if isinstance(record, dict) and record.get("status") == "open":
                    open_artifacts.append(str(record.get("path") or "unknown"))
    return {
        "status": (
            "closed"
            if not open_artifacts and manifest.get("ended_ts") is not None
            else "interrupted"
        ),
        "manifest_path": str(manifest_path),
        "open_artifacts": open_artifacts,
        "ended_ts": manifest.get("ended_ts"),
    }


def _cleanup_robot(spec: dict[str, Any]) -> dict[str, Any]:
    try:
        from .ops_core import OpsConfig, finalize_robot_after_run

        config = OpsConfig.from_env()
        config = replace(
            config,
            robot_host=str(spec.get("robot_host") or config.robot_host),
            robot_port=int(spec.get("robot_port") or config.robot_port),
        )
        result = finalize_robot_after_run(
            config,
            attempts=int(spec.get("cleanup_attempts", 2)),
            request_timeout_s=float(spec.get("cleanup_request_timeout_s", 2.0)),
        )
        return result.to_dict()
    except Exception as exc:
        return {
            "action": "robot.finalize_after_run",
            "status": "failed",
            "changed": False,
            "errors": [repr(exc)],
        }


def _update_active_state(path: Path, *, supervisor_pid: int, runner_pid: int) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        data = _load_json(path)
        if data is not None and int(data.get("pid", -1)) == supervisor_pid:
            data["runner_pid"] = runner_pid
            _atomic_write_json(path, data)
            return
        time.sleep(0.05)


def _retire_active_state(path: Path, *, supervisor_pid: int) -> None:
    data = _load_json(path)
    if data is None or int(data.get("pid", -1)) != supervisor_pid:
        return
    path.unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    return supervise(args.spec)


if __name__ == "__main__":
    raise SystemExit(main())
