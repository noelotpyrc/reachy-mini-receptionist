import asyncio
import hashlib
import json
import os
import subprocess
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
    CompositeEventSink,
    CompositeRuntimeObserver,
    ConversationCuePolicy,
    ConversationCuePolicySettings,
    GestureDetector,
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
    S2SRealtimeHandler,
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
from reachy_mini_brain.official_runtime import live_app as live_app_module
from reachy_mini_brain.official_runtime.live_app import cli as live_app_cli
from reachy_mini_brain.official_runtime.live_app import (
    _AsyncPolicyEventSink,
    _broker_vision_loop,
    _load_backend_instructions,
    _play_cached_policy_speech,
    _register_handler_conversation_session,
    _register_handler_policy_speech,
    _run_policy_tick_loop,
    _run_scripted_playback_wav,
)
from reachy_mini_brain.official_runtime.benchmark_backends import _summarize_run
from reachy_mini_brain.official_runtime.benchmark_backends import cli as backend_benchmark_cli
from reachy_mini_brain.official_runtime.policy_audio_cache import PolicyAudioCache
from reachy_mini_brain.official_runtime.perception import GestureFrameObservation
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
        assert calls == [("approach", "Welcome to the clinic, how can I help?")]
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


def test_reception_policy_confirmed_depart_closes_active_conversation_and_speaks() -> None:
    async def run() -> None:
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
        await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test"))
        await engine.handle_event(RuntimeEvent(kind="vision.depart", source="door_policy"))

        assert policy.conversation_active is False
        assert calls == [
            ("wave", "Hi! How can I help?"),
            ("depart", "Goodbye! Have a nice day!"),
        ]
        closed = [event for event in events.events if event.kind == "policy.conversation_closed"]
        assert closed[-1].data["reason"] == "vision_depart"
        assert "policy.farewell" in events.kinds()

    asyncio.run(run())


def test_reception_policy_v4_suppresses_vision_policies_during_active_conversation() -> None:
    async def run() -> None:
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        calls = []

        async def speak_text(context, text, reason, event):
            calls.append((reason, text))
            return True

        registry.register("speak_text", speak_text)
        policy = ReceptionPolicy(
            ReceptionPolicySettings(
                cooldown_s=0.0,
                suppress_vision_policies_during_conversation=True,
            )
        )
        engine = PolicyEngine([policy], capabilities=registry, context=context)
        await engine.start()
        await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test"))
        await engine.handle_event(RuntimeEvent(kind="vision.approach", source="door_policy"))
        await engine.handle_event(RuntimeEvent(kind="vision.depart", source="door_policy"))

        assert policy.conversation_active is True
        assert calls == [("wave", "Hi! How can I help?")]
        assert "policy.greet_suppressed" in events.kinds()
        assert "policy.farewell_suppressed" in events.kinds()
        assert "policy.conversation_closed" not in events.kinds()
        assert "policy.farewell" not in events.kinds()

        await engine.handle_event(
            RuntimeEvent(
                kind="realtime.conversation.item.input_audio_transcription.completed",
                source="backend",
                data={"transcript": "okay goodbye"},
            )
        )
        assert policy.conversation_active is False
        closed = [event for event in events.events if event.kind == "policy.conversation_closed"]
        assert closed[-1].data["reason"] == "explicit_goodbye"

    asyncio.run(run())


def test_reception_policy_prepares_backend_session_before_opening_gate():
    async def run():
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        calls = []
        policy = ReceptionPolicy(ReceptionPolicySettings(cooldown_s=0.0))

        async def begin_conversation_session(context):
            calls.append(("begin", policy.should_forward_audio()))
            return {"conversation_generation": 1}

        async def speak_text(context, text, reason, event):
            calls.append(("speak", policy.should_forward_audio()))
            return True

        registry.register("begin_conversation_session", begin_conversation_session)
        registry.register("speak_text", speak_text)
        engine = PolicyEngine([policy], capabilities=registry, context=context)

        await engine.start()
        await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test"))

        assert calls == [("begin", False), ("speak", True)]
        kinds = events.kinds()
        assert kinds.index("capability.completed") < kinds.index("policy.conversation_opened")
        assert "policy.conversation_session_ready" in kinds

    asyncio.run(run())


def test_reception_policy_keeps_gate_closed_when_backend_session_reset_fails():
    async def run():
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        policy = ReceptionPolicy(ReceptionPolicySettings(cooldown_s=15.0))

        async def begin_conversation_session(context):
            raise RuntimeError("reconnect failed")

        registry.register("begin_conversation_session", begin_conversation_session)
        engine = PolicyEngine([policy], capabilities=registry, context=context)

        await engine.start()
        with pytest.raises(RuntimeError, match="reconnect failed"):
            await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test"))

        assert policy.should_forward_audio() is False
        assert "policy.conversation_opened" not in events.kinds()
        assert "capability.failed" in events.kinds()

    asyncio.run(run())


def test_live_app_registers_handler_conversation_session_capability():
    class Handler:
        def __init__(self):
            self.calls = 0

        async def begin_conversation_session(self):
            self.calls += 1
            return {"conversation_generation": self.calls}

    async def run():
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        handler = Handler()

        assert _register_handler_conversation_session(registry, handler) is True
        result = await registry.invoke("begin_conversation_session", context)

        assert result == {"conversation_generation": 1}
        assert handler.calls == 1
        assert events.kinds() == ["capability.started", "capability.completed"]
        assert _register_handler_conversation_session(CapabilityRegistry(), object()) is False

    asyncio.run(run())


def test_live_app_routes_all_fixed_policy_text_through_speech_capability():
    class Handler:
        def __init__(self):
            self.calls = []

        async def request_speech(self, text, *, metadata=None):
            self.calls.append((text, metadata))
            return True

    async def run():
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        handler = Handler()

        assert _register_handler_policy_speech(registry, handler) is True
        settings = ReceptionPolicySettings(
            cooldown_s=0.0,
            greeting="Configured greeting.",
            farewell="Configured farewell.",
            conversation_opener="Configured opener.",
        )
        for event in (
            RuntimeEvent(kind="vision.approach", source="test"),
            RuntimeEvent(kind="vision.depart", source="test"),
            RuntimeEvent(kind="vision.wave", source="test"),
        ):
            policy = ReceptionPolicy(settings)
            engine = PolicyEngine([policy], capabilities=registry, context=context)
            await engine.start()
            await engine.handle_event(event)
            await engine.stop()

        assert handler.calls == [
            (
                "Configured greeting.",
                {
                    "source": "reception_policy",
                    "reason": "approach",
                    "trigger_event": "vision.approach",
                },
            ),
            (
                "Configured farewell.",
                {
                    "source": "reception_policy",
                    "reason": "depart",
                    "trigger_event": "vision.depart",
                },
            ),
            (
                "Configured opener.",
                {
                    "source": "reception_policy",
                    "reason": "wave",
                    "trigger_event": "vision.wave",
                },
            ),
        ]
        assert _register_handler_policy_speech(CapabilityRegistry(), object()) is False

    asyncio.run(run())


def test_policy_visitor_boundary_reconnects_s2s_handler_before_second_opener():
    async def run():
        events = InMemoryEventSink()
        context = RuntimeContext(event_sink=events)
        registry = CapabilityRegistry()
        websockets = [_FakeWebSocket(), _FakeWebSocket()]

        async def connect_factory(url):
            websocket = websockets.pop(0)
            await websocket.incoming.put({"type": "session.created"})
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions="You are a clinic receptionist.",
            event_sink=events,
            startup_timeout_s=1.0,
            connect_factory=connect_factory,
        )
        await handler.start_up()
        first_websocket = handler._connection
        _register_handler_conversation_session(registry, handler)

        _register_handler_policy_speech(registry, handler)
        policy = ReceptionPolicy(ReceptionPolicySettings(cooldown_s=0.0))
        engine = PolicyEngine([policy], capabilities=registry, context=context)
        await engine.start()

        await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test"))
        assert policy.conversation_active is True
        await engine.handle_event(
            RuntimeEvent(
                kind="realtime.conversation.item.input_audio_transcription.completed",
                source="backend",
                data={"transcript": "goodbye"},
            )
        )
        assert policy.conversation_active is False

        await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test"))
        second_websocket = handler._connection
        assert policy.conversation_active is True
        await handler.shutdown()
        return first_websocket, second_websocket

    first_websocket, second_websocket = asyncio.run(run())

    assert first_websocket is not second_websocket
    assert first_websocket.closed is True
    assert second_websocket.closed is True
    assert [payload["type"] for payload in first_websocket.sent] == [
        "session.update",
        "response.cancel",
        "tts.create",
    ]
    assert [payload["type"] for payload in second_websocket.sent] == [
        "session.update",
        "response.cancel",
        "tts.create",
    ]
    assert first_websocket.sent[-1]["text"] == "Hi! How can I help?"
    assert second_websocket.sent[-1]["text"] == "Hi! How can I help?"


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


def test_scripted_playback_wav_uses_live_audio_sink_and_records_output(tmp_path):
    wav_path = tmp_path / "preflight.wav"
    audio = np.arange(320, dtype=np.int16)
    _write_pcm_wav(wav_path, 16_000, audio)
    recorder = ArtifactRecorder(tmp_path / "artifacts", run_id="scripted-playback", record_audio=True)

    async def run():
        events = InMemoryEventSink()
        sink = _CollectingAudioSink()
        await _run_scripted_playback_wav(
            wav_path=wav_path,
            audio_sink=sink,
            event_sink=events,
            recorder=recorder,
            run_id="scripted-playback",
            post_roll_s=0,
        )
        return events, sink

    events, sink = asyncio.run(run())
    recorder.close()

    assert len(sink.frames) == 1
    assert sink.frames[0][0] == 16_000
    assert np.array_equal(sink.frames[0][1], audio)
    assert sink.drained is True
    assert events.kinds() == [
        "assistant.audio.started",
        "audio.output_frame",
        "assistant.audio.done",
    ]
    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    streams = {entry["stream"]: entry for entry in manifest["artifacts"]["audio"]}
    assert streams["output"]["samples"] == 320


def test_scripted_policy_head_sway_moves_both_directions_and_returns_to_neutral(monkeypatch):
    monotonic_values = iter([0.0, 0.5, 1.5, 4.0])
    poses = []

    monkeypatch.setattr(live_app_module, "_set_head", lambda pose: poses.append(pose))

    asyncio.run(
        live_app_module._sway_head(
            update_interval_s=0.0,
            ramp_s=0.0,
            clock=lambda: next(monotonic_values),
        )
    )

    assert poses[0][2] == pytest.approx(live_app_module.HEAD_SWAY_YAW_DEG)
    assert poses[1][2] == pytest.approx(-live_app_module.HEAD_SWAY_YAW_DEG)
    assert poses[-1] == (0.0, 0.0, 0.0)


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


@pytest.mark.parametrize(
    ("advance_s", "refresh_activity", "expected_reason"),
    (
        (2.1, False, "idle_timeout"),
        (10.1, True, "max_duration"),
    ),
)
def test_policy_tick_loop_drives_reception_timeout(
    advance_s, refresh_activity, expected_reason
):
    async def run():
        clock = _Clock()
        recorded = InMemoryEventSink()
        policy_sink = _AsyncPolicyEventSink()
        event_sink = CompositeEventSink(recorded, policy_sink)
        context = RuntimeContext(event_sink=event_sink)
        policy = ReceptionPolicy(
            ReceptionPolicySettings(
                cooldown_s=0.0,
                conversation_idle_timeout_s=2.0,
                conversation_max_duration_s=10.0,
                clock=clock,
            )
        )
        engine = PolicyEngine([policy], context=context)
        policy_sink.bind(engine, asyncio.get_running_loop())

        await engine.start()
        await engine.handle_event(RuntimeEvent(kind="vision.wave", source="test"))
        assert policy.conversation_active is True
        clock.advance(advance_s)
        if refresh_activity:
            await engine.handle_event(
                RuntimeEvent(
                    kind="backend.transcript.final",
                    source="test",
                    data={"transcript": "Still talking"},
                )
            )

        stop_event = asyncio.Event()
        tick_task = asyncio.create_task(
            _run_policy_tick_loop(
                event_sink=event_sink,
                stop_event=stop_event,
                interval_s=0.001,
            )
        )
        try:
            async def wait_until_closed():
                while policy.conversation_active:
                    await asyncio.sleep(0)

            await asyncio.wait_for(wait_until_closed(), timeout=0.2)
        finally:
            stop_event.set()
            await tick_task

        await policy_sink.flush()
        await engine.stop()
        await policy_sink.drain()
        return recorded

    events = asyncio.run(run())

    assert "runtime.tick" in events.kinds()
    close = next(
        event for event in events.events if event.kind == "policy.conversation_closed"
    )
    assert close.data["reason"] == expected_reason


def test_perception_pipeline_accepts_injected_detector_and_writes_events(tmp_path):
    events_path = tmp_path / "vision-events.jsonl"
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    tracker = _FakeTracker([{"kind": "approach", "id": 1, "area": 0.2}])
    pipeline = PerceptionPipeline(
        detector=_FakeDetector([{"id": 1}]),
        tracker_factory=lambda frame_wh: tracker,
        events_path=events_path,
    )

    events, people, tracks = pipeline.process(frame, ts=123.456)

    assert events == [{"kind": "approach", "id": 1, "area": 0.2}]
    assert people == 1
    assert tracks == [{"id": 1, "area": 0.2}]
    assert tracker.timestamps == [123.456]
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


def test_perception_pipeline_gesture_only_path_does_not_initialize_person_detector():
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    pipeline = PerceptionPipeline(
        gestures=True,
        gesture_detector=_FakeGestureDetector(),
        gesture_only=True,
    )

    event = pipeline.process_gesture(frame, ts=123.456, frame_index=17)

    assert event == {"kind": "wave", "gesture": "Open_Palm", "score": 0.92}
    with pytest.raises(RuntimeError, match="gesture-only"):
        pipeline.process(frame)


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


def test_perception_pipeline_passes_source_timestamps_to_video_gesture_detector():
    diagnostics = InMemoryEventSink()
    detector = _FakeGestureDetector(running_mode="video")
    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    pipeline = PerceptionPipeline(
        detector=_FakeDetector([]),
        tracker_factory=lambda frame_wh: _FakeTracker(),
        gestures=True,
        gesture_detector=detector,
        gesture_running_mode="video",
        event_sink=diagnostics,
    )

    events, _, _ = pipeline.process(frame, ts=123.456, frame_index=17)

    assert events == [{"kind": "wave", "gesture": "Open_Palm", "score": 0.92}]
    assert detector.timestamps == [123.456]
    candidate = next(event for event in diagnostics.events if event.kind == "vision.gesture_candidate")
    assert candidate.data["running_mode"] == "video"
    assert candidate.data["source_frame_index"] == 17
    assert candidate.data["source_frame_ts"] == 123.456


def test_perception_pipeline_detects_temporal_hand_motion_wave():
    diagnostics = InMemoryEventSink()
    centers = iter([0.30, 0.30, 0.30, 0.50, 0.70, 0.50, 0.30, 0.50, 0.70])

    class FakeHandMotionDetector:
        gestures = ("Open_Palm",)
        threshold = 0.5
        running_mode = "image"
        model_path = "/tmp/fake-gesture.task"

        def observe(self, frame):
            return GestureFrameObservation(
                candidate=None,
                hand_center_x=next(centers),
                hand_center_y=0.4,
                hand_count=1,
            )

    frame = np.zeros((10, 20, 3), dtype=np.uint8)
    pipeline = PerceptionPipeline(
        detector=_FakeDetector([]),
        tracker_factory=lambda frame_wh: _FakeTracker(),
        gestures=True,
        gesture_detector=FakeHandMotionDetector(),
        wave_detection_mode="hand_motion",
        event_sink=diagnostics,
    )

    emitted = []
    for index in range(9):
        events, _, _ = pipeline.process(frame, ts=10.0 + index * 0.2, frame_index=index)
        emitted.extend(events)

    assert len(emitted) == 1
    assert emitted[0]["gesture"] == "Hand_Motion"
    assert emitted[0]["direction_changes"] >= 2
    candidates = [
        event for event in diagnostics.events if event.kind == "vision.hand_motion_candidate"
    ]
    assert candidates[-1].data["accepted"] is True
    assert candidates[-1].data["displacement"] >= 0.08


def test_gesture_detector_video_mode_uses_strictly_increasing_relative_timestamps(monkeypatch):
    calls: list[tuple[str, int | None]] = []
    created_options = []

    class FakeRecognizer:
        def recognize(self, image):
            calls.append(("image", None))
            return types.SimpleNamespace(
                gestures=[[types.SimpleNamespace(category_name="Open_Palm", score=0.9)]]
            )

        def recognize_for_video(self, image, timestamp_ms):
            calls.append(("video", timestamp_ms))
            return types.SimpleNamespace(
                gestures=[[types.SimpleNamespace(category_name="Open_Palm", score=0.9)]]
            )

    recognizer = FakeRecognizer()

    def classifier_options(**kwargs):
        return types.SimpleNamespace(**kwargs)

    def gesture_recognizer_options(**kwargs):
        options = types.SimpleNamespace(**kwargs)
        created_options.append(options)
        return options

    fake_mp = types.SimpleNamespace(
        Image=lambda **kwargs: kwargs,
        ImageFormat=types.SimpleNamespace(SRGB="SRGB"),
        tasks=types.SimpleNamespace(
            BaseOptions=lambda **kwargs: kwargs,
            components=types.SimpleNamespace(
                processors=types.SimpleNamespace(ClassifierOptions=classifier_options)
            ),
            vision=types.SimpleNamespace(
                RunningMode=types.SimpleNamespace(IMAGE="IMAGE", VIDEO="VIDEO"),
                GestureRecognizerOptions=gesture_recognizer_options,
                GestureRecognizer=types.SimpleNamespace(
                    create_from_options=lambda options: recognizer
                ),
            ),
        ),
    )
    monkeypatch.setitem(sys.modules, "mediapipe", fake_mp)
    monkeypatch.setattr(
        "reachy_mini_brain.official_runtime.perception._ensure_gesture_model",
        lambda: "/tmp/gesture.task",
    )
    detector = GestureDetector(running_mode="video")
    frame = np.zeros((10, 20, 3), dtype=np.uint8)

    detector.detect_candidate(frame, timestamp_s=1000.0)
    detector.detect_candidate(frame, timestamp_s=1000.0)
    detector.detect_candidate(frame, timestamp_s=1000.125)

    assert calls == [("video", 0), ("video", 1), ("video", 125)]
    classifier = created_options[0].canned_gesture_classifier_options
    assert classifier.score_threshold == 0.0
    assert classifier.category_allowlist == ["Open_Palm"]


def test_vision_replay_cli_help_loads_without_detector_dependencies():
    result = CliRunner().invoke(vision_replay_cli, ["--help"])

    assert result.exit_code == 0
    assert "Replay recorded video" in result.output
    assert "--visitor-trigger-profile" in result.output
    assert "--gesture-running-mode" in result.output
    assert "--wave-detection-mode" in result.output
    assert "--to-frame" in result.output


def test_official_runtime_live_cli_help_loads_without_robot_dependencies():
    result = CliRunner().invoke(live_app_cli, ["--help"])

    assert result.exit_code == 0
    assert "Run the ported official-runtime path" in result.output
    assert "s2s-local" in result.output
    assert "hf-official" not in result.output
    assert "--ready-cue" in result.output
    assert "--scripted-playback-wav" in result.output
    assert "--visitor-trigger-profile" in result.output
    assert "--vision-runtime" in result.output
    assert "--broker-capture-fps" in result.output
    assert "--gesture-running-mode" in result.output
    assert "--wave-detection-mode" in result.output


def test_backend_benchmark_cli_uses_native_s2s_backend():
    result = CliRunner().invoke(backend_benchmark_cli, ["--help"])

    assert result.exit_code == 0
    assert "s2s-local" in result.output
    assert "hf-official" not in result.output
    assert "--official-app-src" not in result.output


def test_live_cli_rejects_unknown_visitor_trigger_profile():
    result = CliRunner().invoke(live_app_cli, ["--visitor-trigger-profile", "latest"])

    assert result.exit_code == 2
    assert "Invalid value for '--visitor-trigger-profile'" in result.output


def test_live_app_loads_backend_instruction_provenance(tmp_path):
    instructions_file = tmp_path / "instructions.txt"
    instructions_file.write_text("You are a clinic receptionist.\n", encoding="utf-8")

    text, provenance = _load_backend_instructions(instructions_file=instructions_file, instructions=None)

    assert text == "You are a clinic receptionist.\n"
    assert provenance["instructions_source"] == str(instructions_file)
    assert provenance["instructions_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert provenance["instructions_chars"] == len(text)

    inline_text, inline_provenance = _load_backend_instructions(
        instructions_file=instructions_file,
        instructions="Inline clinic context.",
    )

    assert inline_text == "Inline clinic context."
    assert inline_provenance["instructions_source"] == "inline"
    assert inline_provenance["instructions_sha256"] == hashlib.sha256(inline_text.encode("utf-8")).hexdigest()

    profile_text, profile_provenance = _load_backend_instructions(
        instructions_file=instructions_file,
        instructions=None,
        profile_owned_context=True,
    )

    assert profile_text == ""
    assert profile_provenance["instructions_source"] == "hermes-profile"
    assert profile_provenance["instructions_sha256"] == hashlib.sha256(b"").hexdigest()
    assert profile_provenance["instructions_chars"] == 0


def test_reachy_audio_source_reads_fake_robot_audio_as_int16():
    observed = []

    async def run():
        sample = np.array([[0.5, -0.5], [0.25, -0.25]], dtype=np.float32)
        mini = _FakeMini(_FakeMedia(audio_samples=[sample]))
        source = ReachyAudioSource(
            mini,
            poll_interval_s=0.0,
            max_duration_s=1.0,
            on_frame=lambda: observed.append("audio"),
        )
        return await source.read()

    frame = asyncio.run(run())

    assert frame is not None
    sample_rate, audio = frame
    assert sample_rate == 16_000
    assert audio.dtype == np.int16
    assert audio.shape == (2,)
    assert observed == ["audio"]


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
    observed = []
    provider = ReachyCameraFrameProvider(
        _FakeMini(_FakeMedia(frame=frame)),
        on_frame=lambda: observed.append("video"),
    )

    got = provider.get_latest_frame()
    provider.set_head_tracking_enabled(True)

    assert np.array_equal(got, frame)
    assert provider.head_tracking_enabled is True
    assert observed == ["video"]


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
        self.timestamps = []

    @property
    def debug_state(self):
        return {"fake": True}

    def update(self, persons, *, ts=None):
        self.timestamps.append(ts)
        return list(self.events)


class _FakeGestureDetector:
    def __init__(
        self,
        result=("Open_Palm", 0.92),
        gestures=("Open_Palm",),
        threshold=0.5,
        running_mode="image",
    ):
        self.result = result
        self.gestures = gestures
        self.threshold = threshold
        self.running_mode = running_mode
        self.model_path = "/tmp/fake-gesture.task"
        self.timestamps = []

    def detect_candidate(self, frame, *, timestamp_s=None):
        self.timestamps.append(timestamp_s)
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


def test_stream_runtime_stop_skips_long_post_input_drain():
    async def run():
        events = InMemoryEventSink()
        handler = _QueuedOutputHandler([])
        sink = _CollectingAudioSink()
        runtime = OfficialStyleStreamRuntime(
            handler=handler,
            audio_source=_FiniteAudioSource([]),
            audio_sink=sink,
            event_sink=events,
            emit_timeout=0.01,
            drain_idle_polls=10_000,
        )

        task = asyncio.create_task(runtime.run())
        for _ in range(100):
            if "audio.input_done" in events.kinds():
                break
            await asyncio.sleep(0.01)
        assert "audio.input_done" in events.kinds()

        runtime.stop()
        await asyncio.wait_for(task, timeout=0.5)

        assert handler.stopped is True
        assert events.kinds()[-1] == "runtime.stopped"

    asyncio.run(run())


def test_stream_runtime_reports_input_done_before_long_output_drain():
    async def run():
        events = InMemoryEventSink()
        handler = _QueuedOutputHandler([])
        sink = _CollectingAudioSink()
        input_done = asyncio.Event()

        def on_input_done():
            input_done.set()

        runtime = OfficialStyleStreamRuntime(
            handler=handler,
            audio_source=_FiniteAudioSource([]),
            audio_sink=sink,
            event_sink=events,
            on_input_done=on_input_done,
            emit_timeout=0.01,
            drain_idle_polls=10_000,
        )

        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(input_done.wait(), timeout=0.5)

        assert task.done() is False
        assert "audio.input_done" in events.kinds()

        runtime.stop()
        await asyncio.wait_for(task, timeout=0.5)

    asyncio.run(run())


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


class _FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed and self.incoming.empty():
            raise StopAsyncIteration
        item = await self.incoming.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        return json.dumps(item)

    async def close(self):
        self.closed = True
        await self.incoming.put(StopAsyncIteration)


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


def test_s2s_realtime_handler_sends_session_and_audio_without_official_app():
    async def run():
        events = InMemoryEventSink()
        websocket = _FakeWebSocket()
        instructions = "You are a clinic receptionist."

        async def connect_factory(url):
            assert url == "ws://127.0.0.1:8765/v1/realtime"
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions=instructions,
            instructions_source="profiles/clinic_receptionist/instructions.txt",
            event_sink=events,
            voice="Sohee",
            startup_timeout_s=1.0,
            connect_factory=connect_factory,
        )
        await websocket.incoming.put({"type": "session.created", "session": {"id": "sess-1"}})
        await handler.start_up()
        await handler.receive((16_000, np.arange(160, dtype=np.int16)))
        await handler.shutdown()
        return events, websocket

    events, websocket = asyncio.run(run())

    assert websocket.closed is True
    assert websocket.sent[0]["type"] == "session.update"
    assert websocket.sent[0]["session"]["instructions"] == "You are a clinic receptionist."
    assert websocket.sent[0]["session"]["audio"]["output"]["voice"] == "Sohee"
    assert websocket.sent[1]["type"] == "input_audio_buffer.append"
    assert isinstance(websocket.sent[1]["audio"], str)
    snapshot = next(event for event in events.events if event.kind == "hf.session.snapshot").data
    assert snapshot["instructions_source"] == "profiles/clinic_receptionist/instructions.txt"
    assert snapshot["instructions_sha256"] == hashlib.sha256(b"You are a clinic receptionist.").hexdigest()
    assert snapshot["instructions_chars"] == len("You are a clinic receptionist.")
    assert "hf.realtime.session.created" in events.kinds()


def test_profile_owned_context_sends_empty_realtime_instructions_without_audio(tmp_path):
    async def run():
        events = InMemoryEventSink()
        websocket = _FakeWebSocket()
        legacy_instructions = tmp_path / "instructions.txt"
        legacy_instructions.write_text("Fictional Lakeside clinic facts.", encoding="utf-8")
        instructions, provenance = _load_backend_instructions(
            instructions_file=legacy_instructions,
            instructions=None,
            profile_owned_context=True,
        )

        async def connect_factory(url):
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions=instructions,
            instructions_source=provenance["instructions_source"],
            instructions_sha256=provenance["instructions_sha256"],
            event_sink=events,
            startup_timeout_s=1.0,
            connect_factory=connect_factory,
        )
        await websocket.incoming.put({"type": "session.created", "session": {"id": "sess-profile"}})
        await handler.start_up()
        await handler.shutdown()
        return events, websocket

    events, websocket = asyncio.run(run())

    assert websocket.sent[0]["type"] == "session.update"
    assert websocket.sent[0]["session"]["instructions"] == ""
    assert "Lakeside" not in json.dumps(websocket.sent[0])
    snapshot = next(event for event in events.events if event.kind == "hf.session.snapshot").data
    assert snapshot["instructions_source"] == "hermes-profile"
    assert snapshot["instructions_sha256"] == hashlib.sha256(b"").hexdigest()
    assert snapshot["instructions_chars"] == 0


def test_s2s_realtime_handler_emits_transcript_audio_and_text_requests():
    async def run():
        events = InMemoryEventSink()
        websocket = _FakeWebSocket()

        async def connect_factory(url):
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions="You are a clinic receptionist.",
            event_sink=events,
            startup_timeout_s=1.0,
            connect_factory=connect_factory,
        )
        await websocket.incoming.put({"type": "session.created"})
        await handler.start_up()
        audio = np.array([1, -2, 3, -4], dtype=np.int16)
        encoded_audio = __import__("base64").b64encode(audio.astype("<i2").tobytes()).decode("ascii")
        await websocket.incoming.put(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "item-1",
                "transcript": "I need directions.",
            }
        )
        await websocket.incoming.put(
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-1",
                "delta": encoded_audio,
            }
        )
        transcript = await asyncio.wait_for(handler.emit(), timeout=1.0)
        frame = await asyncio.wait_for(handler.emit(), timeout=1.0)
        ok = await handler.request_text_response("Welcome.")
        speech_ok = await handler.request_speech(
            "Goodbye! Have a nice day!",
            metadata={"source": "policy", "reason": "depart"},
        )
        await handler.shutdown()
        return events, websocket, transcript, frame, ok, speech_ok

    events, websocket, transcript, frame, ok, speech_ok = asyncio.run(run())

    assert ok is True
    assert speech_ok is True
    assert transcript["role"] == "user"
    assert transcript["transcript"] == "I need directions."
    assert frame[0] == 16_000
    assert np.array_equal(frame[1], np.array([1, -2, 3, -4], dtype=np.int16))
    assert frame[2]["response_id"] == "resp-1"
    assert websocket.sent[-4]["type"] == "conversation.item.create"
    assert websocket.sent[-3]["type"] == "response.create"
    assert websocket.sent[-2]["type"] == "response.cancel"
    assert websocket.sent[-1] == {
        "type": "tts.create",
        "text": "Goodbye! Have a nice day!",
        "metadata": {"source": "policy", "reason": "depart"},
    }
    assert "hf.realtime.conversation.item.input_audio_transcription.completed" in events.kinds()
    assert "hf.realtime.response.output_audio.delta" in events.kinds()
    assert "hf.response.metadata" in events.kinds()
    assert "hf.realtime.tts.requested" in events.kinds()


def test_s2s_realtime_handler_reconnects_between_visitor_conversations():
    async def run():
        events = InMemoryEventSink()
        websockets = [_FakeWebSocket(), _FakeWebSocket(), _FakeWebSocket()]
        settle_delays = []

        async def sleep_factory(delay):
            settle_delays.append(delay)

        async def connect_factory(url):
            websocket = websockets.pop(0)
            await websocket.incoming.put(
                {"type": "session.created", "session": {"id": f"sess-{3 - len(websockets)}"}}
            )
            return websocket

        first = None
        second = None
        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions="You are a clinic receptionist.",
            event_sink=events,
            startup_timeout_s=1.0,
            connect_factory=connect_factory,
            sleep_factory=sleep_factory,
        )
        await handler.start_up()
        startup_websocket = handler._connection
        await handler.request_text_response("Pre-visitor policy greeting.")
        first = await handler.begin_conversation_session()
        first_websocket = handler._connection
        await handler.request_text_response("First visitor opener.")

        second = await handler.begin_conversation_session()
        second_websocket = handler._connection
        await handler.request_text_response("Second visitor opener.")
        await handler.shutdown()
        return first, second, startup_websocket, first_websocket, second_websocket, events, settle_delays

    first, second, startup_websocket, first_websocket, second_websocket, events, settle_delays = asyncio.run(run())

    assert first == {
        "conversation_generation": 1,
        "connection_generation": 2,
        "reconnected": True,
    }
    assert second == {
        "conversation_generation": 2,
        "connection_generation": 3,
        "reconnected": True,
    }
    assert startup_websocket is not first_websocket
    assert first_websocket is not second_websocket
    assert startup_websocket.closed is True
    assert first_websocket.closed is True
    assert second_websocket.closed is True
    assert startup_websocket.sent[0]["type"] == "session.update"
    assert startup_websocket.sent[-1]["type"] == "response.create"
    assert first_websocket.sent[0]["type"] == "session.update"
    assert first_websocket.sent[-1]["type"] == "response.create"
    assert second_websocket.sent[0]["type"] == "session.update"
    assert second_websocket.sent[-1]["type"] == "response.create"
    starts = [event.data for event in events.events if event.kind == "hf.session.conversation_started"]
    assert [event["reconnected"] for event in starts] == [True, True]
    snapshots = [event.data for event in events.events if event.kind == "hf.session.snapshot"]
    assert [snapshot["connection_generation"] for snapshot in snapshots] == [1, 2, 3]
    assert settle_delays == [0.2, 0.2]


def test_s2s_realtime_handler_reuses_pristine_startup_session_for_first_visitor():
    async def run():
        events = InMemoryEventSink()
        websocket = _FakeWebSocket()

        async def connect_factory(url):
            await websocket.incoming.put({"type": "session.created", "session": {"id": "sess-1"}})
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions="You are a clinic receptionist.",
            event_sink=events,
            startup_timeout_s=1.0,
            connect_factory=connect_factory,
        )
        await handler.start_up()
        result = await handler.begin_conversation_session()
        await handler.shutdown()
        return result

    result = asyncio.run(run())

    assert result == {
        "conversation_generation": 1,
        "connection_generation": 1,
        "reconnected": False,
    }


def test_s2s_realtime_handler_retries_failed_conversation_reconnect():
    async def run():
        events = InMemoryEventSink()
        first_websocket = _FakeWebSocket()
        recovered_websocket = _FakeWebSocket()
        attempts = 0

        async def connect_factory(url):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("temporary connect failure")
            websocket = first_websocket if attempts == 1 else recovered_websocket
            await websocket.incoming.put({"type": "session.created"})
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions="You are a clinic receptionist.",
            event_sink=events,
            startup_timeout_s=1.0,
            connect_factory=connect_factory,
        )
        await handler.start_up()
        await handler.begin_conversation_session()
        await handler.request_text_response("First visitor opener.")

        with pytest.raises(OSError, match="temporary connect failure"):
            await handler.begin_conversation_session()
        recovered = await handler.begin_conversation_session()
        await handler.shutdown()
        return attempts, recovered

    attempts, recovered = asyncio.run(run())

    assert attempts == 3
    assert recovered == {
        "conversation_generation": 2,
        "connection_generation": 2,
        "reconnected": True,
    }


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
    recorder.runtime_summary("vision_broker", {"capture": {"published_frames": 10}})
    recorder.close()

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == "artifact-test"
    assert manifest["config"]["mode"] == "test"
    assert manifest["runtime_summaries"]["vision_broker"]["capture"]["published_frames"] == 10
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


def test_artifact_recorder_writes_video_timestamp_sidecar(monkeypatch, tmp_path):
    class FakeVideoWriter:
        def __init__(self, path, fourcc, fps, size):
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(b"fake-video")
            self.frames = []

        def write(self, frame):
            self.frames.append(frame)

        def release(self):
            return None

    fake_cv2 = types.SimpleNamespace(VideoWriter=FakeVideoWriter, VideoWriter_fourcc=lambda *args: 0)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    recorder = ArtifactRecorder(
        tmp_path,
        run_id="video-ts-test",
        config={"mode": "test"},
        record_video=True,
        capture_vision=True,
    )
    frame = np.zeros((2, 3, 3), dtype=np.uint8)

    recorder.vision_frame(
        frame,
        people=1,
        tracks=[{"id": 1}],
        events=[{"kind": "wave"}],
        fps=5.0,
        ts=123.4567,
    )
    recorder.close()

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    video_entry = manifest["artifacts"]["video"][0]
    metadata_path = Path(video_entry["metadata"])
    capture_path = tmp_path / "capture" / "capture-video-ts-test-01.jsonl"

    assert video_entry["status"] == "closed"
    assert video_entry["frames"] == 1
    assert metadata_path.exists()
    assert metadata_path.name == "video-video-ts-test-01.jsonl"

    video_rows = [json.loads(line) for line in metadata_path.read_text(encoding="utf-8").splitlines()]
    capture_rows = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines()]
    assert video_rows == [
        {
            "fps": 5.0,
            "frame_index": 0,
            "run_id": "video-ts-test",
            "ts": 123.457,
            "type": "frame",
        }
    ]
    assert capture_rows[0]["ts"] == 123.457
    assert capture_rows[0]["events"] == [{"kind": "wave"}]


def test_artifact_recorder_links_broker_video_and_capture_to_source_frame(monkeypatch, tmp_path):
    class FakeVideoWriter:
        def __init__(self, path, fourcc, fps, size):
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(b"fake-video")

        def write(self, frame):
            return None

        def release(self):
            return None

    monkeypatch.setitem(
        sys.modules,
        "cv2",
        types.SimpleNamespace(VideoWriter=FakeVideoWriter, VideoWriter_fourcc=lambda *args: 0),
    )
    recorder = ArtifactRecorder(
        tmp_path,
        run_id="broker-source-test",
        record_video=True,
        capture_vision=True,
    )
    frame = np.zeros((2, 3, 3), dtype=np.uint8)

    recorder.record_video_frame(frame, fps=15.0, ts=200.25, source_frame_id=37)
    recorder.capture_vision_frame(
        people=1,
        tracks=[{"id": 1}],
        events=[],
        ts=200.25,
        source_frame_id=37,
    )
    recorder.close()

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    video_path = Path(manifest["artifacts"]["video"][0]["metadata"])
    capture_path = tmp_path / "capture" / "capture-broker-source-test-01.jsonl"
    video_row = json.loads(video_path.read_text(encoding="utf-8").splitlines()[0])
    capture_row = json.loads(capture_path.read_text(encoding="utf-8").splitlines()[0])
    assert video_row["source_frame_id"] == 37
    assert capture_row["source_frame_id"] == 37
    assert video_row["ts"] == capture_row["ts"] == 200.25


def test_async_policy_event_sink_serializes_events_emitted_from_worker_thread():
    async def run():
        handled = []

        class FakeEngine:
            async def handle_event(self, event):
                await asyncio.sleep(0)
                handled.append(event.data["sequence"])

        sink = _AsyncPolicyEventSink()
        sink.bind(FakeEngine(), asyncio.get_running_loop())

        import threading

        thread = threading.Thread(
            target=lambda: [
                sink.emit(
                    RuntimeEvent(
                        kind="vision.test",
                        source="worker",
                        data={"sequence": index},
                    )
                )
                for index in range(5)
            ]
        )
        thread.start()
        thread.join()
        await sink.flush()
        await sink.drain()

        assert handled == [0, 1, 2, 3, 4]
        assert sink.errors == []
        assert sink.snapshot() == {
            "submitted_events": 5,
            "handled_events": 5,
            "dropped_events": 0,
            "queue_capacity": 256,
            "queue_depth": 0,
            "errors": [],
            "closed": True,
        }

    asyncio.run(run())


def test_broker_vision_loop_records_canonical_source_provenance(tmp_path):
    async def run():
        class FakeCamera:
            def get_latest_frame(self):
                return np.zeros((4, 6, 3), dtype=np.uint8)

        recorder = ArtifactRecorder(
            tmp_path,
            run_id="broker-loop-test",
            capture_vision=True,
        )
        stop_event = asyncio.Event()
        ready_event = asyncio.Event()
        task = asyncio.create_task(
            _broker_vision_loop(
                camera_provider=FakeCamera(),
                policy_event_sink=InMemoryEventSink(),
                recorder=recorder,
                diagnostic_sink=InMemoryEventSink(),
                stop_event=stop_event,
                ready_event=ready_event,
                capture_fps=30.0,
                recorder_queue_size=4,
                gesture_queue_size=4,
                policy_idle_s=0.0,
                perception_enabled=False,
                threshold=0.5,
                smooth=0,
                gestures=False,
                gesture_running_mode="image",
                wave_detection_mode="open_palm",
                visitor_trigger_profile="legacy",
                run_id="broker-loop-test",
            )
        )
        await asyncio.wait_for(ready_event.wait(), timeout=1.0)
        await asyncio.sleep(0.08)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)
        recorder.close()

    asyncio.run(run())

    capture_path = tmp_path / "capture" / "capture-broker-loop-test-01.jsonl"
    rows = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines()]
    assert rows
    assert [row["source_frame_id"] for row in rows] == sorted(
        row["source_frame_id"] for row in rows
    )
    manifest_path = tmp_path / "runs" / "run-broker-loop-test.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = manifest["runtime_summaries"]["vision_broker"]
    assert summary["capture"]["published_frames"] >= len(rows)
    assert summary["consumers"]["policy"]["completed_frames"] == len(rows)


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


def test_artifact_recorder_promotes_hf_session_snapshot_events(tmp_path):
    recorder = ArtifactRecorder(tmp_path, run_id="hf-session")

    recorder.emit(
        RuntimeEvent(
            kind="hf.session.snapshot",
            source="official_runtime.s2s_realtime",
            data={
                "backend_provider": "s2s-local",
                "instructions_source": "profiles/clinic_receptionist/instructions.txt",
                "instructions_sha256": "abc123",
            },
            ts=123.0,
        )
    )
    recorder.close()

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    assert manifest["session"]["backend_provider"] == "s2s-local"
    assert manifest["session"]["instructions_source"] == "profiles/clinic_receptionist/instructions.txt"
    assert manifest["session"]["instructions_sha256"] == "abc123"

    realtime_path = tmp_path / "realtime" / "realtime-hf-session-01.jsonl"
    realtime_rows = [json.loads(line) for line in realtime_path.read_text(encoding="utf-8").splitlines()]
    assert any(row["type"] == "session.snapshot" for row in realtime_rows)


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


def test_project_root_honors_release_repo_environment(tmp_path):
    env = {**os.environ, "REACHY_REPO": str(tmp_path)}

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from reachy_mini_brain.official_runtime.env import PROJECT_ROOT; "
                "print(PROJECT_ROOT)"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert Path(completed.stdout.strip()) == tmp_path.resolve()


@pytest.mark.optional_livekit
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


@pytest.mark.optional_livekit
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
