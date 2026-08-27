from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reachy_mini_brain.official_runtime import session_supervisor
from reachy_mini_brain.official_runtime.liveness import HeartbeatWriter, RuntimeLiveness
from reachy_mini_brain.official_runtime.session_supervisor import (
    HealthThresholds,
    evaluate_heartbeat,
    inspect_artifacts,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_liveness_counts_sources_without_artifact_recording(tmp_path: Path) -> None:
    monotonic = FakeClock()
    wall = FakeClock(1_800_000_000.0)
    liveness = RuntimeLiveness(
        run_id="live-test",
        audio_expected=True,
        video_expected=True,
        monotonic=monotonic,
        wall_clock=wall,
    )
    liveness.set_phase("ready")
    liveness.pulse_event_loop()
    liveness.audio_frame()
    liveness.video_frame()
    monotonic.value += 0.25

    path = tmp_path / "heartbeat.json"
    writer = HeartbeatWriter(path, liveness, interval_s=10.0)
    writer.start()
    writer.close()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["phase"] == "ready"
    assert payload["audio"]["sequence"] == 1
    assert payload["video"]["sequence"] == 1
    assert payload["audio"]["age_s"] == 0.25
    assert payload["event_loop_age_s"] == 0.25


def test_supervisor_allows_startup_grace_before_first_heartbeat() -> None:
    thresholds = HealthThresholds(startup_grace_s=120.0)

    assert evaluate_heartbeat(
        None,
        now_monotonic=119.0,
        supervisor_started_monotonic=0.0,
        thresholds=thresholds,
    ) is None
    assert evaluate_heartbeat(
        None,
        now_monotonic=121.0,
        supervisor_started_monotonic=0.0,
        thresholds=thresholds,
    ) == "heartbeat_missing"


def test_supervisor_faults_on_stale_required_source_only() -> None:
    heartbeat = {
        "updated_monotonic": 200.0,
        "phase": "ready",
        "ready_monotonic": 100.0,
        "event_loop_age_s": 0.1,
        "audio": {"expected": True, "sequence": 10, "age_s": 6.0},
        "video": {"expected": False, "sequence": 0, "age_s": None},
    }

    assert evaluate_heartbeat(
        heartbeat,
        now_monotonic=200.0,
        supervisor_started_monotonic=90.0,
        thresholds=HealthThresholds(source_stale_s=5.0),
    ) == "audio_stale"


def test_supervisor_faults_when_event_loop_stalls() -> None:
    heartbeat = {
        "updated_monotonic": 200.0,
        "phase": "ready",
        "ready_monotonic": 100.0,
        "event_loop_age_s": 6.0,
        "audio": {"expected": False, "sequence": 0, "age_s": None},
        "video": {"expected": False, "sequence": 0, "age_s": None},
    }

    assert evaluate_heartbeat(
        heartbeat,
        now_monotonic=200.0,
        supervisor_started_monotonic=90.0,
        thresholds=HealthThresholds(event_loop_stale_s=5.0),
    ) == "event_loop_stale"


@pytest.mark.parametrize("phase", ["stopping", "stopped"])
def test_supervisor_allows_terminal_heartbeat_phase(phase: str) -> None:
    heartbeat = {
        "updated_monotonic": 200.0,
        "phase": phase,
        "event_loop_age_s": 20.0,
        "audio": {"expected": True, "sequence": 10, "age_s": 20.0},
    }

    assert evaluate_heartbeat(
        heartbeat,
        now_monotonic=200.0,
        supervisor_started_monotonic=0.0,
        thresholds=HealthThresholds(startup_grace_s=1.0),
    ) is None


def test_artifact_inspection_distinguishes_closed_and_interrupted(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "ended_ts": 123.0,
                "artifacts": {"audio": [{"path": "audio.wav", "status": "closed"}]},
            }
        ),
        encoding="utf-8",
    )
    assert inspect_artifacts(manifest)["status"] == "closed"

    manifest.write_text(
        json.dumps(
            {
                "artifacts": {"audio": [{"path": "audio.wav", "status": "open"}]},
            }
        ),
        encoding="utf-8",
    )
    result = inspect_artifacts(manifest)
    assert result["status"] == "interrupted"
    assert result["open_artifacts"] == ["audio.wav"]


def test_supervisor_records_child_launch_failure_and_retires_active_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "runner-state.json"
    terminal_path = tmp_path / "terminal.json"
    spec_path = tmp_path / "spec.json"
    state_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    spec_path.write_text(
        json.dumps(
            {
                "run_id": "live-test",
                "started_at": "2026-08-07T12:00:00",
                "command": ["missing-command"],
                "cwd": str(tmp_path),
                "log_path": str(tmp_path / "run.log"),
                "state_path": str(state_path),
                "heartbeat_path": str(tmp_path / "heartbeat.json"),
                "terminal_status_path": str(terminal_path),
                "manifest_path": str(tmp_path / "manifest.json"),
            }
        ),
        encoding="utf-8",
    )
    def fail_launch(*args, **kwargs):
        assert "GST_REGISTRY_1_0" not in kwargs["env"]
        assert "GST_PLUGIN_PATH_1_0" not in kwargs["env"]
        assert kwargs["env"]["GST_DEBUG"] == "webrtc*:4"
        raise OSError("launch failed")

    monkeypatch.setenv("GST_REGISTRY_1_0", "/bad/registry:/older/registry")
    monkeypatch.setenv(
        "GST_PLUGIN_PATH_1_0",
        "/tmp/venv/lib/python3.12/site-packages/gstreamer_plugins/lib/gstreamer-1.0",
    )
    monkeypatch.setenv("GST_DEBUG", "webrtc*:4")
    monkeypatch.setattr(session_supervisor.subprocess, "Popen", fail_launch)
    monkeypatch.setattr(
        session_supervisor,
        "_cleanup_robot",
        lambda spec: {"status": "ok", "errors": []},
    )

    assert session_supervisor.supervise(spec_path) == 1
    assert not state_path.exists()
    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
    assert terminal["status"] == "failed"
    assert terminal["reason"] == "child_launch_failed"
