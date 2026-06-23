"""Live robot runner for the isolated official-runtime path."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from .artifacts import ArtifactRecorder
from .camera import register_camera_capabilities
from .capabilities import CapabilityRegistry, RuntimeContext
from .conversation_cues import ConversationCuePolicy
from .env import PROJECT_ROOT, load_project_env
from .events import CompositeEventSink, EventSink, RuntimeEvent
from .hf_official import DEFAULT_OFFICIAL_APP_SRC, build_hf_official_handler
from .livekit_handler import LiveKitBackendConfig, LiveKitRealtimeHandler
from .livekit_room_bridge import LiveKitRoomBridge
from .moves import AntennaCueController, PlaybackMovementGate
from .perception import PerceptionPipeline
from .policies import PolicyEngine
from .policy_audio_cache import PolicyAudioCache, load_policy_audio_frame
from .reception import ReceptionPolicy, ReceptionPolicySettings
from .robot_io import ReachyAudioSink, ReachyAudioSource, ReachyCameraFrameProvider, ReachyRobotSession
from .stream_runtime import CompositeRuntimeObserver, OfficialStyleStreamRuntime


load_project_env()

DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "official-runtime-live"
DEFAULT_PROFILE_INSTRUCTIONS = PROJECT_ROOT / "profiles" / "clinic_receptionist" / "instructions.txt"
DEFAULT_POLICY_AUDIO_CACHE_DIR = PROJECT_ROOT / "artifacts" / "policy-audio-cache" / "sohee"


@click.command()
@click.option("--backend", type=click.Choice(["hf-official", "livekit"]), default="hf-official", show_default=True)
@click.option("--run-id", default=None, help="Run id. Defaults to timestamped id.")
@click.option("--artifact-root", type=click.Path(path_type=Path), default=DEFAULT_ARTIFACT_ROOT)
@click.option("--duration", type=float, default=120.0, show_default=True, help="Maximum live run duration in seconds.")
@click.option("--robot-host", envvar="REACHY_HOST", default=None, help="Robot host/IP. Also sets REACHY_HOST.")
@click.option("--warmup-audio/--no-warmup-audio", default=True, show_default=True)
@click.option("--warmup-video/--no-warmup-video", default=False, show_default=True)
@click.option("--record-audio/--no-record-audio", default=True, show_default=True)
@click.option("--record-video/--no-record-video", default=False, show_default=True)
@click.option("--capture-vision/--no-capture-vision", default=False, show_default=True)
@click.option("--perception/--no-perception", default=False, show_default=True)
@click.option("--gestures/--no-gestures", default=False, show_default=True)
@click.option("--audio-gate/--no-audio-gate", default=True, show_default=True)
@click.option("--ready-cue/--no-ready-cue", default=False, show_default=True, help="Pulse antennas when backend is ready and mic input starts.")
@click.option("--ready-cue-hold", type=float, default=0.45, show_default=True, help="Seconds to hold the ready antenna cue.")
@click.option("--conversation-cues/--no-conversation-cues", default=False, show_default=True, help="Show antenna-only thinking cues between user turns and assistant audio.")
@click.option("--conversation-cue-high-s", type=float, default=0.22, show_default=True, help="Thinking cue high-position hold seconds.")
@click.option("--conversation-cue-rest-s", type=float, default=0.38, show_default=True, help="Thinking cue rest-position hold seconds.")
@click.option("--perception-threshold", type=float, default=0.5, show_default=True)
@click.option("--perception-smooth", type=int, default=0, show_default=True)
@click.option("--vision-interval", type=float, default=0.2, show_default=True)
@click.option("--instructions-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=DEFAULT_PROFILE_INSTRUCTIONS)
@click.option("--instructions", default=None, help="Inline backend instructions. Overrides --instructions-file.")
@click.option("--hf-voice", default="Sohee", show_default=True)
@click.option("--hf-connection-mode", type=click.Choice(["local", "deployed"]), default="local", show_default=True)
@click.option("--hf-realtime-ws-url", envvar="HF_REALTIME_WS_URL", default="ws://100.127.86.67:8765/v1/realtime")
@click.option("--hf-token", envvar="HF_TOKEN", default=None)
@click.option("--official-app-src", type=click.Path(path_type=Path), default=DEFAULT_OFFICIAL_APP_SRC)
@click.option(
    "--policy-audio-cache-dir",
    envvar="POLICY_AUDIO_CACHE_DIR",
    type=click.Path(path_type=Path),
    default=DEFAULT_POLICY_AUDIO_CACHE_DIR,
    show_default=True,
    help="Directory of cached WAVs for fixed reception policy speech.",
)
@click.option("--livekit-url", envvar="LIVEKIT_URL", default="")
@click.option("--livekit-api-key", envvar="LIVEKIT_API_KEY", default="")
@click.option("--livekit-api-secret", envvar="LIVEKIT_API_SECRET", default="")
@click.option("--livekit-token", envvar="LIVEKIT_TOKEN", default="")
@click.option("--livekit-room", envvar="LIVEKIT_ROOM", default="reachy-mini-live")
@click.option("--livekit-agent-name", envvar="LIVEKIT_AGENT_NAME", default="reachy-mini-receptionist")
@click.option("--livekit-dispatch-agent/--no-livekit-dispatch-agent", default=True, show_default=True)
@click.option(
    "--scripted-policy-flow",
    type=click.Choice(["none", "goodbye-greet"]),
    default="none",
    show_default=True,
    help="Inject a deterministic policy flow after runtime startup.",
)
@click.option(
    "--scripted-policy-gap-s",
    type=float,
    default=0.25,
    show_default=True,
    help="Delay between scripted policy steps after the prior audio finishes.",
)
@click.option(
    "--scripted-policy-timeout-s",
    type=float,
    default=30.0,
    show_default=True,
    help="Maximum seconds to wait for each scripted policy audio response.",
)
def cli(**kwargs: Any) -> None:
    """Run the ported official-runtime path on a live Reachy Mini."""

    run_id = kwargs["run_id"] or f"official-live-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        asyncio.run(_run_live(run_id=run_id, **{k: v for k, v in kwargs.items() if k != "run_id"}))
    except KeyboardInterrupt:
        raise click.ClickException("Interrupted")


async def _run_live(
    *,
    backend: str,
    run_id: str,
    artifact_root: Path,
    duration: float,
    robot_host: str | None,
    warmup_audio: bool,
    warmup_video: bool,
    record_audio: bool,
    record_video: bool,
    capture_vision: bool,
    perception: bool,
    gestures: bool,
    audio_gate: bool,
    ready_cue: bool,
    ready_cue_hold: float,
    conversation_cues: bool,
    conversation_cue_high_s: float,
    conversation_cue_rest_s: float,
    perception_threshold: float,
    perception_smooth: int,
    vision_interval: float,
    instructions_file: Path,
    instructions: str | None,
    hf_voice: str,
    hf_connection_mode: str,
    hf_realtime_ws_url: str,
    hf_token: str | None,
    official_app_src: Path,
    policy_audio_cache_dir: Path,
    livekit_url: str,
    livekit_api_key: str,
    livekit_api_secret: str,
    livekit_token: str,
    livekit_room: str,
    livekit_agent_name: str,
    livekit_dispatch_agent: bool,
    scripted_policy_flow: str,
    scripted_policy_gap_s: float,
    scripted_policy_timeout_s: float,
) -> None:
    backend_instructions = instructions if instructions is not None else instructions_file.read_text(encoding="utf-8")
    recorder = ArtifactRecorder(
        artifact_root,
        run_id=run_id,
        config={
            "backend": backend,
            "duration": duration,
            "robot_host": robot_host,
            "warmup_audio": warmup_audio,
            "warmup_video": warmup_video,
            "record_audio": record_audio,
            "record_video": record_video,
            "capture_vision": capture_vision,
            "perception": perception,
            "gestures": gestures,
            "audio_gate": audio_gate,
            "ready_cue": ready_cue,
            "ready_cue_hold": ready_cue_hold,
            "conversation_cues": conversation_cues,
            "conversation_cue_high_s": conversation_cue_high_s,
            "conversation_cue_rest_s": conversation_cue_rest_s,
            "hf_voice": hf_voice,
            "hf_connection_mode": hf_connection_mode,
            "hf_realtime_ws_url_set": bool(hf_realtime_ws_url),
            "official_app_src": str(official_app_src),
            "policy_audio_cache_dir": str(policy_audio_cache_dir),
            "scripted_policy_flow": scripted_policy_flow,
            "scripted_policy_gap_s": scripted_policy_gap_s,
            "scripted_policy_timeout_s": scripted_policy_timeout_s,
        },
        record_audio=record_audio,
        record_video=record_video,
        capture_vision=capture_vision,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop_event)

    robot_session = ReachyRobotSession(
        host=robot_host,
        warmup_audio=warmup_audio,
        warmup_video=warmup_video or perception or record_video,
        milestone_callback=lambda name, data: _record_milestone(recorder, run_id, name, **data),
    )
    mini = await asyncio.to_thread(robot_session.start)
    camera_provider = ReachyCameraFrameProvider(mini)

    movement_gate = PlaybackMovementGate(on_change=lambda active, reason: recorder.realtime("movement_gate", active=active, reason=reason))
    reception_policy = ReceptionPolicy(ReceptionPolicySettings(audio_gate_until_wave=audio_gate))
    _record_milestone(
        recorder,
        run_id,
        "audio_gate_initial_state",
        open=not audio_gate,
        reason="disabled" if not audio_gate else "waiting_for_wave",
    )
    policy_sink = _AsyncPolicyEventSink()
    event_waiter = _RuntimeEventWaiter()
    event_waiter.bind(loop)
    console_sink = _ConsoleMilestoneSink(run_id)
    event_sink = CompositeEventSink(recorder, movement_gate, policy_sink, console_sink, event_waiter)
    vision_diagnostic_sink = CompositeEventSink(recorder, console_sink)
    context = RuntimeContext(
        event_sink=event_sink,
        state={
            "camera_worker": camera_provider,
            "movement_manager": None,
        },
    )
    capabilities = CapabilityRegistry()
    register_camera_capabilities(capabilities)
    handler_holder: dict[str, Any] = {}
    antenna_pulse_tasks: set[asyncio.Task[None]] = set()

    async def antenna_pulse(context: RuntimeContext) -> bool:
        task = await _trigger_antenna_cue(event_sink=event_sink, hold_s=0.35, cue="policy_pulse")
        antenna_pulse_tasks.add(task)
        task.add_done_callback(antenna_pulse_tasks.discard)
        return True

    capabilities.register("antenna_pulse", antenna_pulse)

    async def set_antennas_async(antennas: tuple[float, float]) -> None:
        await asyncio.to_thread(_set_antennas, antennas)

    conversation_cue_controller = AntennaCueController(
        set_antennas=set_antennas_async,
        event_sink=event_sink,
        high_s=conversation_cue_high_s,
        rest_s=conversation_cue_rest_s,
    )

    async def start_thinking_cue(context: RuntimeContext, reason: str = "") -> bool:
        return await conversation_cue_controller.start(cue="thinking")

    async def stop_thinking_cue(context: RuntimeContext, reason: str = "") -> bool:
        return await conversation_cue_controller.stop(reason=reason or "stop")

    capabilities.register("start_thinking_cue", start_thinking_cue)
    capabilities.register("stop_thinking_cue", stop_thinking_cue)

    async def speak_text(context: RuntimeContext, text: str, reason: str, event: RuntimeEvent) -> bool:
        handler = handler_holder.get("handler")
        request_text_response = getattr(handler, "request_text_response", None)
        if not callable(request_text_response):
            return False
        return bool(await request_text_response(_policy_speech_prompt(text, reason)))

    capabilities.register("speak_text", speak_text)
    policies = [reception_policy]
    if conversation_cues:
        policies.append(ConversationCuePolicy())
    policy_engine = PolicyEngine(policies, capabilities=capabilities, context=context)
    policy_sink.bind(policy_engine, loop)

    runtime_observer = CompositeRuntimeObserver(reception_policy, recorder, movement_gate)
    audio_source = ReachyAudioSource(mini, max_duration_s=duration, stop_event=stop_event)
    audio_sink = ReachyAudioSink(mini)
    handler = _build_handler(
        backend=backend,
        event_sink=event_sink,
        instructions=backend_instructions,
        hf_voice=hf_voice,
        hf_connection_mode=hf_connection_mode,
        hf_realtime_ws_url=hf_realtime_ws_url,
        hf_token=hf_token,
        official_app_src=official_app_src,
        livekit_url=livekit_url,
        livekit_api_key=livekit_api_key,
        livekit_api_secret=livekit_api_secret,
        livekit_token=livekit_token,
        livekit_room=livekit_room,
        livekit_agent_name=livekit_agent_name,
        livekit_dispatch_agent=livekit_dispatch_agent,
        camera_worker=camera_provider,
        reachy_mini=mini,
    )
    handler_holder["handler"] = handler
    ready_cue_task: asyncio.Task[None] | None = None
    scripted_flow_task: asyncio.Task[None] | None = None

    async def on_runtime_ready() -> None:
        nonlocal ready_cue_task, scripted_flow_task
        _record_milestone(recorder, run_id, "software_pipeline_initialized")
        if ready_cue:
            ready_cue_task = await _trigger_ready_cue(event_sink=event_sink, hold_s=ready_cue_hold)
        if scripted_policy_flow != "none":
            scripted_flow_task = asyncio.create_task(
                _run_scripted_policy_flow(
                    flow=scripted_policy_flow,
                    policy_engine=policy_engine,
                    event_waiter=event_waiter,
                    stop_event=stop_event,
                    recorder=recorder,
                    run_id=run_id,
                    gap_s=scripted_policy_gap_s,
                    timeout_s=scripted_policy_timeout_s,
                ),
                name="official-runtime-scripted-policy-flow",
            )

    runtime = OfficialStyleStreamRuntime(
        handler=handler,
        audio_source=audio_source,
        audio_sink=audio_sink,
        event_sink=event_sink,
        runtime_observer=runtime_observer,
        on_ready=on_runtime_ready,
        emit_timeout=0.1,
        drain_idle_polls=200,
    )
    vision_task: asyncio.Task[None] | None = None
    vision_ready = asyncio.Event()

    try:
        await policy_engine.start()
        if perception or record_video or capture_vision:
            vision_task = asyncio.create_task(
                _vision_loop(
                    camera_provider=camera_provider,
                    policy_engine=policy_engine,
                    recorder=recorder,
                    diagnostic_sink=vision_diagnostic_sink,
                    stop_event=stop_event,
                    ready_event=vision_ready,
                    interval_s=vision_interval,
                    perception_enabled=perception,
                    threshold=perception_threshold,
                    smooth=perception_smooth,
                    gestures=gestures,
                ),
                name="official-runtime-vision",
            )
            ready_waiter = asyncio.create_task(vision_ready.wait(), name="official-runtime-vision-ready")
            done, pending = await asyncio.wait(
                {ready_waiter, vision_task},
                timeout=20.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for pending_task in pending:
                if pending_task is ready_waiter:
                    pending_task.cancel()
            if vision_task in done:
                await vision_task
            if ready_waiter not in done:
                raise RuntimeError("Timed out waiting for official-runtime vision startup.")
        await runtime.run()
        if scripted_flow_task is not None:
            await scripted_flow_task
    finally:
        stop_event.set()
        if scripted_flow_task is not None and not scripted_flow_task.done():
            scripted_flow_task.cancel()
            try:
                await scripted_flow_task
            except asyncio.CancelledError:
                pass
        if vision_task is not None:
            vision_task.cancel()
            try:
                await vision_task
            except asyncio.CancelledError:
                pass
        if ready_cue_task is not None and not ready_cue_task.done():
            ready_cue_task.cancel()
            try:
                await ready_cue_task
            except asyncio.CancelledError:
                pass
        for task in list(antenna_pulse_tasks):
            if task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await policy_engine.stop()
        await policy_sink.drain()
        await asyncio.to_thread(robot_session.stop)
        recorder.close()

    click.echo(f"official runtime live artifacts: {recorder.manifest_path}")


def _record_milestone(recorder: ArtifactRecorder, run_id: str, name: str, **data: Any) -> None:
    recorder.realtime("runtime.milestone", milestone=name, **data)
    click.echo(_format_milestone(run_id, name, data), err=True)


def _format_milestone(run_id: str, name: str, data: dict[str, Any]) -> str:
    details = " ".join(f"{key}={value!r}" for key, value in sorted(data.items()))
    suffix = f" {details}" if details else ""
    return f"official-runtime milestone {run_id}: {name}{suffix}"


class _ConsoleMilestoneSink:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._first_input_frame_seen = False
        self._first_forwarded_frame_seen = False
        self._first_output_frame_seen = False

    def emit(self, event: RuntimeEvent) -> None:
        name: str | None = None
        data: dict[str, Any] = {}

        if event.kind == "runtime.started":
            name = "runtime_started"
        elif event.kind == "runtime.handler_started":
            name = "backend_handler_started"
        elif event.kind == "runtime.input_starting":
            name = "input_loop_starting"
        elif event.kind == "audio.input_frame":
            forwarded = bool(event.data.get("forwarded"))
            if not self._first_input_frame_seen:
                self._first_input_frame_seen = True
                name = "first_mic_frame_captured"
                data = {"forwarded": forwarded}
                if forwarded:
                    self._first_forwarded_frame_seen = True
            elif forwarded and not self._first_forwarded_frame_seen:
                self._first_forwarded_frame_seen = True
                name = "first_mic_frame_forwarded_to_backend"
        elif event.kind == "audio.output_frame" and not self._first_output_frame_seen:
            self._first_output_frame_seen = True
            name = "first_backend_audio_pushed_to_robot"
            data = {
                "duration_s": event.data.get("duration_s"),
                "sample_rate": event.data.get("sample_rate"),
            }
        elif event.kind == "audio.input_done":
            name = "input_loop_done"
            data = {
                "duration_s": event.data.get("duration_s"),
                "forwarded_frames": event.data.get("forwarded_frames"),
                "frames": event.data.get("frames"),
            }
        elif event.kind == "runtime.stopped":
            name = "runtime_stopped"
        elif event.kind == "runtime.failed":
            name = "runtime_failed"
            data = {"error": event.data.get("error")}
        elif event.kind == "runtime.ready_cue":
            name = f"ready_cue_{event.data.get('phase', 'unknown')}"
            data = {"cue": event.data.get("cue"), "hold_s": event.data.get("hold_s")}
        elif event.kind == "runtime.antenna_cue":
            event_phase = event.data.get("event_phase")
            position_phase = event.data.get("phase")
            label = position_phase if event_phase == "position" else event_phase
            name = f"antenna_cue_{event.data.get('cue', 'unknown')}_{label or 'unknown'}"
            data = {
                "hold_s": event.data.get("hold_s"),
                "reason": event.data.get("reason"),
            }
        elif event.kind == "vision.gesture_detector_init_start":
            name = "gesture_detector_init_start"
            data = {
                "gestures": event.data.get("gestures"),
                "threshold": event.data.get("threshold"),
            }
        elif event.kind == "vision.gesture_detector_ready":
            name = "gesture_detector_ready"
            data = {
                "gestures": event.data.get("gestures"),
                "threshold": event.data.get("threshold"),
                "load_ms": event.data.get("load_ms"),
            }
        elif event.kind == "vision.gesture_detector_failed":
            name = "gesture_detector_failed"
            data = {
                "gestures": event.data.get("gestures"),
                "threshold": event.data.get("threshold"),
                "load_ms": event.data.get("load_ms"),
                "error": event.data.get("error"),
            }
        elif event.kind == "policy.conversation_opened":
            name = "audio_gate_opened"
            data = {"audio_gate_open": event.data.get("audio_gate_open"), "reason": "wave"}
        elif event.kind == "policy.conversation_closed":
            name = "audio_gate_closed"
            data = {"audio_gate_open": event.data.get("audio_gate_open"), "reason": event.data.get("reason")}

        if name is not None:
            click.echo(_format_milestone(self.run_id, name, data), err=True)


class _RuntimeEventWaiter:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._condition: asyncio.Condition | None = None
        self._events: list[RuntimeEvent] = []

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._condition = asyncio.Condition()

    def emit(self, event: RuntimeEvent) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._append, event)

    def marker(self) -> int:
        return len(self._events)

    async def wait_for(
        self,
        predicate: Any,
        *,
        after: int = 0,
        timeout_s: float = 30.0,
    ) -> RuntimeEvent:
        condition = self._condition
        if condition is None:
            raise RuntimeError("event waiter is not bound")
        deadline = self._loop.time() + max(0.0, timeout_s) if self._loop else None
        async with condition:
            while True:
                for event in self._events[after:]:
                    if predicate(event):
                        return event
                if deadline is None:
                    await condition.wait()
                    continue
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    raise TimeoutError(f"timed out waiting for runtime event after {timeout_s:.1f}s")
                await asyncio.wait_for(condition.wait(), timeout=remaining)

    def _append(self, event: RuntimeEvent) -> None:
        self._events.append(event)
        condition = self._condition
        if condition is None:
            return
        try:
            condition.notify_all()
        except RuntimeError:
            async def notify() -> None:
                async with condition:
                    condition.notify_all()

            asyncio.create_task(notify())


async def _run_scripted_policy_flow(
    *,
    flow: str,
    policy_engine: PolicyEngine,
    event_waiter: _RuntimeEventWaiter,
    stop_event: asyncio.Event,
    recorder: ArtifactRecorder,
    run_id: str,
    gap_s: float,
    timeout_s: float,
) -> None:
    _record_milestone(recorder, run_id, "scripted_policy_flow_started", flow=flow)
    try:
        if flow == "goodbye-greet":
            steps = [
                (
                    "depart",
                    RuntimeEvent(
                        kind="vision.depart",
                        source="official_runtime.scripted_policy_flow",
                        data={
                            "kind": "depart",
                            "id": "scripted-depart",
                            "area": 0.15,
                            "cx": 0.5,
                            "cy": 0.42,
                            "scripted": True,
                        },
                    ),
                ),
                (
                    "approach",
                    RuntimeEvent(
                        kind="vision.approach",
                        source="official_runtime.scripted_policy_flow",
                        data={
                            "kind": "approach",
                            "id": "scripted-approach",
                            "area": 0.12,
                            "cx": 0.5,
                            "cy": 0.42,
                            "scripted": True,
                        },
                    ),
                ),
            ]
        else:
            raise ValueError(f"unsupported scripted policy flow: {flow}")

        for index, (label, event) in enumerate(steps, start=1):
            marker = event_waiter.marker()
            _record_milestone(recorder, run_id, "scripted_policy_step_started", step=label, index=index)
            await policy_engine.handle_event(event)
            audio_event = await event_waiter.wait_for(
                lambda runtime_event: runtime_event.kind == "assistant.audio.done",
                after=marker,
                timeout_s=timeout_s,
            )
            _record_milestone(
                recorder,
                run_id,
                "scripted_policy_step_audio_done",
                step=label,
                index=index,
                reason=audio_event.data.get("reason"),
            )
            if index < len(steps):
                await asyncio.sleep(max(0.0, gap_s))
        _record_milestone(recorder, run_id, "scripted_policy_flow_completed", flow=flow)
    except Exception as exc:
        _record_milestone(recorder, run_id, "scripted_policy_flow_failed", flow=flow, error=repr(exc))
        raise
    finally:
        stop_event.set()


async def _trigger_ready_cue(
    *,
    event_sink: EventSink,
    hold_s: float,
    high: tuple[float, float] = (18.0, 18.0),
    rest: tuple[float, float] = (-15.0, -15.0),
) -> asyncio.Task[None]:
    """Start an antenna-only cue and return the task that resets it."""

    return await _trigger_antenna_cue(
        event_sink=event_sink,
        hold_s=hold_s,
        high=high,
        rest=rest,
        cue="ready",
        event_kind="runtime.ready_cue",
    )


async def _trigger_antenna_cue(
    *,
    event_sink: EventSink,
    hold_s: float,
    high: tuple[float, float] = (18.0, 18.0),
    rest: tuple[float, float] = (-15.0, -15.0),
    cue: str = "policy_pulse",
    event_kind: str = "runtime.antenna_cue",
) -> asyncio.Task[None]:
    """Start an antenna-only cue and return the task that resets it."""

    hold_s = max(0.0, float(hold_s))
    await asyncio.to_thread(_set_antennas, high)
    event_sink.emit(
        RuntimeEvent(
            kind=event_kind,
            source="official_runtime.live_app",
            data={"cue": cue, "phase": "high", "hold_s": hold_s, "antennas": high},
        )
    )

    async def reset() -> None:
        try:
            await asyncio.sleep(hold_s)
        finally:
            try:
                await asyncio.to_thread(_set_antennas, rest)
                event_sink.emit(
                    RuntimeEvent(
                        kind=event_kind,
                        source="official_runtime.live_app",
                        data={"cue": cue, "phase": "rest", "antennas": rest},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                event_sink.emit(
                    RuntimeEvent(
                        kind="runtime.ready_cue_failed",
                        source="official_runtime.live_app",
                        data={"error": repr(exc)},
                    )
                )

    return asyncio.create_task(reset(), name="official-runtime-ready-cue-reset")


def _set_antennas(antennas: tuple[float, float]) -> None:
    from reachy_mini_brain import robot

    robot.set_target(antennas=antennas)


async def _play_cached_policy_speech(
    *,
    cache: PolicyAudioCache,
    audio_sink: Any,
    event_sink: EventSink,
    recorder: ArtifactRecorder,
    text: str,
    reason: str,
    event: RuntimeEvent,
) -> bool:
    path = cache.resolve(text)
    if path is None:
        expected = cache.expected_path(text)
        event_sink.emit(
            RuntimeEvent(
                kind="policy.speech_cache_missing",
                source="official_runtime.policy_audio_cache",
                data={
                    "text": text,
                    "reason": reason,
                    "trigger_event": event.kind,
                    "expected_path": str(expected) if expected is not None else None,
                },
            )
        )
        return False

    try:
        frame = load_policy_audio_frame(path)
    except Exception as exc:  # noqa: BLE001
        event_sink.emit(
            RuntimeEvent(
                kind="policy.speech_cache_load_failed",
                source="official_runtime.policy_audio_cache",
                data={"text": text, "reason": reason, "path": str(path), "error": repr(exc)},
            )
        )
        return False

    sample_rate, audio = frame
    metadata = {
        "event_type": "policy_audio_cache",
        "policy_reason": reason,
        "policy_text": text,
        "path": str(path),
        "trigger_event": event.kind,
    }
    frame_data = _policy_audio_frame_data(sample_rate, audio)
    event_sink.emit(
        RuntimeEvent(
            kind="policy.speech_cache_hit",
            source="official_runtime.policy_audio_cache",
            data={"text": text, "reason": reason, "path": str(path), **frame_data},
        )
    )
    event_sink.emit(
        RuntimeEvent(
            kind="assistant.audio.started",
            source="official_runtime.policy_audio_cache",
            data={"metadata": metadata, **frame_data},
        )
    )
    try:
        recorder.record_output_audio_frame(sample_rate, audio, metadata=metadata)
        event_sink.emit(
            RuntimeEvent(
                kind="audio.output_frame",
                source="official_runtime.policy_audio_cache",
                data={"metadata": metadata, **frame_data},
            )
        )
        await audio_sink.write(frame)
        drain = getattr(audio_sink, "drain", None)
        if callable(drain):
            await drain()
    except Exception as exc:  # noqa: BLE001
        event_sink.emit(
            RuntimeEvent(
                kind="policy.speech_cache_playback_failed",
                source="official_runtime.policy_audio_cache",
                data={"text": text, "reason": reason, "path": str(path), "error": repr(exc)},
            )
        )
        return False
    finally:
        event_sink.emit(
            RuntimeEvent(
                kind="assistant.audio.done",
                source="official_runtime.policy_audio_cache",
                data={"reason": "policy_audio_cache", "policy_reason": reason, "text": text},
            )
        )

    event_sink.emit(
        RuntimeEvent(
            kind="policy.speech_cache_played",
            source="official_runtime.policy_audio_cache",
            data={"text": text, "reason": reason, "path": str(path), **frame_data},
        )
    )
    return True


def _policy_audio_frame_data(sample_rate: int, audio: Any) -> dict[str, Any]:
    samples = int(getattr(audio, "shape", [len(audio)])[0])
    duration_s = samples / float(sample_rate) if sample_rate else 0.0
    dtype = str(getattr(audio, "dtype", "unknown"))
    return {
        "sample_rate": int(sample_rate),
        "samples": samples,
        "duration_s": round(duration_s, 3),
        "dtype": dtype,
    }


def _build_handler(
    *,
    backend: str,
    event_sink: EventSink,
    instructions: str,
    hf_voice: str,
    hf_connection_mode: str,
    hf_realtime_ws_url: str,
    hf_token: str | None,
    official_app_src: Path,
    livekit_url: str,
    livekit_api_key: str,
    livekit_api_secret: str,
    livekit_token: str,
    livekit_room: str,
    livekit_agent_name: str,
    livekit_dispatch_agent: bool,
    camera_worker: Any | None,
    reachy_mini: Any | None,
) -> Any:
    if backend == "hf-official":
        return build_hf_official_handler(
            event_sink=event_sink,
            official_app_src=official_app_src,
            instructions=instructions,
            voice=hf_voice,
            connection_mode=hf_connection_mode,
            realtime_ws_url=hf_realtime_ws_url if hf_connection_mode == "local" else None,
            hf_token=hf_token,
            camera_worker=camera_worker,
            reachy_mini=reachy_mini,
        )
    if backend == "livekit":
        config = LiveKitBackendConfig(
            url=livekit_url,
            api_key=livekit_api_key,
            api_secret=livekit_api_secret,
            token=livekit_token,
            room_name=livekit_room,
            participant_name="reachy-mini-live",
            agent_name=livekit_agent_name,
            dispatch_agent=livekit_dispatch_agent,
        )
        bridge = LiveKitRoomBridge(config, event_sink=event_sink)
        return LiveKitRealtimeHandler(config=config, bridge=bridge, event_sink=event_sink)
    raise ValueError(f"unsupported backend: {backend}")


async def _vision_loop(
    *,
    camera_provider: ReachyCameraFrameProvider,
    policy_engine: PolicyEngine,
    recorder: ArtifactRecorder,
    diagnostic_sink: EventSink,
    stop_event: asyncio.Event,
    ready_event: asyncio.Event | None,
    interval_s: float,
    perception_enabled: bool,
    threshold: float,
    smooth: int,
    gestures: bool,
) -> None:
    pipeline = (
        PerceptionPipeline(threshold=threshold, smooth=smooth, gestures=gestures, event_sink=diagnostic_sink)
        if perception_enabled
        else None
    )
    if pipeline is not None and gestures:
        pipeline.ensure_gesture_detector()
    if ready_event is not None:
        ready_event.set()
    fps = 1.0 / interval_s if interval_s > 0 else 5.0
    while not stop_event.is_set():
        frame = camera_provider.get_latest_frame()
        if frame is not None:
            events: list[dict[str, Any]] = []
            people = 0
            tracks: list[dict[str, Any]] = []
            if pipeline is not None:
                events, people, tracks = pipeline.process(frame, bgr=True)
            recorder.vision_frame(frame, people=people, tracks=tracks, events=events, fps=fps)
            for event in events:
                await policy_engine.handle_event(
                    RuntimeEvent(kind=f"vision.{event['kind']}", source="official_runtime.vision", data=event)
                )
        await asyncio.sleep(max(0.01, interval_s))


class _AsyncPolicyEventSink:
    def __init__(self) -> None:
        self.engine: PolicyEngine | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.tasks: set[asyncio.Task[None]] = set()

    def bind(self, engine: PolicyEngine, loop: asyncio.AbstractEventLoop) -> None:
        self.engine = engine
        self.loop = loop

    def emit(self, event: RuntimeEvent) -> None:
        if self.engine is None or self.loop is None:
            return
        task = self.loop.create_task(self.engine.handle_event(event))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        if not self.tasks:
            return
        await asyncio.gather(*list(self.tasks), return_exceptions=True)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass


def _policy_speech_prompt(text: str, reason: str) -> str:
    return (
        f"Reception policy event: {reason}. "
        f"Say exactly this line aloud, without adding extra words: {text}"
    )


if __name__ == "__main__":
    cli()
