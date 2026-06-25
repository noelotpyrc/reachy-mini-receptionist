from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
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
    backend_index = command.index("--backend")
    assert command[backend_index + 1] == "s2s-local"
    assert env["HF_REALTIME_WS_URL"] == "ws://127.0.0.1:8765/v1/realtime"
    assert env["REACHY_HOST"] == "192.0.2.10"
    assert "REACHY_MINI_CONVERSATION_APP_SRC" not in env


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
    monkeypatch.setenv("GI_TYPELIB_PATH", "/wrong/venv/girepository")
    monkeypatch.setenv("GST_PLUGIN_SCANNER_1_0", "/wrong/plugin-scanner")

    _, env = ops_core.build_audio_playback_command(config, config.preflight_wav)

    assert env["PYTHONPATH"] == f"{config.repo_path / 'src'}:{gi_python}"
    assert "GI_TYPELIB_PATH" not in env
    assert "GST_PLUGIN_PATH" not in env
    assert "GST_PLUGIN_SCANNER_1_0" not in env
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


def test_start_runner_saves_actual_runner_pid_and_caffeinate_pid(tmp_path, monkeypatch):
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
    assert calls[0][0:3] == ["/usr/bin/python3", "-m", "reachy_mini_brain.official_runtime.live_app"]
    state = ops_core.load_runner_state(config)
    assert state is not None
    assert state.pid == 4321
    assert state.requested_config["caffeinate_pid"] == 9876
    assert state.requested_config["record_audio"] is True
    assert state.requested_config["record_video"] is False


def test_start_runner_can_enable_raw_video_recording(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    calls: list[list[str]] = []

    class FakeProc:
        pid = 4321

    monkeypatch.setattr(ops_core, "runner_status", lambda config: ops_core.ActionResult(action="runner.status", status="stopped"))
    monkeypatch.setattr(ops_core, "_launch_background", lambda command, **kwargs: calls.append(command) or (FakeProc(), None))

    result = ops_core.start_runner(config, authorized=True, run_id="official-live-video", record_video=True)

    assert result.status == "ok"
    assert "--record-video" in calls[0]
    assert "--record-audio" in calls[0]
    state = ops_core.load_runner_state(config)
    assert state is not None
    assert state.requested_config["record_video"] is True
    assert state.requested_config["record_audio"] is True


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


def _write_pcm_wav(path: Path, *, sample_rate: int, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(np.asarray(audio, dtype="<i2").tobytes())
