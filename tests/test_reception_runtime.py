"""Offline native-service integration with the real runtime assembly and fake IO."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from reachy_mini_brain.official_runtime import live_app
from reachy_mini_brain.official_runtime.live_session import LiveSessionControl, runtime_lease
from reachy_mini_brain.official_runtime.liveness import RuntimeLiveness
from reachy_mini_brain.official_runtime.robot_io import ReachyRobotSession
from reachy_mini_brain.official_runtime.session_supervisor import HealthThresholds
from reachy_mini_brain.reception_runtime import load_runtime_options, make_reception_runtime
from reachy_mini_brain.reception_service import SessionController
from reachy_mini_brain.reception_status import ReceptionStatus
from reachy_mini_reception_app.protocol import Command, ProtocolError


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def fake_io(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEPTION_RUNTIME_LOCK_PATH", str(tmp_path / "runtime.lock"))
    calls = []

    class Media:
        def __init__(self):
            self.audio = types.SimpleNamespace(clear_player=lambda: calls.append("flush"))

        def get_audio_sample(self):
            return np.zeros(320, dtype=np.float32)

        def get_output_audio_samplerate(self):
            return 16000

        def push_audio_sample(self, audio):
            calls.append("audio")

        def close(self):
            calls.append("media_close")

    mini = types.SimpleNamespace(media=Media(), client=types.SimpleNamespace(
        disconnect=lambda: calls.append("disconnect"),
    ))
    mini.media_manager = mini.media

    class FakeSession(ReachyRobotSession):
        def start(self):
            calls.append("sdk_start")
            self.mini = mini
            return mini

    class Handler:
        def __init__(self):
            self.sent = False

        async def start_up(self):
            calls.append("backend_start")

        async def receive(self, frame):
            await asyncio.sleep(0.01)

        async def emit(self):
            if not self.sent:
                self.sent = True
                return (16000, np.zeros(320, dtype=np.int16))
            await asyncio.Future()

        async def shutdown(self):
            calls.append("backend_close")

    def no_signals(*args):
        pytest.fail("Embedded runtime must not install process signal handlers")

    monkeypatch.setattr(live_app, "ReachyRobotSession", FakeSession)
    monkeypatch.setattr(live_app, "_build_handler", lambda **kwargs: Handler())
    monkeypatch.setattr(live_app, "_install_signal_handlers", no_signals)
    monkeypatch.setattr(live_app, "_set_antennas", lambda value: calls.append(("antennas", value)))
    from reachy_mini_brain import robot

    def no_network(*args, **kwargs):
        raise AssertionError("Offline test attempted robot HTTP IO")

    monkeypatch.setattr(robot, "_request", no_network)
    return mini, calls


def minimal_options(tmp_path):
    return dict(artifact_root=tmp_path / "artifacts", duration=None, instructions="Test only.",
                robot_host="192.0.2.10", record_audio=False, record_video=False,
                perception=False, gestures=False, capture_vision=False, audio_gate=False)


def test_callable_defaults_match_cli_defaults():
    parameters = inspect.signature(live_app._run_live).parameters
    for option in live_app.cli.params:
        if option.name in {"run_id", "agent_tools"}:
            continue
        assert parameters[option.name].default == option.default, option.name


def test_cli_still_uses_shared_entry_and_preserves_explicit_options(monkeypatch):
    received = []

    async def entry(**options):
        received.append(options)

    monkeypatch.setattr(live_app, "run_live_session", entry)
    result = CliRunner().invoke(live_app.cli, ["--duration", "2", "--run-id", "cli-test"])
    assert result.exit_code == 0, result.output
    assert received[0]["duration"] == 2
    assert received[0]["agent_tools"] == "none"
    assert "control" not in received[0]


def test_runtime_lease_excludes_other_sessions_and_is_reusable(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEPTION_RUNTIME_LOCK_PATH", str(tmp_path / "lease"))
    with runtime_lease():
        with pytest.raises(RuntimeError, match="owns this host"):
            with runtime_lease():
                pytest.fail("Second owner acquired runtime")
    with runtime_lease():
        pass
    assert (tmp_path / "lease").exists()


def test_service_runs_shared_pipeline_stops_and_restarts(tmp_path, fake_io):
    _, calls = fake_io

    async def exercise():
        runner = make_reception_runtime(minimal_options(tmp_path), check_owner=lambda: None)
        service = SessionController({"test": ("robot", runner)})
        for session_id in ("first", "second"):
            run = service.start(object(), Command("start", session_id, "test", "robot"))
            await eventually(lambda: run.phase in {"ready", "faulted"})
            assert run.phase == "ready"
            await eventually(lambda: calls.count("audio") == (1 if session_id == "first" else 2))
            await service.stop(run, "native_stop")
            assert run.phase == "stopped"
            assert not service.fault_latched
            manifest = json.loads((tmp_path / "artifacts/runs" / f"run-native-{session_id}.json").read_text())
            assert manifest["ended_ts"] is not None
        assert calls.count("sdk_start") == 2
        assert calls.count("flush") == 2
        assert calls.count("media_close") == 2
        assert calls.count("disconnect") == 2
        assert calls.count("backend_close") == 2
        assert not any(isinstance(call, tuple) for call in calls)
        count = calls.count("audio")
        await asyncio.sleep(0.05)
        assert calls.count("audio") == count

    asyncio.run(exercise())


def test_failed_backend_start_closes_partial_session(tmp_path, fake_io, monkeypatch):
    _, calls = fake_io

    class FailedHandler:
        async def start_up(self):
            raise RuntimeError("test startup failure")

        async def shutdown(self):
            calls.append("backend_close")

    monkeypatch.setattr(live_app, "_build_handler", lambda **kwargs: FailedHandler())

    async def exercise():
        runner = make_reception_runtime(minimal_options(tmp_path), check_owner=lambda: None)
        service = SessionController({"test": ("robot", runner)})
        run = service.start(object(), Command("start", "backend-failed", "test", "robot"))
        await run.task
        assert run.phase == "faulted" and service.fault_latched
        assert calls.count("backend_close") == 1
        assert calls.count("disconnect") == 1
        manifest = json.loads((tmp_path / "artifacts/runs/run-native-backend-failed.json").read_text())
        assert manifest["ended_ts"] is not None

    asyncio.run(exercise())


def test_immediate_stop_without_worker_allows_another_start(tmp_path):
    status = ReceptionStatus({"agent_profile_id": "test", "agent_tools": "none",
                              "visitor_trigger_profile": "test"})

    async def unexpected_entry(**kwargs):
        pytest.fail("Stop before scheduling must not initialize robot IO")

    async def exercise():
        runner = make_reception_runtime(
            {"artifact_root": tmp_path}, status=status, session_entry=unexpected_entry,
            check_owner=lambda: None,
        )
        service = SessionController({"test": ("robot", runner)}, cleanup_reader=status.cleanup_snapshot)
        for session_id in ("first", "second"):
            run = service.start(object(), Command("start", session_id, "test", "robot"))
            await service.stop(run, "native_stop")
            assert run.phase == "stopped"
            assert run.snapshot()["cleanup"]["state"] == "complete"

    asyncio.run(exercise())


def test_profile_and_approved_tools_reach_existing_handler(tmp_path, fake_io, monkeypatch):
    original = live_app._build_handler
    received = []

    def build(**kwargs):
        received.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(live_app, "_build_handler", build)
    options = minimal_options(tmp_path)
    options.pop("instructions")
    options.update(
        agent_profile_id="test",
        agent_profile_public_dir=Path(__file__).parent / "fixtures/agent_profiles/reference_tool_test",
        agent_tools="time-web",
        hf_realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
    )

    async def exercise():
        runner = make_reception_runtime(options, check_owner=lambda: None)
        service = SessionController({"test": ("robot", runner)})
        run = service.start(object(), Command("start", "profile", "test", "robot"))
        await eventually(lambda: run.phase in {"ready", "faulted"})
        assert run.phase == "ready"
        await service.stop(run, "native_stop")
        assert run.phase == "stopped"

    asyncio.run(exercise())
    assert received[0]["agent_profile"].profile_id == "test"
    assert received[0]["tool_registry"].names() == ["time_now", "web_search"]
    assert "Current date context" in received[0]["instructions"]
    assert received[0]["hf_realtime_ws_url"] == options["hf_realtime_ws_url"]


@pytest.mark.parametrize("vision_runtime", ["serial-v1", "broker-v1"])
def test_service_owns_vision_task_until_stop(tmp_path, fake_io, monkeypatch, vision_runtime):
    calls = []

    async def vision(**kwargs):
        calls.append(kwargs)
        kwargs["ready_event"].set()
        try:
            await asyncio.Future()
        finally:
            calls.append("vision_closed")

    monkeypatch.setattr(live_app, "_vision_loop", vision)
    monkeypatch.setattr(live_app, "_broker_vision_loop", vision)
    options = {**minimal_options(tmp_path), "vision_runtime": vision_runtime, "perception": True}

    async def exercise():
        runner = make_reception_runtime(options, check_owner=lambda: None)
        service = SessionController({"test": ("robot", runner)})
        run = service.start(object(), Command("start", "vision", "test", "robot"))
        await eventually(lambda: run.phase in {"ready", "faulted"})
        assert run.phase == "ready"
        await service.stop(run, "native_stop")
        assert run.phase == "stopped"
        assert calls[-1] == "vision_closed"

    asyncio.run(exercise())
    assert calls[0]["perception_enabled"]
    if vision_runtime == "broker-v1":
        assert isinstance(calls[0]["session_control"], LiveSessionControl)


def test_failing_stop_callback_still_cancels_and_reports_failure():
    async def exercise():
        control = LiveSessionControl(lambda: None)

        def fail():
            raise RuntimeError("test stop callback")

        async def worker():
            control.bind()
            control.on_stop(fail)
            await asyncio.Future()

        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        control.request_stop()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ExceptionGroup, match="cleanup failed"):
            await control.close()

    asyncio.run(exercise())


def test_stop_during_sdk_start_waits_for_cleanup_and_never_starts_backend(tmp_path, fake_io, monkeypatch):
    mini, calls = fake_io
    entered = threading.Event()
    release = threading.Event()

    class DelayedSession(ReachyRobotSession):
        def start(self):
            entered.set()
            assert release.wait(2)
            self.mini = mini
            return mini

    monkeypatch.setattr(live_app, "ReachyRobotSession", DelayedSession)

    async def exercise():
        runner = make_reception_runtime(minimal_options(tmp_path), check_owner=lambda: None)
        service = SessionController({"test": ("robot", runner)})
        run = service.start(object(), Command("start", "startup", "test", "robot"))
        await eventually(entered.is_set)
        stop_task = asyncio.create_task(service.stop(run, "native_stop"))
        await asyncio.sleep(0.03)
        assert not stop_task.done()
        release.set()
        await stop_task
        assert run.phase == "stopped"
        assert "backend_start" not in calls
        assert calls == ["flush", "media_close", "disconnect"]

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_cleanup_failure_latches_service_and_still_disconnects(tmp_path, fake_io):
    mini, calls = fake_io

    def fail_flush():
        raise RuntimeError("flush failed")

    mini.media.audio.clear_player = fail_flush

    async def exercise():
        runner = make_reception_runtime(minimal_options(tmp_path), check_owner=lambda: None)
        service = SessionController({"test": ("robot", runner)})
        run = service.start(object(), Command("start", "flush-error", "test", "robot"))
        await eventually(lambda: run.phase == "ready")
        await service.stop(run, "native_stop")
        assert run.phase == "faulted"
        assert service.fault_latched
        assert "media_close" in calls and "disconnect" in calls
        with pytest.raises(ProtocolError):
            service.start(object(), Command("start", "other", "test", "robot"))

    asyncio.run(exercise())


def test_control_loop_responsive_during_blocking_worker_and_timeout_latches(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    async def blocked_entry(*, control, **options):
        control.bind()
        entered.set()
        try:
            assert release.wait(2)
        finally:
            finished.set()

    async def exercise():
        runner = make_reception_runtime(
            {"artifact_root": tmp_path}, session_entry=blocked_entry,
            check_owner=lambda: None, cleanup_timeout=0.03,
        )
        service = SessionController({"test": ("robot", runner)}, stop_timeout=0.1)
        run = service.start(object(), Command("start", "blocked", "test", "robot"))
        await eventually(entered.is_set)
        started = time.monotonic()
        await service.stop(run, "native_stop")
        assert time.monotonic() - started < 0.5
        assert run.phase == "faulted" and service.fault_latched
        release.set()
        await eventually(finished.is_set)

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_stale_media_uses_existing_liveness_thresholds(tmp_path):
    stopped = threading.Event()

    async def stale_entry(*, control, run_id, **options):
        control.bind()
        control.liveness = RuntimeLiveness(run_id=run_id, audio_expected=True, video_expected=False)
        control.liveness.set_phase("ready")
        control.liveness.pulse_event_loop()
        control.ready()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            stopped.set()

    async def exercise():
        runner = make_reception_runtime(
            {"artifact_root": tmp_path}, session_entry=stale_entry, check_owner=lambda: None,
            thresholds=HealthThresholds(source_stale_s=0.02), poll_interval=0.01,
        )
        service = SessionController({"test": ("robot", runner)})
        run = service.start(object(), Command("start", "stale", "test", "robot"))
        await run.task
        assert stopped.is_set()
        assert run.phase == "faulted" and service.fault_latched

    asyncio.run(exercise())


@pytest.mark.parametrize("cleanup_timeout,stop_timeout", [(0.02, 0.1), (0.2, 0.02)])
@pytest.mark.parametrize("failure", [None, "exception", "stop_callback"])
def test_manual_restart_requires_eventual_worker_cleanup(tmp_path, cleanup_timeout, stop_timeout, failure):
    release = threading.Event()
    cleaning = threading.Event()
    entered = []
    status = ReceptionStatus({"agent_profile_id": "test", "agent_tools": "none",
                              "visitor_trigger_profile": "test"})

    async def entry(*, control, run_id, **options):
        entered.append(run_id)
        control.bind()
        control.ready()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            if run_id == "native-first":
                cleaning.set()
                while not release.is_set():
                    await asyncio.sleep(0.005)
                if failure == "exception":
                    raise RuntimeError("private cleanup exception")
                if failure == "stop_callback":
                    control.cleanup_faults.append("private callback exception")

    async def exercise():
        runner = make_reception_runtime(
            {"artifact_root": tmp_path}, session_entry=entry, status=status,
            check_owner=lambda: None, cleanup_timeout=cleanup_timeout,
        )
        service = SessionController(
            {"test": ("robot", runner)}, stop_timeout=stop_timeout,
            status_reader=status.snapshot, cleanup_reader=status.cleanup_snapshot,
            receipt_dir=tmp_path / "receipts",
        )
        run = service.start(object(), Command("start", "first", "test", "robot"))
        await eventually(lambda: run.phase == "ready")
        await service.stop(run, "control_heartbeat_timeout")
        await eventually(lambda: run.task.done())
        assert cleaning.is_set()
        assert run.phase == "faulted" and service.fault_latched
        assert run.snapshot()["cleanup"]["state"] == "pending"
        with pytest.raises(ProtocolError) as refused:
            service.start(object(), Command("start", "too-early", "test", "robot"))
        assert refused.value.code == "cleanup_pending"
        release.set()
        await eventually(lambda: status.cleanup_snapshot("first")["state"] != "pending")
        # Finishing cleanup does not automatically create a new physical run.
        assert entered == ["native-first"]
        assert service.active is run and service.fault_latched
        if failure:
            with pytest.raises(ProtocolError) as refused:
                service.start(object(), Command("start", "second", "test", "robot"))
            assert refused.value.code == "operator_review_required"
            assert "private" not in json.dumps(run.snapshot())
            return
        second = service.start(object(), Command("start", "second", "test", "robot"))
        receipt = json.loads((tmp_path / "receipts/native-first.json").read_text())
        assert receipt["phase"] == "faulted"
        assert receipt["cleanup"]["state"] == "complete"
        assert receipt["cleanup"]["finished_at"] > 0
        await eventually(lambda: second.phase == "ready")
        assert entered == ["native-first", "native-second"]
        assert not service.fault_latched
        # Stale completion reports cannot certify a later session's cleanup.
        status.worker_finished("first", clean=True)
        assert status.cleanup_snapshot("second")["state"] == "pending"
        await service.stop(second, "native_stop")
        assert second.phase == "stopped"

    try:
        asyncio.run(exercise())
    finally:
        release.set()


def test_existing_cli_owner_prevents_sdk_initialization(tmp_path):
    entered = []

    def occupied():
        raise RuntimeError("existing CLI owner")

    async def entry(**kwargs):
        entered.append(True)

    async def exercise():
        runner = make_reception_runtime({"artifact_root": tmp_path}, session_entry=entry, check_owner=occupied)
        with pytest.raises(RuntimeError, match="CLI owner"):
            await runner(asyncio.Event(), lambda: None, "occupied")
        assert not entered

    asyncio.run(exercise())


@pytest.mark.parametrize("update", [
    {"record_video": "false"}, {"duration": -1}, {"duration": float("inf")},
    {"artifact_root": "relative"}, {"agent_tools": "reference-test"},
    {"scripted_policy_flow": "greet"}, {"broker_gesture_queue_size": 1.5},
    {"hf_realtime_ws_url": "https://example.test"},
])
def test_config_rejects_unsupported_or_ambiguous_settings(tmp_path, update):
    options = {"robot_host": "192.0.2.10", "artifact_root": str(tmp_path),
               "hf_realtime_ws_url": "ws://127.0.0.1:8765/v1/realtime",
               "agent_profile_id": "test", "agent_tools": "none", "agent_profile_format": "overlay",
               "visitor_trigger_profile": "legacy", **update}
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema_version": 1, "options": options}))
    with pytest.raises(ValueError):
        load_runtime_options(path)


def test_config_explicit_profile_tools_and_unbounded_duration(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema_version": 1, "options": {
        "robot_host": "192.0.2.10", "artifact_root": str(tmp_path),
        "hf_realtime_ws_url": "ws://127.0.0.1:8765/v1/realtime",
        "agent_profile_id": "reviewed", "agent_tools": "time-web",
        "agent_profile_format": "hermes", "visitor_trigger_profile": "door-v4-20260827",
    }}))
    options = load_runtime_options(path)
    assert options["duration"] is None
    assert options["record_video"] is False
    assert options["agent_tools"] == "time-web"
    assert options["artifact_root"] == tmp_path
