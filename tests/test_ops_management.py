from __future__ import annotations

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest
import yaml
from click.testing import CliRunner

from reachy_mini_brain.official_runtime import ops_core
from reachy_mini_brain.official_runtime import playback_probe
from reachy_mini_brain.official_runtime.ops_cli import cli


def make_config(tmp_path: Path) -> ops_core.OpsConfig:
    repo = tmp_path / "repo"
    official = tmp_path / "official"
    repo.mkdir()
    official.mkdir()
    return ops_core.OpsConfig(
        repo_path=repo,
        official_app_repo=official,
        robot_host="192.0.2.10",
        robot_port=8000,
        s2s_host="127.0.0.1",
        s2s_port=8765,
        live_duration_s=900,
        policy_preflight_duration_s=90,
        policy_preflight_timeout_s=30,
        policy_preflight_gap_s=3,
        policy_preflight_greeting="Welcome to the clinic. How can I help you today?",
        preflight_between_probes_gap_s=3,
        log_dir=repo / "artifacts" / "logs",
        state_dir=repo / "artifacts" / "ops",
        preflight_wav=repo / "artifacts" / "known.wav",
        stop_backend_on_exit=False,
        conversation_cues=True,
        capture_vision=True,
        record_audio=True,
        record_video=False,
        python_bin=Path("/usr/bin/python3"),
        backend_start_timeout_s=1,
        keep_awake=True,
    )


def test_physical_actions_require_authorization_before_robot_calls(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(ops_core, "_robot_post", lambda *args, **kwargs: calls.append("post"))

    try:
        ops_core.sleep_robot(config, authorized=False)
    except ops_core.AuthorizationError as exc:
        assert "physical robot action" in str(exc)
    else:
        raise AssertionError("sleep_robot should require physical authorization")

    assert calls == []


def test_supervisor_robot_cleanup_is_bounded_and_disables_motors(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls: list[tuple[str, str, float]] = []

    monkeypatch.setattr(
        ops_core,
        "_robot_get",
        lambda config, path, *, timeout_s=8.0: calls.append(("GET", path, timeout_s)) or [],
    )
    monkeypatch.setattr(
        ops_core,
        "_robot_post",
        lambda config, path, **kwargs: calls.append(
            ("POST", path, kwargs.get("timeout_s", 8.0))
        )
        or {},
    )

    result = ops_core.finalize_robot_after_run(
        config,
        attempts=2,
        request_timeout_s=0.25,
        sleep_fn=lambda _: None,
    )

    assert result.status == "ok"
    assert calls == [
        ("GET", "/api/move/running", 0.25),
        ("POST", "/api/media/release", 0.25),
        ("POST", "/api/move/play/goto_sleep", 0.25),
        ("POST", "/api/motors/set_mode/disabled", 0.25),
    ]


def test_runner_state_status_reports_stale_state(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    state = ops_core.RunnerState(
        pid=999999,
        run_id="official-live-test",
        log_path=config.log_dir / "official-live-test.log",
        artifact_root=config.artifact_root,
        started_at="2026-06-23T12:00:00",
        requested_config={"duration_s": 10},
        command=("python", "-m", "reachy_mini_brain.official_runtime.live_app"),
    )
    ops_core.save_runner_state(config, state)
    monkeypatch.setattr(ops_core, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(ops_core, "_find_pids", lambda pattern: [])

    result = ops_core.runner_status(config)

    assert result.status == "stale_state"
    assert result.data["state"]["run_id"] == "official-live-test"
    assert result.errors == ("runner state file points to a non-running PID",)


def test_runner_status_reports_faulting_for_stale_audio(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    heartbeat_path = config.heartbeat_dir / "official-live-test.json"
    heartbeat_path.parent.mkdir(parents=True)
    heartbeat_path.write_text(
        json.dumps(
            {
                "started_monotonic": 10.0,
                "updated_monotonic": 100.0,
                "phase": "ready",
                "ready_monotonic": 20.0,
                "event_loop_age_s": 0.1,
                "audio": {"expected": True, "sequence": 5, "age_s": 9.0},
                "video": {"expected": False, "sequence": 0, "age_s": None},
            }
        ),
        encoding="utf-8",
    )
    state = ops_core.RunnerState(
        pid=123,
        run_id="official-live-test",
        log_path=config.log_dir / "official-live-test.log",
        artifact_root=config.artifact_root,
        started_at="2026-08-07T12:00:00",
        requested_config={},
        command=("python",),
        heartbeat_path=heartbeat_path,
    )
    ops_core.save_runner_state(config, state)
    monkeypatch.setattr(ops_core, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(ops_core, "_find_pids", lambda pattern: [123])
    monkeypatch.setattr(ops_core.time, "monotonic", lambda: 100.0)

    result = ops_core.runner_status(config)

    assert result.status == "faulting"
    assert result.data["health_fault"] == "audio_stale"


def test_build_live_command_includes_official_runtime_defaults(tmp_path):
    config = make_config(tmp_path)
    command, env = ops_core.build_live_command(
        config,
        run_id="official-live-test",
        duration_s=12,
        perception=True,
        gestures=True,
        audio_gate=True,
        ready_cue=True,
        warmup_video=True,
        conversation_cues=True,
        capture_vision=True,
        record_audio=True,
        record_video=True,
        scripted_policy_flow="none",
    )

    assert command[:3] == ["/usr/bin/python3", "-m", "reachy_mini_brain.official_runtime.live_app"]
    assert "--run-id" in command
    assert "official-live-test" in command
    assert "--perception" in command
    assert "--gestures" in command
    assert "--audio-gate" in command
    assert "--conversation-cues" in command
    assert "--capture-vision" in command
    assert "--record-audio" in command
    assert "--record-video" in command
    profile_index = command.index("--visitor-trigger-profile")
    assert command[profile_index + 1] == "legacy"
    runtime_index = command.index("--vision-runtime")
    assert command[runtime_index + 1] == "serial-v1"
    capture_fps_index = command.index("--broker-capture-fps")
    assert command[capture_fps_index + 1] == "15.0"
    gesture_mode_index = command.index("--gesture-running-mode")
    assert command[gesture_mode_index + 1] == "image"
    wave_mode_index = command.index("--wave-detection-mode")
    assert command[wave_mode_index + 1] == "open_palm"
    backend_index = command.index("--backend")
    assert command[backend_index + 1] == "s2s-local"
    assert env["HF_REALTIME_WS_URL"] == "ws://127.0.0.1:8765/v1/realtime"
    assert env["REACHY_HOST"] == "192.0.2.10"
    assert "REACHY_MINI_CONVERSATION_APP_SRC" not in env
    assert "--profile-owned-context" not in command


def test_build_live_command_uses_profile_owned_context_for_hermes(tmp_path):
    config = ops_core.OpsConfig(**{**make_config(tmp_path).__dict__, "profile_owned_context": True})

    command, _ = ops_core.build_live_command(
        config,
        run_id="official-live-hermes",
        duration_s=12,
        perception=True,
        gestures=True,
        audio_gate=True,
        ready_cue=True,
        warmup_video=True,
        conversation_cues=True,
        capture_vision=True,
        record_audio=True,
        record_video=False,
    )

    assert "--profile-owned-context" in command


def test_ops_config_uses_profile_owned_context_for_conversation_mode(monkeypatch):
    monkeypatch.setenv("S2S_RESPONSES_CONVERSATION", "1")

    config = ops_core.OpsConfig.from_env()

    assert config.profile_owned_context is True


def test_ops_config_loads_versioned_visitor_trigger_profile(monkeypatch):
    monkeypatch.setenv("RECEPTION_VISITOR_TRIGGER_PROFILE", "visitor-v1-20260802")

    config = ops_core.OpsConfig.from_env()

    assert config.visitor_trigger_profile == "visitor-v1-20260802"


def test_ops_config_uses_baselined_media_liveness_thresholds(monkeypatch):
    monkeypatch.delenv("MEDIA_HEARTBEAT_STALE_S", raising=False)
    monkeypatch.delenv("MEDIA_SOURCE_STALE_S", raising=False)
    monkeypatch.delenv("EVENT_LOOP_STALE_S", raising=False)

    config = ops_core.OpsConfig.from_env()

    assert config.media_heartbeat_stale_s == 5.0
    assert config.media_source_stale_s == 8.0
    assert config.event_loop_stale_s == 8.0


def test_ops_config_loads_broker_vision_settings(monkeypatch):
    monkeypatch.setenv("RECEPTION_VISION_RUNTIME", "broker-v1")
    monkeypatch.setenv("RECEPTION_BROKER_CAPTURE_FPS", "15")
    monkeypatch.setenv("RECEPTION_BROKER_RECORDER_QUEUE_SIZE", "45")
    monkeypatch.setenv("RECEPTION_BROKER_GESTURE_QUEUE_SIZE", "20")
    monkeypatch.setenv("RECEPTION_BROKER_POLICY_IDLE_S", "0.1")
    monkeypatch.setenv("RECEPTION_GESTURE_RUNNING_MODE", "video")
    monkeypatch.setenv("RECEPTION_WAVE_DETECTION_MODE", "hand_motion")

    config = ops_core.OpsConfig.from_env()

    assert config.vision_runtime == "broker-v1"
    assert config.broker_capture_fps == 15.0
    assert config.broker_recorder_queue_size == 45
    assert config.broker_gesture_queue_size == 20
    assert config.broker_policy_idle_s == 0.1
    assert config.gesture_running_mode == "video"
    assert config.wave_detection_mode == "hand_motion"


def test_ops_config_rejects_unknown_visitor_trigger_profile(monkeypatch):
    monkeypatch.setenv("RECEPTION_VISITOR_TRIGGER_PROFILE", "latest")

    with pytest.raises(ValueError, match="unknown visitor trigger profile"):
        ops_core.OpsConfig.from_env()


def test_build_policy_command_can_target_single_greet(tmp_path):
    config = make_config(tmp_path)
    command, _ = ops_core.build_live_command(
        config,
        run_id="official-policy-preflight-greet-test",
        duration_s=20,
        perception=False,
        gestures=False,
        audio_gate=False,
        ready_cue=True,
        warmup_video=False,
        conversation_cues=False,
        capture_vision=False,
        record_audio=True,
        record_video=False,
        scripted_policy_flow="greet",
        scripted_policy_gap_s=3,
        scripted_policy_timeout_s=30,
        scripted_policy_greeting=config.policy_preflight_greeting,
    )

    assert "--scripted-policy-flow" in command
    flow_index = command.index("--scripted-policy-flow")
    assert command[flow_index + 1] == "greet"
    greeting_index = command.index("--scripted-policy-greeting")
    assert command[greeting_index + 1] == "Welcome to the clinic. How can I help you today?"
    assert "--no-perception" in command
    assert "--no-gestures" in command
    assert "--no-audio-gate" in command
    assert "--record-audio" in command
    assert "--no-record-video" in command


def test_build_audio_playback_command_uses_live_app_scripted_playback(tmp_path):
    config = make_config(tmp_path)
    command, env = ops_core.build_audio_playback_command(config, config.preflight_wav, run_id="audio-preflight-test")

    assert command == [
        str(config.repo_path / "scripts" / "m1max" / "run_official_runtime_live.sh"),
        "--run-id",
        "audio-preflight-test",
        "--artifact-root",
        str(config.artifact_root),
        "--duration",
        "30",
        "--robot-host",
        "192.0.2.10",
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
        str(config.preflight_wav),
        "--scripted-playback-post-roll-s",
        "3.0",
    ]
    assert env["PYTHONPATH"].startswith(str(config.repo_path / "src"))
    assert env["REACHY_HOST"] == "192.0.2.10"


def test_base_env_resets_pythonpath_without_gstreamer_overrides(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    repo_python = config.repo_path / ".venv" / "bin" / "python"
    repo_python.parent.mkdir(parents=True)
    repo_python.write_text("", encoding="utf-8")
    config = ops_core.OpsConfig(**{**config.__dict__, "python_bin": repo_python})
    gi_python = (
        config.repo_path
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "gstreamer_python"
        / "lib"
        / "python3.12"
        / "site-packages"
    )
    gi_python.mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", "/wrong/venv/site-packages")
    bundle_root = "/wrong/venv/site-packages/gstreamer_libs"
    monkeypatch.setenv("PATH", f"{bundle_root}/bin:/usr/bin")
    monkeypatch.setenv("GI_TYPELIB_PATH", f"{bundle_root}/lib/girepository-1.0")
    monkeypatch.setenv("GST_PLUGIN_PATH_1_0", f"{bundle_root}/lib/gstreamer-1.0")
    monkeypatch.setenv("GST_PLUGIN_SCANNER_1_0", "/wrong/plugin-scanner")
    monkeypatch.setenv("GST_REGISTRY_1_0", "/wrong/registry.bin:/older/registry.bin")
    monkeypatch.setenv("GST_DEBUG", "webrtc*:4")

    _, env = ops_core.build_audio_playback_command(config, config.preflight_wav)

    assert env["PYTHONPATH"] == f"{config.repo_path / 'src'}:{gi_python}"
    assert env["PATH"] == "/usr/bin"
    assert "GI_TYPELIB_PATH" not in env
    assert "GST_PLUGIN_PATH" not in env
    assert "GST_PLUGIN_PATH_1_0" not in env
    assert "GST_PLUGIN_SCANNER_1_0" not in env
    assert "GST_REGISTRY_1_0" not in env
    assert env["GST_DEBUG"] == "webrtc*:4"
    assert "OFFICIAL_APP_REPO" not in env


def test_default_python_prefers_clean_repo_venv(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    official = tmp_path / "official"
    repo_python = repo / ".venv" / "bin" / "python"
    repo_python.parent.mkdir(parents=True)
    repo_python.write_text("", encoding="utf-8")
    monkeypatch.delenv("OFFICIAL_RUNTIME_PYTHON", raising=False)

    assert ops_core._default_python_bin(repo_path=repo, official_app_repo=official) == repo_python


def test_audio_playback_validation_does_not_require_official_app_source(tmp_path):
    config = make_config(tmp_path)
    script = config.repo_path / "scripts" / "m1max" / "run_official_runtime_live.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    assert ops_core._validate_audio_playback_launch_paths(config) == []


def test_cli_blocks_physical_command_without_confirmation(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(ops_core.OpsConfig, "from_env", classmethod(lambda cls: config))
    runner = CliRunner()

    result = runner.invoke(cli, ["sleep-robot"])

    assert result.exit_code != 0
    assert "physical robot action" in result.output


def test_latest_run_roundtrip(tmp_path):
    config = make_config(tmp_path)
    state = ops_core.RunnerState(
        pid=123,
        run_id="official-live-test",
        log_path=config.log_dir / "official-live-test.log",
        artifact_root=config.artifact_root,
        started_at="2026-06-23T12:00:00",
        requested_config={},
        command=("python",),
    )

    ops_core.save_latest_run(config, state)

    latest = ops_core.load_latest_run(config)
    assert latest is not None
    assert latest["run_id"] == "official-live-test"
    assert latest["manifest_path"].endswith("run-official-live-test.json")


def test_launch_background_detaches_and_starts_caffeinate_watcher(tmp_path, monkeypatch):
    calls: list[dict] = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            self.pid = 1000 + len(calls)

    monkeypatch.setattr(ops_core.shutil, "which", lambda name: "/usr/bin/caffeinate")
    monkeypatch.setattr(ops_core.subprocess, "Popen", FakePopen)

    proc, caffeinate_pid = ops_core._launch_background(
        ["python", "-m", "module"],
        cwd=tmp_path,
        env={},
        logfile=tmp_path / "run.log",
        keep_awake=True,
    )

    assert proc.pid == 1001
    assert caffeinate_pid == 1002
    assert calls[0]["args"][0] == ["python", "-m", "module"]
    assert calls[0]["kwargs"]["start_new_session"] is True
    assert calls[1]["args"][0] == ["/usr/bin/caffeinate", "-dimsu", "-w", "1001"]
    assert calls[1]["kwargs"]["start_new_session"] is True


def test_official_runtime_playback_probe_uses_session_and_audio_sink(tmp_path):
    wav_path = tmp_path / "probe.wav"
    audio = np.arange(320, dtype=np.int16)
    _write_pcm_wav(wav_path, sample_rate=16_000, audio=audio)
    sessions = []
    sinks = []

    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.stopped = False
            sessions.append(self)

        def start(self):
            return "mini"

        def stop(self):
            self.stopped = True

    class FakeSink:
        def __init__(self, mini):
            self.mini = mini
            self.frames = []
            self.closed = False
            sinks.append(self)

        async def write(self, frame):
            self.frames.append(frame)

        async def drain(self):
            pass

        async def close(self):
            self.closed = True

    result = playback_probe.play_wav_once(
        wav_path,
        robot_host="192.0.2.10",
        audio_timeout_s=12,
        post_roll_s=0,
        session_factory=FakeSession,
        sink_factory=FakeSink,
    )

    assert result["sample_rate"] == 16_000
    assert result["samples"] == 320
    assert result["robot_host"] == "192.0.2.10"
    assert sessions[0].kwargs == {
        "host": "192.0.2.10",
        "warmup_audio": True,
        "warmup_video": False,
        "audio_timeout_s": 12,
    }
    assert sessions[0].stopped is True
    assert sinks[0].mini == "mini"
    assert sinks[0].closed is True
    sample_rate, written = sinks[0].frames[0]
    assert sample_rate == 16_000
    assert np.array_equal(written, audio)


def test_start_runner_saves_supervisor_pid_and_child_command(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls: list[list[str]] = []

    class FakeProc:
        pid = 4321

    monkeypatch.setattr(ops_core, "runner_status", lambda config: ops_core.ActionResult(action="runner.status", status="stopped"))

    def fake_launch(command, *, cwd, env, logfile, keep_awake):
        calls.append(command)
        return FakeProc(), 9876

    monkeypatch.setattr(ops_core, "_launch_background", fake_launch)

    result = ops_core.start_runner(config, authorized=True, run_id="official-live-test")

    assert result.status == "ok"
    assert result.data["pid"] == 4321
    assert result.data["caffeinate_pid"] == 9876
    assert calls[0][0:3] == [
        "/usr/bin/python3",
        "-m",
        "reachy_mini_brain.official_runtime.session_supervisor",
    ]
    state = ops_core.load_runner_state(config)
    assert state is not None
    assert state.pid == 4321
    assert state.command[0:3] == (
        "/usr/bin/python3",
        "-m",
        "reachy_mini_brain.official_runtime.live_app",
    )
    assert state.heartbeat_path is not None
    assert "--heartbeat-path" in state.command
    assert state.supervisor_spec_path is not None
    assert state.supervisor_spec_path.exists()
    assert state.requested_config["caffeinate_pid"] == 9876
    assert state.requested_config["record_audio"] is True
    assert state.requested_config["record_video"] is False
    assert state.requested_config["profile_owned_context"] is False


def test_start_runner_can_enable_raw_video_recording(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls: list[list[str]] = []

    class FakeProc:
        pid = 4321

    monkeypatch.setattr(ops_core, "runner_status", lambda config: ops_core.ActionResult(action="runner.status", status="stopped"))
    monkeypatch.setattr(ops_core, "_launch_background", lambda command, **kwargs: calls.append(command) or (FakeProc(), None))

    result = ops_core.start_runner(config, authorized=True, run_id="official-live-video", record_video=True)

    assert result.status == "ok"
    state = ops_core.load_runner_state(config)
    assert state is not None
    assert "--record-video" in state.command
    assert "--record-audio" in state.command
    assert state.requested_config["record_video"] is True
    assert state.requested_config["record_audio"] is True


def test_stop_runner_uses_extended_grace_for_artifact_finalization(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    state = ops_core.RunnerState(
        pid=4321,
        run_id="official-live-artifacts",
        log_path=config.log_dir / "official-live-artifacts.log",
        artifact_root=config.artifact_root,
        started_at="2026-06-25T14:00:00",
        requested_config={"record_video": True},
        command=("python", "-m", "reachy_mini_brain.official_runtime.live_app"),
    )
    ops_core.save_runner_state(config, state)
    calls: list[tuple[list[int], float]] = []

    monkeypatch.setattr(ops_core, "_pid_alive", lambda pid: pid == 4321)

    def fake_terminate(pids, *, grace_s=2.0):
        calls.append((list(pids), grace_s))
        return list(pids)

    monkeypatch.setattr(ops_core, "_terminate_pids", fake_terminate)

    result = ops_core.stop_runner(config, authorized=True)

    assert result.status == "ok"
    assert result.changed is True
    assert calls == [([4321], ops_core.RUNNER_STOP_GRACE_S)]


def test_runner_cli_start_requires_confirmation(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(ops_core.OpsConfig, "from_env", classmethod(lambda cls: config))
    runner = CliRunner()

    result = runner.invoke(cli, ["runner", "start"])

    assert result.exit_code != 0
    assert "physical robot action" in result.output


def test_start_session_composes_resource_primitives_in_order(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(
        ops_core,
        "stop_runner",
        lambda config, *, authorized, include_unmanaged=False: calls.append("stop_runner")
        or ops_core.ActionResult(action="runner.stop"),
    )
    monkeypatch.setattr(
        ops_core,
        "start_backend",
        lambda config: calls.append("start_backend") or ops_core.ActionResult(action="backend.start"),
    )
    monkeypatch.setattr(
        ops_core,
        "wake_robot",
        lambda config, *, authorized: calls.append("wake_robot") or ops_core.ActionResult(action="robot.wake"),
    )
    monkeypatch.setattr(
        ops_core,
        "start_runner",
        lambda config, *, authorized: calls.append("start_runner") or ops_core.ActionResult(action="runner.start"),
    )

    results = ops_core.start_session(config, authorized=True)

    assert [result.action for result in results] == ["runner.stop", "backend.start", "robot.wake", "runner.start"]
    assert calls == ["stop_runner", "start_backend", "wake_robot", "start_runner"]


def test_stop_session_and_shutdown_are_scoped_to_runner_and_robot(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(
        ops_core,
        "stop_runner",
        lambda config, *, authorized, include_unmanaged=False: calls.append(f"stop_runner:{include_unmanaged}")
        or ops_core.ActionResult(action="runner.stop"),
    )
    monkeypatch.setattr(
        ops_core,
        "sleep_robot",
        lambda config, *, authorized: calls.append("sleep_robot") or ops_core.ActionResult(action="robot.sleep"),
    )
    monkeypatch.setattr(
        ops_core,
        "stop_backend",
        lambda config: calls.append("stop_backend") or ops_core.ActionResult(action="backend.stop"),
    )

    stop_results = ops_core.stop_session(config, authorized=True)
    shutdown_results = ops_core.shutdown(config, authorized=True)

    assert [result.action for result in stop_results] == ["runner.stop", "robot.sleep"]
    assert [result.action for result in shutdown_results] == ["runner.stop", "robot.sleep"]
    assert "stop_backend" not in calls


def test_full_preflight_runs_exposed_substeps_in_order(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr(
        ops_core,
        "preflight_audio_playback",
        lambda config, *, authorized: calls.append("audio") or ops_core.ActionResult(action="preflight.audio_playback"),
    )
    monkeypatch.setattr(
        ops_core,
        "preflight_policy",
        lambda config, *, authorized, flow, run_id=None: calls.append(flow)
        or ops_core.ActionResult(action=f"preflight.policy_{flow}"),
    )

    results = ops_core.full_preflight(config, authorized=True, sleep_fn=lambda seconds: calls.append(f"sleep:{seconds}"))

    assert [result.action for result in results] == [
        "preflight.audio_playback",
        "preflight.policy_goodbye",
        "preflight.policy_greet",
    ]
    assert calls == ["audio", "sleep:3", "goodbye", "sleep:3", "greet"]


def test_aggregate_status_excludes_robot_by_default_and_includes_when_requested(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    robot_calls: list[str] = []
    monkeypatch.setattr(
        ops_core,
        "backend_status",
        lambda config: ops_core.ActionResult(action="backend.status", status="ok", data={"port_live": True}),
    )
    monkeypatch.setattr(
        ops_core,
        "runner_status",
        lambda config: ops_core.ActionResult(action="runner.status", status="stopped", data={"live_pids": []}),
    )

    def fake_robot_status(config):
        robot_calls.append("robot")
        return ops_core.ActionResult(action="robot.status", status="ok", data={"daemon": {"state": "running"}})

    monkeypatch.setattr(ops_core, "robot_status", fake_robot_status)

    without_robot = ops_core.aggregate_status(config)
    with_robot = ops_core.aggregate_status(config, include_robot=True)

    assert "robot" not in without_robot.data
    assert "robot" in with_robot.data
    assert robot_calls == ["robot"]


def test_backend_start_reports_missing_launch_script(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(ops_core, "_port_open", lambda host, port: False)
    monkeypatch.setattr(ops_core, "_find_pids", lambda pattern: [])

    result = ops_core.start_backend(config)

    assert result.status == "failed"
    assert "missing backend launch script" in result.errors[0]


def test_backend_start_noops_when_port_is_already_live(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    launch_calls: list[str] = []
    monkeypatch.setattr(ops_core, "_port_open", lambda host, port: True)
    monkeypatch.setattr(ops_core, "_find_pids", lambda pattern: [123])
    monkeypatch.setattr(
        ops_core,
        "_launch_background",
        lambda *args, **kwargs: launch_calls.append("launch"),
    )

    result = ops_core.start_backend(config)

    assert result.status == "ok"
    assert result.changed is False
    assert result.data["already_running"] is True
    assert launch_calls == []


def test_backend_start_reports_ready_after_detached_launch(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    script = config.repo_path / "scripts" / "m1max" / "run_s2s_backend.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    port_checks = iter([False, True])

    class FakeProc:
        pid = 222

        def poll(self):
            return None

    monkeypatch.setattr(ops_core, "_port_open", lambda host, port: next(port_checks))
    monkeypatch.setattr(ops_core, "_find_pids", lambda pattern: [])
    monkeypatch.setattr(ops_core, "_launch_background", lambda *args, **kwargs: (FakeProc(), 333))

    result = ops_core.start_backend(config)

    assert result.status == "ok"
    assert result.changed is True
    assert result.data["pid"] == 222
    assert result.data["caffeinate_pid"] == 333


def test_backend_start_reports_process_exit_before_ready(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    script = config.repo_path / "scripts" / "m1max" / "run_s2s_backend.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    class FakeProc:
        pid = 222
        returncode = 7

        def poll(self):
            return self.returncode

    monkeypatch.setattr(ops_core, "_port_open", lambda host, port: False)
    monkeypatch.setattr(ops_core, "_find_pids", lambda pattern: [])
    monkeypatch.setattr(ops_core, "_launch_background", lambda *args, **kwargs: (FakeProc(), None))

    result = ops_core.start_backend(config)

    assert result.status == "failed"
    assert result.errors == ("backend exited before the websocket port became ready",)
    assert result.data["pid"] == 222


def test_backend_start_reports_timeout(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config = ops_core.OpsConfig(**{**config.__dict__, "backend_start_timeout_s": 0})
    script = config.repo_path / "scripts" / "m1max" / "run_s2s_backend.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    class FakeProc:
        pid = 222

        def poll(self):
            return None

    monkeypatch.setattr(ops_core, "_port_open", lambda host, port: False)
    monkeypatch.setattr(ops_core, "_find_pids", lambda pattern: [])
    monkeypatch.setattr(ops_core, "_launch_background", lambda *args, **kwargs: (FakeProc(), None))

    result = ops_core.start_backend(config)

    assert result.status == "failed"
    assert result.errors == ("backend did not become ready before timeout",)


def test_backend_stop_terminates_matching_backend_pids(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(ops_core, "_find_pids", lambda pattern: [101, 202])
    monkeypatch.setattr(ops_core, "_terminate_pids", lambda pids: pids)

    result = ops_core.stop_backend(config)

    assert result.status == "ok"
    assert result.changed is True
    assert result.data["stopped_pids"] == [101, 202]


def test_s2s_backend_setup_script_contract() -> None:
    script = Path("scripts/m1max/setup_s2s_backend.sh")
    text = script.read_text(encoding="utf-8")

    assert script.exists()
    assert "S2S_BACKEND_VERSION:-0.2.10" in text
    assert "https://github.com/noelotpyrc/speech-to-speech.git" in text
    assert "a963ca68b9aa3599b7ea5eeabb9505a68263fbff" in text
    assert "speech_to_speech_fork_url" in text
    assert "speech_to_speech_fork_sha" in text
    assert "/Users/leon/projects/speech_to_speech_backend" in text
    assert "speech-to-speech==$BACKEND_VERSION" in text
    assert "speech_to_speech.STT.parakeet_tdt_handler" in text
    assert "runtime-info.json" in text
    assert "--skip-running-check" in text
    assert "rm -rf" not in text


def test_hermes_profile_sync_script_contract() -> None:
    script = Path("scripts/m1max/sync_hermes_profile.sh")
    text = script.read_text(encoding="utf-8")

    assert script.exists()
    assert "--profile is required" in text
    assert "--allow-production" in text
    assert '"$PROFILE" == "reachyclinic"' in text
    assert "personality.md" in text
    assert "clinic_facts.md" in text
    assert "capabilities.md" in text
    assert "reference_catalog.yaml" in text
    assert "reference-library" in text
    assert "latency-trace" in text
    assert "reference_readonly" in text
    assert '"api_server"' in text
    assert '"no_mcp"' in text
    assert '("file", "skills", "memory", "web", "terminal")' in text
    assert "--delete" not in text
    assert "rm " not in text


def test_hermes_profile_sync_dry_run_does_not_write(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profile_dir = profiles_dir / "reachyclinic-test"
    source_dir = tmp_path / "source"
    profile_dir.mkdir(parents=True)
    source_dir.mkdir()
    (profile_dir / "SOUL.md").write_text("original\n", encoding="utf-8")
    for name in (
        "personality.md",
        "HERMES.md",
        "reference_catalog.yaml",
        "clinic_facts.md",
        "capabilities.md",
    ):
        (source_dir / name).write_text(f"new {name}\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "scripts/m1max/sync_hermes_profile.sh",
            "--profile",
            "reachyclinic-test",
            "--profiles-dir",
            str(profiles_dir),
            "--source-dir",
            str(source_dir),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HERMES_PYTHON": sys.executable,
        },
    )

    assert result.returncode == 0, result.stderr
    assert (profile_dir / "SOUL.md").read_text(encoding="utf-8") == "original\n"
    assert not (profile_dir / "context").exists()
    assert not (profile_dir / "config.yaml").exists()


def test_hermes_profile_sync_installs_read_only_reference_policy(tmp_path: Path) -> None:
    profiles_dir = tmp_path / "profiles"
    profile_dir = profiles_dir / "reachyclinic-test"
    source_dir = tmp_path / "source"
    profile_dir.mkdir(parents=True)
    source_dir.mkdir()
    (source_dir / "personality.md").write_text("New personality.\n", encoding="utf-8")
    (source_dir / "HERMES.md").write_text("# Stable instructions\n", encoding="utf-8")
    (source_dir / "clinic_facts.md").write_text(
        "# Clinic facts\n\nOpen weekdays.\n", encoding="utf-8"
    )
    (source_dir / "capabilities.md").write_text(
        "# Receptionist capabilities\n\n## Supported\n\nEscalate unsupported requests.\n",
        encoding="utf-8",
    )
    (source_dir / "reference_catalog.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "references": {
                    "clinic.facts": {
                        "path": "clinic_facts.md",
                        "title": "Clinic facts",
                        "summary": "Hours.",
                        "delivery": "prompt",
                        "tags": ["hours"],
                        "audience": "visitor",
                        "max_bytes": 1024,
                    },
                    "clinic.capabilities": {
                        "path": "capabilities.md",
                        "title": "Receptionist capabilities",
                        "summary": "Action boundaries.",
                        "delivery": "prompt",
                        "tags": ["capabilities"],
                        "audience": "visitor",
                        "max_bytes": 1024,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/m1max/sync_hermes_profile.sh",
            "--profile",
            "reachyclinic-test",
            "--profiles-dir",
            str(profiles_dir),
            "--source-dir",
            str(source_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HERMES_PYTHON": sys.executable,
        },
    )

    assert result.returncode == 0, result.stderr
    config = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert config["platform_toolsets"]["api_server"] == [
        "reference_readonly",
        "no_mcp",
    ]
    assert set(config["agent"]["disabled_toolsets"]) >= {
        "file",
        "skills",
        "memory",
        "web",
        "terminal",
    }
    assert "reference-library" in config["plugins"]["enabled"]
    assert "latency-trace" in config["plugins"]["enabled"]
    assert config["reference_library"]["catalog"] == str(
        profile_dir / "context/receptionist/reference_catalog.yaml"
    )
    assert (profile_dir / "plugins/reference-library/plugin.yaml").exists()
    assert (profile_dir / "plugins/reference-library/__init__.py").exists()
    assert (profile_dir / "plugins/latency-trace/plugin.yaml").exists()
    assert (profile_dir / "plugins/latency-trace/__init__.py").exists()
    assert (profile_dir / "context/receptionist/HERMES.md").read_text(
        encoding="utf-8"
    ) == (
        "# Stable instructions\n\n"
        "## Clinic facts\n\nOpen weekdays.\n\n"
        "## Receptionist capabilities\n\n"
        "### Supported\n\nEscalate unsupported requests.\n"
    )


def test_hermes_profile_sync_rejects_unguarded_production(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profiles" / "reachyclinic"
    source_dir = tmp_path / "source"
    profile_dir.mkdir(parents=True)
    source_dir.mkdir()
    for name in (
        "personality.md",
        "HERMES.md",
        "reference_catalog.yaml",
        "clinic_facts.md",
        "capabilities.md",
    ):
        (source_dir / name).write_text(f"new {name}\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            "scripts/m1max/sync_hermes_profile.sh",
            "--profile",
            "reachyclinic",
            "--profiles-dir",
            str(tmp_path / "profiles"),
            "--source-dir",
            str(source_dir),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HERMES_PYTHON": sys.executable,
        },
    )

    assert result.returncode == 2
    assert "Refusing to update production profile" in result.stderr


def test_s2s_backend_launcher_supports_responses_wrapper_endpoint() -> None:
    script = Path("scripts/m1max/run_s2s_backend.sh")
    text = script.read_text(encoding="utf-8")

    assert "S2S_RESPONSES_BASE_URL" in text
    assert "S2S_RESPONSES_API_KEY" in text
    assert '--responses_api_base_url "$S2S_RESPONSES_BASE_URL"' in text
    assert 'S2S_MODEL_NAME="${S2S_MODEL_NAME:-wrapper-routed}"' in text
    assert 'export OPENAI_API_KEY="local-wrapper"' in text
    assert 'S2S_MODEL_NAME="${S2S_MODEL_NAME:-openai/gpt-5.6-luna}"' in text
    assert "S2S_RESPONSES_CONVERSATION" in text
    assert "S2S_RESPONSES_DIRECT_BASE_URL" in text
    assert "S2S_RESPONSES_DIRECT_MODEL" in text
    assert (
        'S2S_RESPONSES_DIRECT_MODEL="${S2S_RESPONSES_DIRECT_MODEL:-openai/gpt-5.6-luna}"'
        in text
    )
    assert "S2S_RESPONSES_DIRECT_API_KEY" in text
    assert 'export RESPONSES_API_DIRECT_API_KEY="$S2S_RESPONSES_DIRECT_API_KEY"' in text
    assert "--responses_api_direct_api_key" not in text


def test_live_ops_status_redacts_credential_arguments() -> None:
    text = Path("scripts/m1max/live_ops.sh").read_text(encoding="utf-8")

    assert "redact_process_args" in text
    assert "api[-_]?key|token|secret|password" in text
    assert "grep -v grep | redact_process_args" in text


def test_s2s_backend_launcher_rejects_conversation_without_wrapper(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", "scripts/m1max/run_s2s_backend.sh"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "BACKEND_DIR": str(tmp_path / "backend"),
            "ENV_FILE": str(tmp_path / "missing.env"),
            "S2S_PORT": "65431",
            "S2S_RESPONSES_CONVERSATION": "1",
        },
    )

    assert result.returncode == 2
    assert "requires S2S_RESPONSES_BASE_URL" in result.stderr


def test_s2s_backend_launcher_passes_conversation_and_direct_lane_args(tmp_path: Path) -> None:
    backend_dir = tmp_path / "backend"
    cli = backend_dir / ".venv" / "bin" / "speech-to-speech"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${RESPONSES_API_DIRECT_API_KEY:-}\" == \"direct-key\" ]]; then\n"
        "  printf 'direct-key-env=set\\n' >&2\n"
        "fi\n"
        "printf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)

    result = subprocess.run(
        ["bash", "scripts/m1max/run_s2s_backend.sh"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "BACKEND_DIR": str(backend_dir),
            "ENV_FILE": str(tmp_path / "missing.env"),
            "S2S_PORT": "65432",
            "S2S_RESPONSES_BASE_URL": "http://127.0.0.1:8642/v1",
            "S2S_RESPONSES_API_KEY": "hermes-key",
            "S2S_RESPONSES_CONVERSATION": "1",
            "S2S_RESPONSES_CONVERSATION_PREFIX": "reachy-test",
            "S2S_RESPONSES_DIRECT_BASE_URL": "https://openrouter.ai/api/v1",
            "S2S_RESPONSES_DIRECT_MODEL": "openai/gpt-5.4-mini",
            "OPENROUTER_API_KEY": "direct-key",
        },
    )

    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert "--responses_api_conversation" in args
    assert args[args.index("--responses_api_conversation_prefix") + 1] == "reachy-test"
    assert "--no_responses_api_disable_thinking" in args
    assert args[args.index("--responses_api_direct_base_url") + 1] == "https://openrouter.ai/api/v1"
    assert args[args.index("--responses_api_direct_model_name") + 1] == "openai/gpt-5.4-mini"
    assert "--responses_api_direct_api_key" not in args
    assert "direct-key" not in result.stdout
    assert "direct-key-env=set" in result.stderr


def test_s2s_backend_setup_script_dry_run_does_not_create_backend_dir(tmp_path: Path) -> None:
    backend_dir = tmp_path / "backend"
    result = subprocess.run(
        [
            "bash",
            "scripts/m1max/setup_s2s_backend.sh",
            "--dry-run",
            "--backend-dir",
            str(backend_dir),
            "--python",
            sys.executable,
            "--skip-running-check",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "speech-to-speech==0.2.10" in result.stderr
    assert "would verify package version, CLI, and Parakeet STT handler import" in result.stderr
    assert not backend_dir.exists()


def _write_pcm_wav(path: Path, *, sample_rate: int, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(np.asarray(audio, dtype="<i2").tobytes())
