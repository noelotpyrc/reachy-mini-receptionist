import asyncio
import json
import os
import sys
import types
import wave
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from reachy_mini_brain.official_runtime import (
    ArtifactRecorder,
    AntennaCueController,
    AntennaPulseMove,
    CapabilityRegistry,
    CompositeRuntimeObserver,
    ConversationCuePolicy,
    ConversationCuePolicySettings,
    InMemoryEventSink,
    JsonlEventSink,
    LiveKitRealtimeHandler,
    LiveKitRoomBridge,
    OfficialStyleStreamRuntime,
    PerceptionPipeline,
    PlaybackMovementGate,
    PolicyEngine,
    ReceptionPolicy,
    ReceptionPolicySettings,
    ReachyAudioSink,
    ReachyAudioSource,
    ReachyCameraFrameProvider,
    ReachyRobotSession,
    RulePolicy,
    RuntimeContext,
    RuntimeEvent,
    WavAudioSource,
    camera_question,
    encode_bgr_frame_as_jpeg,
    load_project_env,
    queue_antenna_pulse,
    register_camera_capabilities,
    run_wav_replay,
    set_head_tracking,
)
from reachy_mini_brain.official_runtime.replay_livekit import cli as livekit_replay_cli
from reachy_mini_brain.official_runtime.replay_vision import cli as vision_replay_cli
from reachy_mini_brain.official_runtime.live_app import cli as live_app_cli
from reachy_mini_brain.official_runtime.live_app import _play_cached_policy_speech
from reachy_mini_brain.official_runtime.benchmark_backends import _summarize_run
from reachy_mini_brain.official_runtime.policy_audio_cache import PolicyAudioCache
from reachy_mini_brain import robot


def test_capability_registry_invokes_sync_and_async_capabilities():
    async def run():
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()

        def remember(context, value):
            context.state["value"] = value
            return value

        async def double(context, value):
            return value * 2

        registry.register("remember", remember)
        registry.register("double", double)

        assert await registry.invoke("remember", context, value=21) == 21
        assert await registry.invoke("double", context, value=21) == 42
        assert context.state["value"] == 21
        assert events.kinds() == [
            "capability.started",
            "capability.completed",
            "capability.started",
            "capability.completed",
        ]

    asyncio.run(run())


def test_antenna_pulse_move_evaluates_low_high_low():
    move = AntennaPulseMove(low=(-0.1, 0.1), high=(0.3, -0.3), duration=1.0)

    assert np.allclose(move.evaluate(0.0)[1], np.array([-0.1, 0.1]))
    assert np.allclose(move.evaluate(0.5)[1], np.array([0.3, -0.3]))
    assert np.allclose(move.evaluate(1.0)[1], np.array([-0.1, 0.1]))


def test_queue_antenna_pulse_capability_uses_context_movement_manager():
    manager = _FakeMovementManager()
    context = RuntimeContext(state={"movement_manager": manager})

    assert queue_antenna_pulse(context) is True

    assert len(manager.moves) == 1
    assert isinstance(manager.moves[0], AntennaPulseMove)


def test_antenna_cue_controller_stops_at_rest():
    async def run():
        events = InMemoryEventSink()
        positions = []

        controller = AntennaCueController(
            set_antennas=lambda antennas: positions.append(antennas),
            event_sink=events,
            high=(10.0, 10.0),
            rest=(-5.0, -5.0),
            high_s=0.001,
            rest_s=0.001,
        )

        assert await controller.start(cue="thinking") is True
        await asyncio.sleep(0.005)
        assert await controller.stop(reason="test_done") is True

        assert positions
        assert positions[-1] == (-5.0, -5.0)
        assert "runtime.antenna_cue" in events.kinds()

    asyncio.run(run())


def test_encode_bgr_frame_as_jpeg_returns_jpeg_bytes():
    frame = np.zeros((4, 6, 3), dtype=np.uint8)
    frame[:, :, 2] = 255

    jpeg = encode_bgr_frame_as_jpeg(frame)

    assert jpeg.startswith(b"\xff\xd8")
    assert jpeg.endswith(b"\xff\xd9")


def test_camera_question_uses_local_vision_processor_when_available():
    async def run():
        frame = np.zeros((3, 4, 3), dtype=np.uint8)
        camera = _FakeCameraWorker(frame)
        vision = _FakeVisionProcessor("The lobby is visible.")
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events, state={"camera_worker": camera, "vision_processor": vision})

        result = await camera_question(context, question="What do you see?")

        assert result == {"image_description": "The lobby is visible."}
        assert len(vision.calls) == 1
        assert vision.calls[0][1] == "What do you see?"
        assert "capability.camera_frame" in events.kinds()

    asyncio.run(run())


def test_camera_question_returns_base64_jpeg_without_local_vision():
    async def run():
        frame = np.zeros((3, 4, 3), dtype=np.uint8)
        camera = _FakeCameraWorker(frame)
        context = RuntimeContext(state={"camera_worker": camera})

        result = await camera_question(context, question="What is here?")

        assert result["mime_type"] == "image/jpeg"
        assert result["question"] == "What is here?"
        decoded = __import__("base64").b64decode(result["b64_im"])
        assert decoded.startswith(b"\xff\xd8")

    asyncio.run(run())


def test_register_camera_capabilities_and_summarizes_base64_result_in_events():
    async def run():
        frame = np.zeros((3, 4, 3), dtype=np.uint8)
        camera = _FakeCameraWorker(frame)
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events, state={"camera_worker": camera})
        registry = CapabilityRegistry()
        register_camera_capabilities(registry)

        result = await registry.invoke("camera", context, question="What is here?")

        assert "b64_im" in result
        completed = next(event for event in events.events if event.kind == "capability.completed")
        logged_image = completed.data["result"]["b64_im"]
        assert set(logged_image) == {"base64_chars", "decoded_bytes"}
        assert logged_image["decoded_bytes"] > 0

    asyncio.run(run())


def test_set_head_tracking_toggles_camera_worker():
    async def run():
        camera = _FakeCameraWorker(np.zeros((3, 4, 3), dtype=np.uint8))
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events, state={"camera_worker": camera})

        result = await set_head_tracking(context, start=True)

        assert result == {"status": "head tracking started"}
        assert camera.head_tracking_states == [True]
        assert "capability.head_tracking" in events.kinds()

    asyncio.run(run())


def test_playback_movement_gate_suppresses_and_resumes_motion():
    manager = _FakeMovementManager()
    changes = []
    gate = PlaybackMovementGate(movement_manager=manager, on_change=lambda active, reason: changes.append((active, reason)))

    gate.record_output_audio_frame(16000, np.ones(160, dtype=np.int16), metadata={"response_id": "resp-1"})
    gate.emit(RuntimeEvent(kind="realtime.response.output_audio.done", source="backend"))

    assert changes == [(True, "assistant_audio"), (False, "realtime.response.output_audio.done")]
    assert manager.playback_states == [True, False]
    assert manager.idle_states == [False, True]


def test_policy_engine_routes_trigger_events_to_capabilities():
    async def run():
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        calls = []

        async def greet(context, event, phrase):
            calls.append((event.kind, phrase))
            return {"ok": True}

        registry.register("greet", greet)
        engine = PolicyEngine(
            [
                RulePolicy(
                    name="wave-greet",
                    trigger_kind="vision.wave",
                    capability_name="greet",
                    arguments={"phrase": "hello"},
                )
            ],
            capabilities=registry,
            context=context,
        )

        await engine.start()
        await engine.handle_event(RuntimeEvent(kind="vision.person", source="test"))
        await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test"))
        await engine.stop()

        assert calls == [("vision.wave", "hello")]
        assert "policy.triggered" in events.kinds()
        assert events.kinds().count("capability.completed") == 1

    asyncio.run(run())


def test_conversation_cue_policy_starts_on_transcript_and_stops_on_audio():
    async def run():
        clock = _Clock()
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        calls = []

        async def start_thinking_cue(context, reason=""):
            calls.append(("start", reason))
            return True

        async def stop_thinking_cue(context, reason=""):
            calls.append(("stop", reason))
            return True

        registry.register("start_thinking_cue", start_thinking_cue)
        registry.register("stop_thinking_cue", stop_thinking_cue)
        policy = ConversationCuePolicy(ConversationCuePolicySettings(clock=clock, min_start_interval_s=0.0))
        engine = PolicyEngine([policy], capabilities=registry, context=context)

        await engine.start()
        await engine.handle_event(
            RuntimeEvent(kind="backend.transcript.final", source="backend", data={"text": "Where should I check in?"})
        )
        await engine.handle_event(RuntimeEvent(kind="assistant.audio.started", source="runtime"))
        await engine.handle_event(
            RuntimeEvent(kind="backend.transcript.final", source="backend", data={"text": "One more question"})
        )
        await engine.handle_event(RuntimeEvent(kind="response.done", source="backend"))
        clock.advance(1.0)
        await engine.handle_event(
            RuntimeEvent(kind="backend.transcript.final", source="backend", data={"text": "One more question"})
        )
        await engine.handle_event(RuntimeEvent(kind="assistant.audio.done", source="runtime"))
        clock.advance(1.0)
        await engine.handle_event(
            RuntimeEvent(kind="backend.transcript.final", source="backend", data={"text": "One more question"})
        )
        await engine.stop()

        assert calls == [
            ("start", "backend.transcript.final"),
            ("stop", "assistant.audio.started"),
            ("start", "backend.transcript.final"),
            ("stop", "policy_stop"),
        ]
        assert "policy.conversation_cue.thinking_started" in events.kinds()
        assert "policy.conversation_cue.thinking_stopped" in events.kinds()
        suppressed = [event for event in events.events if event.kind == "policy.conversation_cue.start_suppressed"]
        assert [event.data["reason"] for event in suppressed] == ["robot_speaking", "robot_speaking"]

    asyncio.run(run())


def test_reception_policy_greets_without_opening_audio_gate():
    async def run():
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        calls = []

        async def speak_text(context, text, reason, event):
            calls.append((reason, text))
            return True

        registry.register("speak_text", speak_text)
        policy = ReceptionPolicy(ReceptionPolicySettings(cooldown_s=0.0))
        engine = PolicyEngine([policy], capabilities=registry, context=context)

        await engine.start()
        assert policy.should_forward_audio() is False
        await engine.handle_event(RuntimeEvent(kind="vision.approach", source="test", data={"id": 1}))

        assert policy.should_forward_audio() is False
        assert calls == [("approach", "Welcome!")]
        assert "policy.greet" in events.kinds()

    asyncio.run(run())


def test_reception_policy_wave_opens_gate_and_goodbye_closes_it():
    async def run():
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        calls = []

        async def speak_text(context, text, reason, event):
            calls.append((reason, text))
            return True

        registry.register("speak_text", speak_text)
        policy = ReceptionPolicy(ReceptionPolicySettings(cooldown_s=0.0))
        engine = PolicyEngine([policy], capabilities=registry, context=context)

        await engine.start()
        await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test", data={"gesture": "Open_Palm"}))
        assert policy.should_forward_audio() is True
        assert calls == [("wave", "Hi! How can I help?")]
        wave_received = next(event for event in events.events if event.kind == "policy.wave_received")
        assert wave_received.data["conversation_active"] is False
        assert wave_received.data["cooldown_ready"] is True

        await engine.handle_event(
            RuntimeEvent(
                kind="realtime.conversation.item.input_audio_transcription.completed",
                source="backend",
                data={"transcript": "okay goodbye"},
            )
        )

        assert policy.should_forward_audio() is False
        assert "policy.conversation_opened" in events.kinds()
        assert "policy.conversation_closed" in events.kinds()

    asyncio.run(run())


def test_cached_policy_speech_plays_wav_and_emits_audio_lifecycle(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    audio = np.arange(320, dtype=np.int16)
    _write_pcm_wav(cache_dir / "welcome.wav", 16_000, audio)
    recorder = ArtifactRecorder(tmp_path / "artifacts", run_id="policy-cache", record_audio=True)

    async def run():
        events = InMemoryEventSink()
        sink = _CollectingAudioSink()
        ok = await _play_cached_policy_speech(
            cache=PolicyAudioCache(cache_dir),
            audio_sink=sink,
            event_sink=events,
            recorder=recorder,
            text="Welcome!",
            reason="approach",
            event=RuntimeEvent(kind="policy.greet", source="test"),
        )
        return ok, events, sink

    ok, events, sink = asyncio.run(run())
    recorder.close()

    assert ok is True
    assert len(sink.frames) == 1
    assert sink.frames[0][0] == 16_000
    assert np.array_equal(sink.frames[0][1], audio)
    assert sink.drained is True
    assert events.kinds() == [
        "policy.speech_cache_hit",
        "assistant.audio.started",
        "audio.output_frame",
        "assistant.audio.done",
        "policy.speech_cache_played",
    ]
    output_event = next(event for event in events.events if event.kind == "audio.output_frame")
    assert output_event.data["metadata"]["policy_text"] == "Welcome!"
    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    streams = {entry["stream"]: entry for entry in manifest["artifacts"]["audio"]}
    assert streams["output"]["samples"] == 320


def test_cached_policy_speech_missing_file_does_not_fall_back_to_backend(tmp_path):
    recorder = ArtifactRecorder(tmp_path / "artifacts", run_id="policy-cache-missing", record_audio=True)

    async def run():
        events = InMemoryEventSink()
        sink = _CollectingAudioSink()
        ok = await _play_cached_policy_speech(
            cache=PolicyAudioCache(tmp_path / "missing-cache"),
            audio_sink=sink,
            event_sink=events,
            recorder=recorder,
            text="Welcome!",
            reason="approach",
            event=RuntimeEvent(kind="policy.greet", source="test"),
        )
        return ok, events, sink

    ok, events, sink = asyncio.run(run())
    recorder.close()

    assert ok is False
    assert sink.frames == []
    assert events.kinds() == ["policy.speech_cache_missing"]


def test_reception_policy_idle_tick_closes_conversation():
    async def run():
        clock = _Clock()
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()

        async def speak_text(context, text, reason, event):
            return True

        registry.register("speak_text", speak_text)
        policy = ReceptionPolicy(
            ReceptionPolicySettings(
                cooldown_s=0.0,
                conversation_idle_timeout_s=2.0,
                conversation_max_duration_s=10.0,
                clock=clock,
            )
        )
        engine = PolicyEngine([policy], capabilities=registry, context=context)

        await engine.start()
        await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test"))
        assert policy.should_forward_audio() is True
        clock.advance(2.1)
        await engine.handle_event(RuntimeEvent(kind="runtime.tick", source="test"))

        assert policy.should_forward_audio() is False
        close = next(event for event in events.events if event.kind == "policy.conversation_closed")
        assert close.data["reason"] == "idle_timeout"

    asyncio.run(run())


def test_perception_pipeline_accepts_injected_detector_and_writes_events(tmp_path):
    events_path = tmp_path / "vision-events.jsonl"
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    pipeline = PerceptionPipeline(
        detector=_FakeDetector([{"id": 1}]),
        tracker_factory=lambda frame_wh: _FakeTracker([{"kind": "approach", "id": 1, "area": 0.2}]),
        events_path=events_path,
    )

    events, people, tracks = pipeline.process(frame)

    assert events == [{"kind": "approach", "id": 1, "area": 0.2}]
    assert people == 1
    assert tracks == [{"id": 1, "area": 0.2}]
    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["type"] == "approach"
    assert rows[0]["id"] == 1


def test_perception_pipeline_applies_wave_cooldown_with_injected_gesture_detector():
    clock = _Clock()
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    pipeline = PerceptionPipeline(
        detector=_FakeDetector([]),
        tracker_factory=lambda frame_wh: _FakeTracker(),
        gestures=True,
        gesture_detector=_FakeGestureDetector(),
        gesture_cooldown=3.0,
        clock=clock,
    )

    first, _, _ = pipeline.process(frame)
    second, _, _ = pipeline.process(frame)
    clock.advance(3.1)
    third, _, _ = pipeline.process(frame)

    assert first == [{"kind": "wave", "gesture": "Open_Palm", "score": 0.92}]
    assert second == []
    assert third == [{"kind": "wave", "gesture": "Open_Palm", "score": 0.92}]


def test_perception_pipeline_emits_gesture_diagnostics_for_candidate_and_cooldown():
    clock = _Clock()
    diagnostics = InMemoryEventSink()
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    pipeline = PerceptionPipeline(
        detector=_FakeDetector([]),
        tracker_factory=lambda frame_wh: _FakeTracker(),
        gestures=True,
        gesture_detector=_FakeGestureDetector(),
        gesture_cooldown=3.0,
        clock=clock,
        event_sink=diagnostics,
    )

    pipeline.ensure_gesture_detector()
    first, _, _ = pipeline.process(frame)
    second, _, _ = pipeline.process(frame)

    assert first == [{"kind": "wave", "gesture": "Open_Palm", "score": 0.92}]
    assert second == []
    assert diagnostics.kinds() == [
        "vision.gesture_detector_ready",
        "vision.gesture_candidate",
        "vision.gesture_emitted",
        "vision.gesture_candidate",
        "vision.gesture_suppressed",
    ]
    suppressed = diagnostics.events[-1]
    assert suppressed.data["reason"] == "cooldown"
    assert suppressed.data["remaining_s"] == 3.0


def test_perception_pipeline_emits_below_threshold_gesture_candidate():
    diagnostics = InMemoryEventSink()
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    pipeline = PerceptionPipeline(
        detector=_FakeDetector([]),
        tracker_factory=lambda frame_wh: _FakeTracker(),
        gestures=True,
        gesture_detector=_FakeGestureDetector(result=("Open_Palm", 0.42)),
        event_sink=diagnostics,
    )

    events, _, _ = pipeline.process(frame)

    assert events == []
    candidate = next(event for event in diagnostics.events if event.kind == "vision.gesture_candidate")
    assert candidate.data["gesture"] == "Open_Palm"
    assert candidate.data["accepted"] is False
    assert candidate.data["reason"] == "below_threshold"


def test_vision_replay_cli_help_loads_without_detector_dependencies():
    result = CliRunner().invoke(vision_replay_cli, ["--help"])

    assert result.exit_code == 0
    assert "Replay recorded video" in result.output


def test_official_runtime_live_cli_help_loads_without_robot_dependencies():
    result = CliRunner().invoke(live_app_cli, ["--help"])

    assert result.exit_code == 0
    assert "Run the ported official-runtime path" in result.output
    assert "--hf-connection-mode" in result.output
    assert "--ready-cue" in result.output


def test_reachy_audio_source_reads_fake_robot_audio_as_int16():
    async def run():
        sample = np.array([[0.5, -0.5], [0.25, -0.25]], dtype=np.float32)
        mini = _FakeMini(_FakeMedia(audio_samples=[sample]))
        source = ReachyAudioSource(mini, poll_interval_s=0.0, max_duration_s=1.0)
        return await source.read()

    frame = asyncio.run(run())

    assert frame is not None
    sample_rate, audio = frame
    assert sample_rate == 16_000
    assert audio.dtype == np.int16
    assert audio.shape == (2,)


def test_robot_ensure_ready_starts_stopped_daemon(monkeypatch):
    statuses = iter(
        [
            {"state": "stopped"},
            {"state": "running", "backend_status": {"ready": False, "motor_control_mode": "disabled"}},
            {"state": "running", "backend_status": {"ready": False, "motor_control_mode": "enabled"}},
            {"state": "running", "backend_status": {"ready": True, "motor_control_mode": "enabled"}},
        ]
    )
    posts = []

    def fake_get(path, **params):
        if path == "/api/daemon/status":
            return next(statuses)
        if path == "/api/motors/status":
            mode = "enabled" if any(post[0] == "/api/motors/set_mode/enabled" for post in posts) else "disabled"
            return {"mode": mode}
        raise AssertionError(path)

    def fake_post(path, json=None, **params):
        posts.append((path, params))
        return {}

    monkeypatch.setattr(robot, "_last_ready_at", 0.0)
    monkeypatch.setattr(robot, "_session_active", False)
    monkeypatch.setattr(robot, "_get", fake_get)
    monkeypatch.setattr(robot, "_post", fake_post)
    monkeypatch.setattr(robot.time, "sleep", lambda seconds: None)

    robot.ensure_ready()

    assert posts == [
        ("/api/daemon/start", {"wake_up": "false"}),
        ("/api/motors/set_mode/enabled", {}),
    ]


def test_robot_ensure_ready_accepts_usable_control_when_ready_flag_stays_false(monkeypatch):
    statuses = iter(
        [
            {"state": "running", "backend_status": {"ready": False, "motor_control_mode": "enabled"}},
        ]
        + [
            {"state": "running", "backend_status": {"ready": False, "motor_control_mode": "enabled"}}
            for _ in range(30)
        ]
    )

    def fake_get(path, **params):
        if path == "/api/daemon/status":
            return next(statuses)
        if path == "/api/motors/status":
            return {"mode": "enabled"}
        if path == "/api/state/full":
            return {"control_mode": "enabled", "head_pose": {}}
        raise AssertionError(path)

    monkeypatch.setattr(robot, "_last_ready_at", 0.0)
    monkeypatch.setattr(robot, "_session_active", False)
    monkeypatch.setattr(robot, "_get", fake_get)
    monkeypatch.setattr(robot, "_post", lambda *args, **kwargs: {})
    monkeypatch.setattr(robot.time, "sleep", lambda seconds: None)

    robot.ensure_ready()


def test_reachy_robot_session_uses_explicit_network_host(monkeypatch):
    constructed = []

    class FakeReachyMini:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    fake_module = types.SimpleNamespace(ReachyMini=FakeReachyMini)
    monkeypatch.setitem(sys.modules, "reachy_mini", fake_module)
    monkeypatch.setattr(robot, "ensure_ready", lambda: None)
    monkeypatch.setattr(robot, "_session_active", False)

    session = ReachyRobotSession(
        host="192.168.1.165",
        warmup_audio=False,
        warmup_video=False,
    )

    session.start()
    session.stop()

    assert constructed == [
        {
            "host": "192.168.1.165",
            "connection_mode": "network",
            "timeout": 15.0,
        }
    ]


def test_reachy_robot_session_reports_startup_milestones(monkeypatch):
    monkeypatch.setattr(robot, "ensure_ready", lambda: None)
    monkeypatch.setattr(robot, "_session_active", False)
    milestones = []
    mini = _FakeMini(
        _FakeMedia(
            audio_samples=[np.ones(160, dtype=np.int16)],
            frame=np.zeros((2, 2, 3), dtype=np.uint8),
        )
    )
    session = ReachyRobotSession(
        host="192.168.1.165",
        warmup_audio=True,
        warmup_video=True,
        robot_factory=lambda: mini,
        milestone_callback=lambda name, data: milestones.append((name, data)),
    )

    session.start()
    session.stop()

    names = [name for name, _data in milestones]
    assert names == [
        "robot_host_selected",
        "robot_control_check_start",
        "robot_control_ready",
        "robot_sdk_connect_start",
        "robot_sdk_connected",
        "robot_audio_warmup_start",
        "robot_audio_warmup_ok",
        "robot_video_warmup_start",
        "robot_video_warmup_ok",
        "robot_session_stop_start",
        "robot_session_stop_done",
    ]
    assert milestones[0][1] == {"host": "192.168.1.165", "connection_mode": "network"}


def test_reachy_audio_sink_pushes_float32_robot_audio():
    async def run():
        mini = _FakeMini(_FakeMedia())
        sink = ReachyAudioSink(mini)
        await sink.write((16_000, np.array([0, 32767], dtype=np.int16)))
        await sink.drain()
        await sink.close()
        return mini.media.pushed

    pushed = asyncio.run(run())

    assert len(pushed) == 1
    assert pushed[0].dtype == np.float32
    assert pushed[0].shape == (2,)
    assert pushed[0][1] > 0.99


def test_reachy_audio_sink_pushes_one_backend_tuple_without_python_pacing():
    async def run():
        mini = _FakeMini(_FakeMedia())
        sink = ReachyAudioSink(mini)
        await sink.write((16_000, np.zeros(800, dtype=np.int16)))
        await sink.drain()
        await sink.close()
        return mini.media.pushed

    pushed = asyncio.run(run())

    assert len(pushed) == 1
    assert pushed[0].dtype == np.float32
    assert pushed[0].shape == (800,)


def test_reachy_audio_sink_resamples_to_robot_output_rate_before_push():
    async def run():
        mini = _FakeMini(_FakeMedia(output_sample_rate=16_000))
        sink = ReachyAudioSink(mini)
        await sink.write((24_000, np.zeros(1_200, dtype=np.int16)))
        await sink.drain()
        await sink.close()
        return mini.media.pushed

    pushed = asyncio.run(run())

    assert len(pushed) == 1
    assert pushed[0].dtype == np.float32
    assert pushed[0].shape == (800,)


def test_reachy_audio_sink_uses_first_channel_like_official_app():
    async def run():
        mini = _FakeMini(_FakeMedia())
        sink = ReachyAudioSink(mini)
        stereo = np.array(
            [
                [0, 32767],
                [32767, 0],
                [0, 32767],
                [32767, 0],
            ],
            dtype=np.int16,
        )
        await sink.write((16_000, stereo))
        await sink.close()
        return mini.media.pushed

    pushed = asyncio.run(run())

    assert len(pushed) == 1
    assert pushed[0].tolist() == pytest.approx([0.0, 0.9999695, 0.0, 0.9999695])


def test_reachy_camera_frame_provider_gets_frame_and_tracks_toggle():
    frame = np.ones((3, 4, 3), dtype=np.uint8)
    provider = ReachyCameraFrameProvider(_FakeMini(_FakeMedia(frame=frame)))

    got = provider.get_latest_frame()
    provider.set_head_tracking_enabled(True)

    assert np.array_equal(got, frame)
    assert provider.head_tracking_enabled is True


class _FiniteAudioSource:
    def __init__(self, frames):
        self.frames = list(frames)

    async def read(self):
        if not self.frames:
            return None
        await asyncio.sleep(0)
        return self.frames.pop(0)


class _CollectingAudioSink:
    def __init__(self):
        self.frames = []
        self.drained = False

    async def write(self, frame):
        self.frames.append(frame)

    async def drain(self):
        self.drained = True


class _EchoHandler:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.received = []
        self.outputs = asyncio.Queue()

    async def start_up(self):
        self.started = True

    async def shutdown(self):
        self.stopped = True

    async def receive(self, frame):
        self.received.append(frame)
        sample_rate, audio = frame
        await self.outputs.put({"role": "user", "samples": int(audio.shape[0])})
        await self.outputs.put((sample_rate, audio.copy()))

    async def emit(self):
        try:
            return self.outputs.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0)
            return None


class _QueuedOutputHandler:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.started = False
        self.stopped = False

    async def start_up(self):
        self.started = True

    async def shutdown(self):
        self.stopped = True

    async def receive(self, frame):
        return None

    async def emit(self):
        if self.outputs:
            await asyncio.sleep(0)
            return self.outputs.pop(0)
        await asyncio.sleep(0)
        return None


class _MetadataAudioHandler(_EchoHandler):
    async def receive(self, frame):
        sample_rate, audio = frame
        await self.outputs.put((sample_rate, audio.copy(), {"response_id": "resp-test"}))


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _FakeDetector:
    def __init__(self, people=None):
        self.people = people if people is not None else [{"id": 1}]

    def detect(self, frame, *, bgr=False):
        return list(self.people)


class _FakeTracker:
    def __init__(self, events=None):
        self.events = list(events or [])
        self.frame_debug = [{"id": 1, "area": 0.2}]

    @property
    def debug_state(self):
        return {"fake": True}

    def update(self, persons):
        return list(self.events)


class _FakeGestureDetector:
    def __init__(self, result=("Open_Palm", 0.92), gestures=("Open_Palm",), threshold=0.5):
        self.result = result
        self.gestures = gestures
        self.threshold = threshold
        self.model_path = "/tmp/fake-gesture.task"

    def detect_candidate(self, frame):
        return self.result

    def detect(self, frame):
        if self.result is None:
            return None
        name, score = self.result
        if name in set(self.gestures) and score >= self.threshold:
            return self.result
        return None


class _FakeMovementManager:
    def __init__(self):
        self.moves = []
        self.playback_states = []
        self.idle_states = []

    def queue_move(self, move):
        self.moves.append(move)

    def set_playback_active(self, active):
        self.playback_states.append(active)

    def set_idle_breathing_enabled(self, enabled):
        self.idle_states.append(enabled)


class _FakeCameraWorker:
    def __init__(self, frame=None):
        self.frame = frame
        self.head_tracking_states = []

    def get_latest_frame(self):
        return None if self.frame is None else self.frame.copy()

    def set_head_tracking_enabled(self, enabled):
        self.head_tracking_states.append(bool(enabled))


class _FakeVisionProcessor:
    def __init__(self, response="a front desk"):
        self.response = response
        self.calls = []

    def process_image(self, frame, prompt):
        self.calls.append((frame.copy(), prompt))
        return self.response


class _FakeMedia:
    def __init__(self, audio_samples=None, frame=None, output_sample_rate=16_000):
        self.audio_samples = list(audio_samples or [])
        self.frame = frame
        self.output_sample_rate = output_sample_rate
        self.pushed = []

    def get_audio_sample(self):
        if not self.audio_samples:
            return None
        return self.audio_samples.pop(0)

    def push_audio_sample(self, sample):
        self.pushed.append(sample.copy())

    def get_output_audio_samplerate(self):
        return self.output_sample_rate

    def get_frame(self):
        return None if self.frame is None else self.frame.copy()


class _FakeMini:
    def __init__(self, media):
        self.media = media


def test_stream_runtime_pumps_audio_through_official_style_handler():
    async def run():
        input_frame = (16_000, np.arange(160, dtype=np.int16))
        events = InMemoryEventSink()
        handler = _EchoHandler()
        sink = _CollectingAudioSink()
        runtime = OfficialStyleStreamRuntime(
            handler=handler,
            audio_source=_FiniteAudioSource([input_frame]),
            audio_sink=sink,
            event_sink=events,
        )

        await runtime.run()

        assert handler.started
        assert handler.stopped
        assert handler.received == [input_frame]
        assert len(sink.frames) == 1
        assert np.array_equal(sink.frames[0][1], input_frame[1])
        kinds = events.kinds()
        assert kinds[0] == "runtime.started"
        assert kinds[1] == "runtime.handler_started"
        assert kinds[2] == "runtime.input_starting"
        assert "audio.input_frame" in kinds
        assert "audio.input_done" in kinds
        assert "handler.output" in kinds
        assert "audio.output_frame" in kinds
        assert kinds[-1] == "runtime.stopped"
        input_frame_event = next(event for event in events.events if event.kind == "audio.input_frame")
        assert input_frame_event.data["duration_s"] == 0.01

    asyncio.run(run())


def test_stream_runtime_emits_conversation_cue_semantic_events():
    async def run():
        audio = np.arange(160, dtype=np.int16)
        events = InMemoryEventSink()
        handler = _QueuedOutputHandler(
            [
                {"role": "user", "content": "Where should I check in?"},
                (16_000, audio),
            ]
        )
        sink = _CollectingAudioSink()
        runtime = OfficialStyleStreamRuntime(
            handler=handler,
            audio_source=_FiniteAudioSource([]),
            audio_sink=sink,
            event_sink=events,
        )

        await runtime.run()
        return events.kinds()

    kinds = asyncio.run(run())

    assert "assistant.thinking.started" in kinds
    assert "assistant.audio.started" in kinds
    assert "assistant.audio.done" in kinds
    assert kinds.index("assistant.thinking.started") < kinds.index("handler.output")
    assert kinds.index("assistant.audio.started") < kinds.index("audio.output_frame")


def test_stream_runtime_calls_on_ready_before_input_starts():
    async def run():
        order = []
        events = InMemoryEventSink()
        handler = _EchoHandler()
        sink = _CollectingAudioSink()

        async def on_ready():
            order.append("ready")

        class Source:
            async def read(self):
                order.append("read")
                return None

        runtime = OfficialStyleStreamRuntime(
            handler=handler,
            audio_source=Source(),
            audio_sink=sink,
            event_sink=events,
            on_ready=on_ready,
        )

        await runtime.run()
        return order, events.kinds()

    order, kinds = asyncio.run(run())

    assert order == ["ready", "read"]
    assert kinds[:3] == ["runtime.started", "runtime.handler_started", "runtime.input_starting"]


def test_wav_source_chunks_pcm_wav(tmp_path):
    path = tmp_path / "input.wav"
    audio = np.arange(320, dtype=np.int16)
    _write_pcm_wav(path, 16_000, audio)

    async def run():
        source = WavAudioSource(path, frame_duration_ms=10)
        try:
            first = await source.read()
            second = await source.read()
            done = await source.read()
        finally:
            source.close()

        assert first is not None
        assert second is not None
        assert done is None
        assert first[0] == 16_000
        assert np.array_equal(first[1], audio[:160])
        assert np.array_equal(second[1], audio[160:])

    asyncio.run(run())


def test_run_wav_replay_collects_output_wav_and_events(tmp_path):
    input_path = tmp_path / "input.wav"
    output_path = tmp_path / "output.wav"
    audio = np.arange(320, dtype=np.int16)
    _write_pcm_wav(input_path, 16_000, audio)

    async def run():
        events = InMemoryEventSink()
        await run_wav_replay(
            input_wav=input_path,
            output_wav=output_path,
            handler=_EchoHandler(),
            event_sink=events,
            frame_duration_ms=10,
        )
        return events

    events = asyncio.run(run())

    with wave.open(str(output_path), "rb") as wav:
        assert wav.getframerate() == 16_000
        assert wav.getnchannels() == 1
        assert wav.getnframes() == 320
        output_audio = np.frombuffer(wav.readframes(320), dtype="<i2")

    assert np.array_equal(output_audio, audio)
    kinds = events.kinds()
    assert kinds[0] == "runtime.started"
    assert kinds[-1] == "runtime.stopped"
    assert kinds.count("audio.input_frame") == 2
    assert kinds.count("audio.input_done") == 1
    assert kinds.count("handler.output") == 2
    assert kinds.count("audio.output_frame") == 2


def test_stream_runtime_accepts_official_metadata_audio_tuple(tmp_path):
    input_path = tmp_path / "input.wav"
    output_path = tmp_path / "output.wav"
    audio = np.arange(320, dtype=np.int16)
    _write_pcm_wav(input_path, 16_000, audio)

    async def run():
        events = InMemoryEventSink()
        await run_wav_replay(
            input_wav=input_path,
            output_wav=output_path,
            handler=_MetadataAudioHandler(),
            event_sink=events,
            frame_duration_ms=20,
        )
        return events

    events = asyncio.run(run())

    assert events.kinds().count("audio.output_frame") == 1
    output_event = next(event for event in events.events if event.kind == "audio.output_frame")
    assert output_event.data["samples"] == 320
    assert output_event.data["metadata"] == {"response_id": "resp-test"}
    with wave.open(str(output_path), "rb") as wav:
        assert wav.getnframes() == 320


def test_stream_runtime_taps_audio_into_artifact_recorder(tmp_path):
    pytest.importorskip("soundfile")
    input_path = tmp_path / "input.wav"
    output_path = tmp_path / "output.wav"
    audio = np.arange(320, dtype=np.int16)
    _write_pcm_wav(input_path, 16_000, audio)
    recorder = ArtifactRecorder(tmp_path / "artifacts", run_id="runtime-tap", record_audio=True)

    async def run():
        await run_wav_replay(
            input_wav=input_path,
            output_wav=output_path,
            handler=_MetadataAudioHandler(),
            event_sink=recorder,
            frame_duration_ms=20,
            runtime_options={"runtime_observer": recorder},
        )

    asyncio.run(run())
    recorder.close()

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    streams = {entry["stream"]: entry for entry in manifest["artifacts"]["audio"]}
    assert {"input", "output", "response-resp-test"}.issubset(streams)
    assert streams["input"]["samples"] == 320
    assert streams["output"]["samples"] == 320
    assert manifest["responses"]["resp-test"]["audio_stream"] == "response-resp-test"

    events_path = tmp_path / "artifacts" / "events" / "events-runtime-tap-01.jsonl"
    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    input_row = next(row for row in rows if row["type"] == "audio.input_frame")
    output_row = next(row for row in rows if row["type"] == "audio.output_frame")
    assert input_row["forwarded"] is True
    assert output_row["metadata"]["response_id"] == "resp-test"


def test_composite_observer_records_input_even_when_reception_gate_blocks_backend(tmp_path):
    pytest.importorskip("soundfile")
    input_path = tmp_path / "input.wav"
    output_path = tmp_path / "output.wav"
    audio = np.arange(320, dtype=np.int16)
    _write_pcm_wav(input_path, 16_000, audio)
    recorder = ArtifactRecorder(tmp_path / "artifacts", run_id="gate-test", record_audio=True)
    policy = ReceptionPolicy()
    observer = CompositeRuntimeObserver(policy, recorder)
    handler = _EchoHandler()

    async def run():
        await run_wav_replay(
            input_wav=input_path,
            output_wav=output_path,
            handler=handler,
            event_sink=recorder,
            frame_duration_ms=20,
            runtime_options={"runtime_observer": observer},
        )

    asyncio.run(run())
    recorder.close()

    assert handler.received == []
    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    streams = {entry["stream"]: entry for entry in manifest["artifacts"]["audio"]}
    assert streams["input"]["samples"] == 320
    assert "output" not in streams

    input_meta_path = Path(streams["input"]["metadata"])
    chunk = json.loads(input_meta_path.read_text(encoding="utf-8").splitlines()[0])
    assert chunk["forwarded"] is False


def test_wav_source_reads_float_wav_when_soundfile_available(tmp_path):
    sf = pytest.importorskip("soundfile")
    path = tmp_path / "float.wav"
    sf.write(path, np.linspace(-0.5, 0.5, 320, dtype=np.float32), 16_000, subtype="FLOAT")

    async def run():
        source = WavAudioSource(path, frame_duration_ms=20)
        try:
            frame = await source.read()
        finally:
            source.close()
        return frame

    frame = asyncio.run(run())

    assert frame is not None
    sample_rate, audio = frame
    assert sample_rate == 16_000
    assert audio.dtype == np.int16
    assert audio.shape == (320,)


def test_benchmark_summary_uses_input_done_to_first_output_audio(tmp_path):
    events = InMemoryEventSink()
    events.emit(RuntimeEvent(kind="runtime.started", source="test", ts=10.0))
    events.emit(RuntimeEvent(kind="audio.input_frame", source="test", data={"samples": 160}, ts=10.1))
    events.emit(
        RuntimeEvent(
            kind="audio.input_done",
            source="test",
            data={"frames": 1, "samples": 160, "sample_rate": 16000, "duration_s": 0.01},
            ts=10.2,
        )
    )
    events.emit(RuntimeEvent(kind="audio.output_frame", source="test", data={"samples": 320}, ts=11.0))
    events.emit(RuntimeEvent(kind="runtime.stopped", source="test", ts=11.5))

    summary = _summarize_run(
        batch_id="batch",
        backend="fake",
        input_wav=tmp_path / "input.wav",
        run_dir=tmp_path,
        output_wav=tmp_path / "output.wav",
        events_jsonl=tmp_path / "events.jsonl",
        events=events.events,
        status="completed",
    )

    assert summary.input_done_to_first_output_audio_s == 0.8
    assert summary.input_start_to_first_output_audio_s == 0.9
    assert summary.output_audio_frames == 1
    assert summary.output_audio_samples == 320


def test_benchmark_summary_counts_channel_first_audio_samples(tmp_path):
    events = InMemoryEventSink()
    events.emit(RuntimeEvent(kind="runtime.started", source="test", ts=1.0))
    events.emit(
        RuntimeEvent(
            kind="audio.output_frame",
            source="test",
            data={"samples": 1600, "duration_s": 0.1},
            ts=2.0,
        )
    )

    summary = _summarize_run(
        batch_id="batch",
        backend="fake",
        input_wav=tmp_path / "input.wav",
        run_dir=tmp_path,
        output_wav=tmp_path / "output.wav",
        events_jsonl=tmp_path / "events.jsonl",
        events=events.events,
        status="completed",
    )

    assert summary.output_audio_samples == 1600


class _FakeLiveKitBridge:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.sent = []
        self.outputs = asyncio.Queue()

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send_audio(self, frame):
        self.sent.append(frame)
        sample_rate, audio = frame
        await self.outputs.put({"role": "user_partial", "samples": int(audio.shape[0])})
        await self.outputs.put((sample_rate, audio.copy()))

    async def next_output(self):
        try:
            return self.outputs.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0)
            return None


def test_livekit_handler_wraps_bridge_with_official_handler_contract():
    async def run():
        events = InMemoryEventSink()
        bridge = _FakeLiveKitBridge()
        handler = LiveKitRealtimeHandler(bridge=bridge, event_sink=events)
        frame = (16_000, np.arange(160, dtype=np.int16))

        await handler.start_up()
        await handler.receive(frame)
        event_item = await handler.emit()
        audio_item = await handler.emit()
        await handler.shutdown()

        assert bridge.started
        assert bridge.stopped
        assert bridge.sent == [frame]
        assert event_item == {"role": "user_partial", "samples": 160}
        assert audio_item is not None
        assert audio_item[0] == 16_000
        assert np.array_equal(audio_item[1], frame[1])
        assert events.kinds() == [
            "livekit.handler.starting",
            "livekit.handler.started",
            "livekit.audio.sent",
            "livekit.output.event",
            "livekit.output.audio",
            "livekit.handler.stopped",
        ]

    asyncio.run(run())


def test_jsonl_event_sink_writes_runtime_events(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(path)

    sink.emit(RuntimeEvent(kind="test.event", source="test", data={"value": 1}, ts=123.0))

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "data": {"value": 1},
            "kind": "test.event",
            "source": "test",
            "ts": 123.0,
        }
    ]


def test_artifact_recorder_writes_manifest_and_runtime_jsonl(tmp_path):
    recorder = ArtifactRecorder(tmp_path, run_id="artifact-test", config={"mode": "test"})

    recorder.emit(RuntimeEvent(kind="runtime.started", source="test", data={"value": 1}, ts=123.0))
    recorder.emit(RuntimeEvent(kind="policy.greet", source="reception", data={"text": "hello"}, ts=124.0))
    recorder.emit(RuntimeEvent(kind="livekit.output.event", source="backend", data={"role": "assistant"}, ts=125.0))
    recorder.close()

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == "artifact-test"
    assert manifest["config"]["mode"] == "test"
    assert manifest["ended_ts"] >= manifest["started_ts"]

    events_path = tmp_path / "events" / "events-artifact-test-01.jsonl"
    policy_path = tmp_path / "policies" / "policies-artifact-test-01.jsonl"
    realtime_path = tmp_path / "realtime" / "realtime-artifact-test-01.jsonl"
    event_rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    policy_rows = [json.loads(line) for line in policy_path.read_text(encoding="utf-8").splitlines()]
    realtime_rows = [json.loads(line) for line in realtime_path.read_text(encoding="utf-8").splitlines()]

    assert event_rows[0]["type"] == "run.started"
    assert any(row["type"] == "runtime.started" and row["source"] == "test" for row in event_rows)
    assert policy_rows[0]["type"] == "greet"
    assert realtime_rows[0]["type"] == "livekit.output.event"


def test_artifact_recorder_sanitizes_reserved_runtime_payload_keys(tmp_path):
    recorder = ArtifactRecorder(tmp_path, run_id="artifact-reserved")

    recorder.emit(
        RuntimeEvent(
            kind="runtime.ready_cue",
            source="test",
            data={"kind": "antenna", "type": "cue", "source": "robot", "phase": "high"},
            ts=123.0,
        )
    )
    recorder.close()

    events_path = tmp_path / "events" / "events-artifact-reserved-01.jsonl"
    rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    ready_row = next(row for row in rows if row["type"] == "runtime.ready_cue")

    assert ready_row["source"] == "test"
    assert ready_row["payload_kind"] == "antenna"
    assert ready_row["payload_type"] == "cue"
    assert ready_row["payload_source"] == "robot"
    assert ready_row["phase"] == "high"


def test_artifact_recorder_writes_session_snapshot_and_response_audio(tmp_path):
    pytest.importorskip("soundfile")
    recorder = ArtifactRecorder(tmp_path, run_id="audio-test", config={"mode": "test"}, record_audio=True)

    recorder.record_session_snapshot(
        {
            "backend_provider": "huggingface",
            "session_id": "session-123",
            "resolved_voice": "Sohee",
            "tool_names": ["camera"],
        }
    )
    recorder.record_input_audio_frame(16000, np.ones(160, dtype=np.int16), forwarded=False)
    recorder.record_output_audio_frame(
        16000,
        np.ones(160, dtype=np.float32) * 0.1,
        metadata={"response_id": "resp/one", "response_audio_chunk": 1},
    )
    recorder.record_response_metadata("resp/one", {"transcript": "hello"})
    recorder.close()

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    assert manifest["session"]["backend_provider"] == "huggingface"
    assert manifest["session"]["session_id"] == "session-123"
    assert manifest["session"]["resolved_voice"] == "Sohee"

    response = manifest["responses"]["resp/one"]
    assert response["transcript"] == "hello"
    assert response["audio_stream"].startswith("response-resp_one")
    assert response["audio_path"]
    assert response["audio_metadata"]

    streams = {entry["stream"]: entry for entry in manifest["artifacts"]["audio"]}
    assert {"input", "output", response["audio_stream"]}.issubset(streams)
    input_meta_path = Path(streams["input"]["metadata"])
    input_chunk = json.loads(input_meta_path.read_text(encoding="utf-8").splitlines()[0])
    assert input_chunk["forwarded"] is False

    response_audio_path = Path(response["audio_path"])
    response_meta_path = Path(response["audio_metadata"])
    assert response_audio_path.is_file()
    chunk = json.loads(response_meta_path.read_text(encoding="utf-8").splitlines()[0])
    assert chunk["response_id"] == "resp/one"
    assert chunk["response_audio_chunk"] == 1


def test_load_project_env_reads_dotenv_without_overriding_shell_env(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# local LiveKit test config",
                "LIVEKIT_URL=wss://example.livekit.cloud",
                "LIVEKIT_API_KEY=from-file",
                "LIVEKIT_API_SECRET='quoted secret'",
                "LIVEKIT_ROOM=clinic-test # comment",
                "export LIVEKIT_AGENT_NAME=reachy-mini-test",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LIVEKIT_API_KEY", "from-shell")
    for key in ["LIVEKIT_URL", "LIVEKIT_API_SECRET", "LIVEKIT_ROOM", "LIVEKIT_AGENT_NAME"]:
        monkeypatch.delenv(key, raising=False)

    loaded_path = load_project_env(env_path)

    assert loaded_path == env_path
    assert os.environ["LIVEKIT_URL"] == "wss://example.livekit.cloud"
    assert os.environ["LIVEKIT_API_KEY"] == "from-shell"
    assert os.environ["LIVEKIT_API_SECRET"] == "quoted secret"
    assert os.environ["LIVEKIT_ROOM"] == "clinic-test"
    assert os.environ["LIVEKIT_AGENT_NAME"] == "reachy-mini-test"


def test_livekit_room_bridge_reports_missing_credentials_before_network():
    async def run():
        bridge = LiveKitRoomBridge(
            config=__import__(
                "reachy_mini_brain.official_runtime",
                fromlist=["LiveKitBackendConfig"],
            ).LiveKitBackendConfig(url="ws://example.invalid")
        )
        try:
            await bridge.start()
        except RuntimeError as exc:
            return str(exc)
        raise AssertionError("expected RuntimeError")

    message = asyncio.run(run())

    assert "LiveKit token is required" in message


def test_livekit_replay_cli_writes_failed_manifest_when_credentials_missing(tmp_path, monkeypatch):
    for key in ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_TOKEN", "LIVEKIT_ROOM"]:
        monkeypatch.delenv(key, raising=False)

    input_path = tmp_path / "input.wav"
    artifact_root = tmp_path / "artifacts"
    _write_pcm_wav(input_path, 16_000, np.arange(160, dtype=np.int16))

    result = CliRunner().invoke(
        livekit_replay_cli,
        [
            str(input_path),
            "--run-id",
            "missing-livekit",
            "--artifact-root",
            str(artifact_root),
            "--url",
            "ws://example.invalid",
            "--no-real-time",
        ],
    )

    assert result.exit_code != 0
    run_dir = artifact_root / "missing-livekit"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "LiveKit token is required" in manifest["error"]
    assert (run_dir / "input.wav").exists()
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "transcript.jsonl").exists()
    assert (run_dir / "transcript.jsonl").read_text(encoding="utf-8") == ""


def _write_pcm_wav(path, sample_rate, audio):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(np.asarray(audio, dtype="<i2").tobytes())
