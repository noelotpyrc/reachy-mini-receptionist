"""Live robot runner for the isolated official-runtime path."""

from __future__ import annotations

import asyncio
import hashlib
import math
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import click

from .agent_profile import (
    AgentProfileError,
    ComposedAgentProfile,
    compose_agent_profile,
    with_session_date,
)
from .artifacts import ArtifactRecorder
from .camera import register_camera_capabilities
from .capabilities import CapabilityRegistry, RuntimeContext
from .conversation_cues import ConversationCuePolicy
from .door_observation import DoorObserverSettings
from .door_policy import DoorPolicySettings
from .door_policy_live import LiveDoorPolicyCoordinator
from .env import PROJECT_ROOT, load_project_env
from .events import CompositeEventSink, EventSink, RuntimeEvent
from .live_detection import FramePacket, LiveDetectionManager, load_pipeline_config
from .livekit_handler import LiveKitBackendConfig, LiveKitRealtimeHandler
from .livekit_room_bridge import LiveKitRoomBridge
from .live_rerun import RERUN_MODES, LiveRerunPublisher
from .liveness import HeartbeatWriter, RuntimeLiveness, pulse_event_loop
from .moves import AntennaCueController, PlaybackMovementGate
from .perception import (
    GESTURE_RUNNING_MODES,
    WAVE_DETECTION_MODES,
    PerceptionPipeline,
)
from .policies import PolicyEngine
from .policy_audio_cache import PolicyAudioCache, load_policy_audio_frame
from .reception import ReceptionPolicy, ReceptionPolicySettings
from .realtime_tools import ToolExecutionContext, ToolRegistry, build_reference_tool_registry
from .reception_tools import build_reception_tool_registry, with_reception_tool_instructions
from .robot_io import ReachyAudioSink, ReachyAudioSource, ReachyCameraFrameProvider, ReachyRobotSession
from .s2s_realtime import S2SRealtimeHandler
from .stream_runtime import CompositeRuntimeObserver, OfficialStyleStreamRuntime
from .visitor_trigger_profiles import (
    LEGACY_VISITOR_TRIGGER_PROFILE,
    VISITOR_TRIGGER_PROFILE_NAMES,
    resolve_visitor_trigger_profile,
)
from .vision_broker_runtime import BrokerVisionRuntime, VisionConsumerSpec


load_project_env()

DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "official-runtime-live"
DEFAULT_PROFILE_INSTRUCTIONS = PROJECT_ROOT / "profiles" / "clinic_receptionist" / "instructions.txt"
DEFAULT_AGENT_PROFILE_PUBLIC_DIR = PROJECT_ROOT / "profiles" / "clinic_receptionist"
DEFAULT_POLICY_AUDIO_CACHE_DIR = PROJECT_ROOT / "artifacts" / "policy-audio-cache" / "sohee"
DEFAULT_DOOR_POLICY_PIPELINES = PROJECT_ROOT / "config" / "vision" / "door-policy-v1.json"
VISION_RUNTIME_MODES = ("serial-v1", "broker-v1")
POLICY_TICK_INTERVAL_S = 1.0


def _load_backend_instructions(
    *,
    instructions_file: Path,
    instructions: str | None,
    profile_owned_context: bool = False,
) -> tuple[str, dict[str, Any]]:
    if profile_owned_context:
        text = ""
        source = "hermes-profile"
    elif instructions is not None:
        text = instructions
        source = "inline"
    else:
        text = instructions_file.read_text(encoding="utf-8")
        source = str(instructions_file)
    return text, _instruction_provenance(text, source=source)


def _instruction_provenance(instructions: str, *, source: str) -> dict[str, Any]:
    return {
        "instructions_source": source,
        "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        "instructions_chars": len(instructions),
    }


async def _run_policy_tick_loop(
    *,
    event_sink: EventSink,
    stop_event: asyncio.Event,
    interval_s: float = POLICY_TICK_INTERVAL_S,
) -> None:
    """Emit the clock events used by reception conversation timeouts."""

    if interval_s <= 0:
        raise ValueError("policy tick interval must be positive")

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except TimeoutError:
            event_sink.emit(
                RuntimeEvent(kind="runtime.tick", source="official_runtime.clock")
            )


@click.command()
@click.option("--backend", type=click.Choice(["s2s-local", "livekit"]), default="s2s-local", show_default=True)
@click.option("--run-id", default=None, help="Run id. Defaults to timestamped id.")
@click.option("--artifact-root", type=click.Path(path_type=Path), default=DEFAULT_ARTIFACT_ROOT)
@click.option("--duration", type=float, default=120.0, show_default=True, help="Maximum live run duration in seconds.")
@click.option(
    "--heartbeat-path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Optional source-liveness heartbeat file for the detached supervisor.",
)
@click.option("--heartbeat-interval-s", type=float, default=1.0, show_default=True)
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
@click.option(
    "--visitor-trigger-profile",
    envvar="RECEPTION_VISITOR_TRIGGER_PROFILE",
    type=click.Choice(VISITOR_TRIGGER_PROFILE_NAMES),
    default=LEGACY_VISITOR_TRIGGER_PROFILE,
    show_default=True,
    help="Versioned greet/goodbye trigger implementation.",
)
@click.option("--vision-interval", type=float, default=0.2, show_default=True)
@click.option(
    "--vision-runtime",
    envvar="RECEPTION_VISION_RUNTIME",
    type=click.Choice(VISION_RUNTIME_MODES),
    default="serial-v1",
    show_default=True,
)
@click.option("--broker-capture-fps", type=click.FloatRange(min=0.1), default=15.0, show_default=True)
@click.option(
    "--broker-recorder-queue-size",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--broker-gesture-queue-size",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--broker-policy-idle-s",
    type=click.FloatRange(min=0.0),
    default=0.1,
    show_default=True,
)
@click.option(
    "--gesture-running-mode",
    type=click.Choice(GESTURE_RUNNING_MODES),
    default="image",
    show_default=True,
)
@click.option(
    "--wave-detection-mode",
    type=click.Choice(WAVE_DETECTION_MODES),
    default="open_palm",
    show_default=True,
)
@click.option(
    "--vision-pipelines-config",
    envvar="RECEPTION_VISION_PIPELINES_CONFIG",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Versioned JSON configuration for additional live detector/tracker pipelines.",
)
@click.option(
    "--rerun-mode",
    envvar="RECEPTION_RERUN_MODE",
    type=click.Choice(RERUN_MODES),
    default="off",
    show_default=True,
)
@click.option(
    "--rerun-grpc-url",
    envvar="RECEPTION_RERUN_GRPC_URL",
    default="rerun+http://127.0.0.1:9876/proxy",
    show_default=True,
)
@click.option("--rerun-image-fps", type=float, default=5.0, show_default=True)
@click.option("--rerun-jpeg-quality", type=click.IntRange(1, 100), default=80, show_default=True)
@click.option("--rerun-queue-size", type=click.IntRange(1), default=3, show_default=True)
@click.option("--instructions-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=DEFAULT_PROFILE_INSTRUCTIONS)
@click.option("--instructions", default=None, help="Inline backend instructions. Overrides --instructions-file.")
@click.option(
    "--profile-owned-context",
    is_flag=True,
    default=False,
    help="Send no application profile prompt because the upstream Hermes profile owns receptionist context.",
)
@click.option(
    "--agent-profile-id",
    envvar="RECEPTION_AGENT_PROFILE_ID",
    default="",
    help="Enable the client-owned profile for this profile ID.",
)
@click.option(
    "--agent-tools",
    envvar="RECEPTION_AGENT_TOOLS",
    type=click.Choice(["none", "time-web", "reference-test"]),
    default="time-web",
    show_default=True,
    help="Client profile tools; no tools without a profile. reference-test is test-only.",
)
@click.option(
    "--agent-profile-format",
    envvar="RECEPTION_AGENT_PROFILE_FORMAT",
    type=click.Choice(["overlay", "hermes"]),
    default="overlay",
    help="hermes requires original private HERMES.md/personality.md sources; no public facts fallback.",
)
@click.option(
    "--agent-profile-public-dir",
    envvar="RECEPTION_AGENT_PROFILE_PUBLIC_DIR",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=DEFAULT_AGENT_PROFILE_PUBLIC_DIR,
    show_default=True,
)
@click.option(
    "--agent-profile-private-dir",
    envvar="RECEPTION_AGENT_PROFILE_PRIVATE_DIR",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Optional private profile overlay outside Git.",
)
@click.option("--hf-voice", default="Sohee", show_default=True)
@click.option("--hf-realtime-ws-url", envvar="HF_REALTIME_WS_URL", default="ws://100.127.86.67:8765/v1/realtime")
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
    type=click.Choice(["none", "goodbye", "greet", "goodbye-greet"]),
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
@click.option(
    "--scripted-policy-greeting",
    default=None,
    help="Override the deterministic greeting text for scripted policy preflights.",
)
@click.option(
    "--scripted-playback-wav",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Play one fixed WAV through the live app audio sink, then exit.",
)
@click.option(
    "--scripted-playback-post-roll-s",
    type=float,
    default=0.5,
    show_default=True,
    help="Seconds to keep the live app open after scripted WAV playback.",
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
    heartbeat_path: Path | None,
    heartbeat_interval_s: float,
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
    visitor_trigger_profile: str,
    vision_interval: float,
    vision_runtime: str,
    broker_capture_fps: float,
    broker_recorder_queue_size: int,
    broker_gesture_queue_size: int,
    broker_policy_idle_s: float,
    gesture_running_mode: str,
    wave_detection_mode: str,
    vision_pipelines_config: Path | None,
    rerun_mode: str,
    rerun_grpc_url: str,
    rerun_image_fps: float,
    rerun_jpeg_quality: int,
    rerun_queue_size: int,
    instructions_file: Path,
    instructions: str | None,
    profile_owned_context: bool,
    agent_profile_id: str,
    agent_tools: str,
    agent_profile_format: str,
    agent_profile_public_dir: Path,
    agent_profile_private_dir: Path | None,
    hf_voice: str,
    hf_realtime_ws_url: str,
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
    scripted_policy_greeting: str | None,
    scripted_playback_wav: Path | None,
    scripted_playback_post_roll_s: float,
) -> None:
    resolved_visitor_profile = resolve_visitor_trigger_profile(visitor_trigger_profile)
    door_policy_enabled = resolved_visitor_profile.implementation.startswith("door_policy_")
    if door_policy_enabled and not perception:
        raise click.ClickException("door-v1 requires person perception")
    resolved_pipeline_path = (
        vision_pipelines_config
        if vision_pipelines_config is not None
        else DEFAULT_DOOR_POLICY_PIPELINES
        if door_policy_enabled
        else None
    )
    resolved_pipeline_config = (
        load_pipeline_config(resolved_pipeline_path)
        if resolved_pipeline_path is not None
        else None
    )
    if door_policy_enabled:
        policy_pipelines = [
            item for item in resolved_pipeline_config.pipelines if item.role == "policy"
        ]
        if len(policy_pipelines) != 1 or policy_pipelines[0].detector != "grounding-dino":
            raise click.ClickException(
                "door-v1 requires exactly one policy-role Grounding DINO pipeline"
            )
    rerun_save_path = (
        artifact_root / "rerun" / f"review-{run_id}-01.rrd"
        if "file" in rerun_mode
        else None
    )
    if rerun_save_path is not None and rerun_save_path.exists():
        raise click.ClickException(f"refusing to overwrite existing Rerun artifact: {rerun_save_path}")
    agent_profile: ComposedAgentProfile | None = None
    tool_registry: ToolRegistry | None = None
    if (
        not agent_profile_id
        and click.get_current_context().get_parameter_source("agent_tools")
        == click.core.ParameterSource.DEFAULT
    ):
        agent_tools = "none"
    if agent_tools != "none" and not agent_profile_id:
        raise click.ClickException("--agent-tools requires --agent-profile-id")
    if agent_profile_id:
        if backend != "s2s-local":
            raise click.ClickException(
                "client-owned agent profiles currently require --backend s2s-local"
            )
        if profile_owned_context:
            raise click.ClickException(
                "--agent-profile-id cannot be combined with --profile-owned-context"
            )
        if instructions is not None:
            raise click.ClickException(
                "--agent-profile-id cannot be combined with --instructions"
            )
        try:
            agent_profile = compose_agent_profile(
                profile_id=agent_profile_id,
                public_dir=agent_profile_public_dir,
                private_dir=agent_profile_private_dir,
                source_format=agent_profile_format,
            )
        except AgentProfileError as exc:
            raise click.ClickException(f"invalid agent profile: {exc}") from exc
        if agent_tools == "time-web":
            agent_profile = with_reception_tool_instructions(agent_profile)
            tool_registry = build_reception_tool_registry()
        elif agent_tools == "reference-test":
            tool_registry = build_reference_tool_registry(agent_profile.reference_store)
        try:
            agent_profile = with_session_date(agent_profile)
        except AgentProfileError as exc:
            raise click.ClickException(f"invalid agent profile: {exc}") from exc
        backend_instructions = agent_profile.instructions
        instructions_provenance = agent_profile.provenance()
    else:
        backend_instructions, instructions_provenance = _load_backend_instructions(
            instructions_file=instructions_file,
            instructions=instructions,
            profile_owned_context=profile_owned_context,
        )
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
            "perception_threshold": perception_threshold,
            "perception_smooth": perception_smooth,
            "vision_interval": vision_interval,
            "vision_runtime": vision_runtime,
            "broker": {
                "capture_fps": broker_capture_fps,
                "recorder_queue_size": broker_recorder_queue_size,
                "gesture_queue_size": broker_gesture_queue_size,
                "policy_idle_s": broker_policy_idle_s,
            },
            "gesture_running_mode": gesture_running_mode,
            "wave_detection_mode": wave_detection_mode,
            "vision_pipelines": (
                resolved_pipeline_config.to_dict()
                if resolved_pipeline_config is not None
                else None
            ),
            "rerun": {
                "mode": rerun_mode,
                "grpc_url_set": bool(rerun_grpc_url) if "grpc" in rerun_mode else False,
                "image_fps": rerun_image_fps,
                "jpeg_quality": rerun_jpeg_quality,
                "queue_size": rerun_queue_size,
                "save_path": str(rerun_save_path) if rerun_save_path is not None else None,
            },
            "visitor_trigger_profile": resolved_visitor_profile.metadata(smooth=perception_smooth),
            "audio_gate": audio_gate,
            "profile_owned_context": profile_owned_context,
            "agent_profile": (
                {
                    "profile_id": agent_profile.profile_id,
                    "source_format": agent_profile_format,
                    "tool_selection": agent_tools,
                    "source_ids": list(agent_profile.source_ids),
                    "tool_names": tool_registry.names() if tool_registry is not None else [],
                }
                if agent_profile is not None
                else None
            ),
            "ready_cue": ready_cue,
            "ready_cue_hold": ready_cue_hold,
            "conversation_cues": conversation_cues,
            "conversation_cue_high_s": conversation_cue_high_s,
            "conversation_cue_rest_s": conversation_cue_rest_s,
            "hf_voice": hf_voice,
            "hf_realtime_ws_url_set": bool(hf_realtime_ws_url),
            **instructions_provenance,
            "policy_audio_cache_dir": str(policy_audio_cache_dir),
            "scripted_policy_flow": scripted_policy_flow,
            "scripted_policy_gap_s": scripted_policy_gap_s,
            "scripted_policy_timeout_s": scripted_policy_timeout_s,
            "scripted_policy_greeting": scripted_policy_greeting,
            "scripted_playback_wav": str(scripted_playback_wav) if scripted_playback_wav is not None else None,
            "scripted_playback_post_roll_s": scripted_playback_post_roll_s,
        },
        record_audio=record_audio,
        record_video=record_video,
        capture_vision=capture_vision,
        capture_detections=resolved_pipeline_config is not None,
        rerun_path=rerun_save_path,
    )
    video_expected = bool(
        perception
        or (vision_runtime == "broker-v1" and gestures)
        or record_video
        or capture_vision
        or resolved_pipeline_config is not None
        or rerun_mode != "off"
    )
    liveness = RuntimeLiveness(
        run_id=run_id,
        audio_expected=scripted_playback_wav is None,
        video_expected=video_expected,
    )
    heartbeat_writer = (
        HeartbeatWriter(heartbeat_path, liveness, interval_s=heartbeat_interval_s)
        if heartbeat_path is not None
        else None
    )
    if heartbeat_writer is not None:
        heartbeat_writer.start()
    event_loop_pulse_task = asyncio.create_task(
        pulse_event_loop(liveness),
        name="official-runtime-event-loop-liveness",
    )
    rerun_publisher: LiveRerunPublisher | None = None
    detection_manager: LiveDetectionManager | None = None
    door_policy_coordinator_holder: dict[str, LiveDoorPolicyCoordinator] = {}

    def diagnosis_health(event: str, data: Any) -> None:
        recorder.realtime("vision.diagnosis", event=event, **dict(data))

    try:
        if rerun_mode != "off":
            rerun_publisher = LiveRerunPublisher(
                mode=rerun_mode,
                recording_id=run_id,
                grpc_url=rerun_grpc_url,
                save_path=rerun_save_path,
                jpeg_quality=rerun_jpeg_quality,
                image_fps=rerun_image_fps,
                queue_size=rerun_queue_size,
                health_callback=diagnosis_health,
            )
            rerun_publisher.start()
        if resolved_pipeline_config is not None:
            def detection_result(observation: Any) -> None:
                recorder.detection_layer(observation.to_dict())
                if rerun_publisher is not None:
                    rerun_publisher.submit_detection_layer(observation)
                coordinator = door_policy_coordinator_holder.get("coordinator")
                if coordinator is not None:
                    coordinator.submit_detection(observation)

            detection_manager = LiveDetectionManager(
                run_id=run_id,
                config=resolved_pipeline_config,
                result_callback=detection_result,
                health_callback=diagnosis_health,
            )
            detection_manager.start()
    except Exception:
        liveness.set_fault("diagnosis_start_failed")
        liveness.set_phase("failed")
        if detection_manager is not None:
            detection_manager.close()
        if rerun_publisher is not None:
            rerun_publisher.close()
        recorder.close()
        event_loop_pulse_task.cancel()
        if heartbeat_writer is not None:
            heartbeat_writer.close()
        raise
    diagnosis_closed = False

    def close_diagnosis() -> None:
        nonlocal diagnosis_closed
        if diagnosis_closed:
            return
        diagnosis_closed = True
        if detection_manager is not None:
            detection_manager.close()
            diagnosis_health("pipelines_closed", detection_manager.snapshot())
        if rerun_publisher is not None:
            stats = rerun_publisher.close()
            diagnosis_health("rerun_closed", stats.__dict__)

    stop_event = asyncio.Event()
    stop_callbacks: list[Callable[[], None]] = []
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop_event, stop_callbacks)

    robot_session = ReachyRobotSession(
        host=robot_host,
        warmup_audio=warmup_audio,
        warmup_video=(
            warmup_video
            or perception
            or (vision_runtime == "broker-v1" and gestures)
            or record_video
            or resolved_pipeline_config is not None
            or rerun_mode != "off"
        ),
        milestone_callback=lambda name, data: _record_milestone(recorder, run_id, name, **data),
    )
    try:
        mini = await asyncio.to_thread(robot_session.start)
        liveness.set_phase("robot_connected")
    except Exception as exc:
        liveness.set_fault(repr(exc))
        liveness.set_phase("failed")
        close_diagnosis()
        recorder.close()
        event_loop_pulse_task.cancel()
        if heartbeat_writer is not None:
            heartbeat_writer.close()
        raise
    camera_provider = ReachyCameraFrameProvider(mini, on_frame=liveness.video_frame)

    movement_gate = PlaybackMovementGate(on_change=lambda active, reason: recorder.realtime("movement_gate", active=active, reason=reason))
    policy_settings: dict[str, Any] = {"audio_gate_until_wave": audio_gate}
    policy_settings.update(resolved_visitor_profile.parameters.get("reception_policy", {}))
    if scripted_policy_greeting is not None:
        policy_settings["greeting"] = scripted_policy_greeting
    reception_policy = ReceptionPolicy(ReceptionPolicySettings(**policy_settings))
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

    policies = [reception_policy]
    if conversation_cues:
        policies.append(ConversationCuePolicy())
    policy_engine = PolicyEngine(policies, capabilities=capabilities, context=context)
    policy_sink.bind(policy_engine, loop)
    door_policy_coordinator: LiveDoorPolicyCoordinator | None = None
    if door_policy_enabled:
        def door_policy_result(door_observation: Any, policy_observation: Any) -> None:
            recorder.realtime(
                "vision.door_policy",
                door=door_observation.to_dict(),
                policy=policy_observation.to_dict(),
            )
            for event in policy_observation.events:
                event_sink.emit(
                    RuntimeEvent(
                        kind=f"vision.{event['kind']}",
                        source="official_runtime.door_policy",
                        data=event,
                    )
                )

        door_policy_coordinator = LiveDoorPolicyCoordinator(
            result_callback=door_policy_result,
            health_callback=lambda event, data: diagnosis_health(
                f"door_policy.{event}", data
            ),
            observer_settings=DoorObserverSettings(
                **resolved_visitor_profile.parameters["door_observer"]
            ),
            policy_settings=DoorPolicySettings(
                **resolved_visitor_profile.parameters["door_policy"]
            ),
        )
        door_policy_coordinator_holder["coordinator"] = door_policy_coordinator

    runtime_observer = CompositeRuntimeObserver(reception_policy, recorder, movement_gate)
    audio_source = ReachyAudioSource(
        mini,
        max_duration_s=duration,
        stop_event=stop_event,
        on_frame=liveness.audio_frame,
    )
    audio_sink = ReachyAudioSink(mini)
    if scripted_playback_wav is not None:
        try:
            await _run_scripted_playback_wav(
                wav_path=scripted_playback_wav,
                audio_sink=audio_sink,
                event_sink=event_sink,
                recorder=recorder,
                run_id=run_id,
                post_roll_s=scripted_playback_post_roll_s,
            )
        finally:
            liveness.set_phase("stopping")
            stop_event.set()
            try:
                await audio_sink.close()
                await asyncio.to_thread(robot_session.stop)
            finally:
                close_diagnosis()
                recorder.close()
                liveness.set_phase("stopped")
                event_loop_pulse_task.cancel()
                if heartbeat_writer is not None:
                    heartbeat_writer.close()
        click.echo(f"official runtime live artifacts: {recorder.manifest_path}")
        return

    handler = _build_handler(
        backend=backend,
        event_sink=event_sink,
        instructions=backend_instructions,
        instructions_source=instructions_provenance["instructions_source"],
        instructions_sha256=instructions_provenance["instructions_sha256"],
        hf_voice=hf_voice,
        hf_realtime_ws_url=hf_realtime_ws_url,
        livekit_url=livekit_url,
        livekit_api_key=livekit_api_key,
        livekit_api_secret=livekit_api_secret,
        livekit_token=livekit_token,
        livekit_room=livekit_room,
        livekit_agent_name=livekit_agent_name,
        livekit_dispatch_agent=livekit_dispatch_agent,
        camera_worker=camera_provider,
        reachy_mini=mini,
        agent_profile=agent_profile,
        tool_registry=tool_registry,
    )
    _register_handler_policy_speech(capabilities, handler)
    _register_handler_conversation_session(capabilities, handler)
    ready_cue_task: asyncio.Task[None] | None = None
    scripted_flow_task: asyncio.Task[None] | None = None

    async def on_runtime_ready() -> None:
        nonlocal ready_cue_task, scripted_flow_task
        liveness.set_phase("ready")
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

    def on_runtime_input_done() -> None:
        liveness.set_phase("stopping")

    runtime = OfficialStyleStreamRuntime(
        handler=handler,
        audio_source=audio_source,
        audio_sink=audio_sink,
        event_sink=event_sink,
        runtime_observer=runtime_observer,
        on_ready=on_runtime_ready,
        on_input_done=on_runtime_input_done,
        emit_timeout=0.1,
        drain_idle_polls=200,
    )
    stop_callbacks.append(runtime.stop)
    vision_task: asyncio.Task[None] | None = None
    policy_tick_task: asyncio.Task[None] | None = None
    vision_ready = asyncio.Event()

    runtime_error: BaseException | None = None
    try:
        await policy_engine.start()
        policy_tick_task = asyncio.create_task(
            _run_policy_tick_loop(event_sink=event_sink, stop_event=stop_event),
            name="official-runtime-policy-ticks",
        )
        if (
            perception
            or (vision_runtime == "broker-v1" and gestures)
            or record_video
            or capture_vision
            or detection_manager is not None
            or rerun_publisher is not None
        ):
            vision_loop = _vision_loop if vision_runtime == "serial-v1" else _broker_vision_loop
            vision_kwargs: dict[str, Any] = {
                "camera_provider": camera_provider,
                "recorder": recorder,
                "diagnostic_sink": vision_diagnostic_sink,
                "stop_event": stop_event,
                "ready_event": vision_ready,
                "perception_enabled": perception,
                "threshold": perception_threshold,
                "smooth": perception_smooth,
                "gestures": gestures,
                "visitor_trigger_profile": resolved_visitor_profile.name,
                "run_id": run_id,
                "detection_manager": detection_manager,
                "rerun_publisher": rerun_publisher,
                "door_policy_coordinator": door_policy_coordinator,
            }
            if vision_runtime == "serial-v1":
                vision_kwargs.update(
                    policy_engine=policy_engine,
                    interval_s=vision_interval,
                )
            else:
                vision_kwargs.update(
                    policy_event_sink=policy_sink,
                    capture_fps=broker_capture_fps,
                    recorder_queue_size=broker_recorder_queue_size,
                    gesture_queue_size=broker_gesture_queue_size,
                    policy_idle_s=broker_policy_idle_s,
                    gesture_running_mode=gesture_running_mode,
                    wave_detection_mode=wave_detection_mode,
                )
            vision_task = asyncio.create_task(
                vision_loop(**vision_kwargs),
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
    except BaseException as exc:
        runtime_error = exc
        liveness.set_fault(repr(exc))
        liveness.set_phase("failed")
        raise
    finally:
        if runtime_error is None:
            liveness.set_phase("stopping")
        stop_event.set()
        runtime.stop()
        if policy_tick_task is not None:
            await policy_tick_task
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
        await policy_sink.flush()
        await policy_engine.stop()
        await policy_sink.drain()
        recorder.runtime_summary("policy_event_sink", policy_sink.snapshot())
        try:
            await asyncio.to_thread(robot_session.stop)
        finally:
            close_diagnosis()
            recorder.close()
            if runtime_error is None:
                liveness.set_phase("stopped")
            event_loop_pulse_task.cancel()
            try:
                await event_loop_pulse_task
            except asyncio.CancelledError:
                pass
            if heartbeat_writer is not None:
                heartbeat_writer.close()

    click.echo(f"official runtime live artifacts: {recorder.manifest_path}")


def _record_milestone(recorder: ArtifactRecorder, run_id: str, name: str, **data: Any) -> None:
    recorder.realtime("runtime.milestone", milestone=name, **data)
    click.echo(_format_milestone(run_id, name, data), err=True)


def _register_handler_conversation_session(
    capabilities: CapabilityRegistry,
    handler: Any,
) -> bool:
    begin_conversation_session = getattr(handler, "begin_conversation_session", None)
    if not callable(begin_conversation_session):
        return False

    async def begin_handler_conversation_session(context: RuntimeContext) -> Any:
        return await begin_conversation_session()

    capabilities.register("begin_conversation_session", begin_handler_conversation_session)
    return True


def _register_handler_policy_speech(
    capabilities: CapabilityRegistry,
    handler: Any,
) -> bool:
    request_speech = getattr(handler, "request_speech", None)
    if not callable(request_speech):
        return False

    async def speak_text(
        context: RuntimeContext,
        text: str,
        reason: str,
        event: RuntimeEvent,
    ) -> bool:
        metadata = {
            "source": "reception_policy",
            "reason": reason,
            "trigger_event": event.kind,
        }
        return bool(await request_speech(text, metadata=metadata))

    capabilities.register("speak_text", speak_text)
    return True


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
        depart_step = (
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
        )
        approach_step = (
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
        )
        if flow == "goodbye":
            steps = [depart_step]
        elif flow == "greet":
            steps = [approach_step]
        elif flow == "goodbye-greet":
            steps = [depart_step, approach_step]
        else:
            raise ValueError(f"unsupported scripted policy flow: {flow}")

        for index, (label, event) in enumerate(steps, start=1):
            marker = event_waiter.marker()
            _record_milestone(recorder, run_id, "scripted_policy_step_started", step=label, index=index)
            sway = asyncio.create_task(_sway_head(), name="scripted-policy-head-sway")
            try:
                await policy_engine.handle_event(event)
                audio_event = await event_waiter.wait_for(
                    lambda runtime_event: runtime_event.kind == "assistant.audio.done",
                    after=marker,
                    timeout_s=timeout_s,
                )
            finally:
                await _stop_head_sway(sway)
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


async def _run_scripted_playback_wav(
    *,
    wav_path: Path,
    audio_sink: Any,
    event_sink: EventSink,
    recorder: ArtifactRecorder,
    run_id: str,
    post_roll_s: float,
) -> None:
    _record_milestone(recorder, run_id, "scripted_playback_started", wav_path=str(wav_path))
    frame = load_policy_audio_frame(wav_path)
    sample_rate, audio = frame
    frame_data = _policy_audio_frame_data(sample_rate, audio)
    metadata = {
        "event_type": "scripted_playback",
        "path": str(wav_path),
    }
    _record_milestone(recorder, run_id, "scripted_playback_audio_loaded", **frame_data)
    event_sink.emit(
        RuntimeEvent(
            kind="assistant.audio.started",
            source="official_runtime.scripted_playback",
            data={"metadata": metadata, **frame_data},
        )
    )
    recorder.record_output_audio_frame(sample_rate, audio, metadata=metadata)
    event_sink.emit(
        RuntimeEvent(
            kind="audio.output_frame",
            source="official_runtime.scripted_playback",
            data={"metadata": metadata, **frame_data},
        )
    )
    await audio_sink.write(frame)
    drain = getattr(audio_sink, "drain", None)
    if callable(drain):
        await drain()
    if post_roll_s > 0:
        await asyncio.sleep(post_roll_s)
    event_sink.emit(
        RuntimeEvent(
            kind="assistant.audio.done",
            source="official_runtime.scripted_playback",
            data={"reason": "scripted_playback", "path": str(wav_path)},
        )
    )
    _record_milestone(recorder, run_id, "scripted_playback_completed", **frame_data)


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


def _set_head(pose: tuple[float, float, float]) -> None:
    from reachy_mini_brain import robot

    pitch, roll, yaw = pose
    robot.set_target(pitch=pitch, roll=roll, yaw=yaw)


HEAD_SWAY_YAW_DEG = 20.0
HEAD_SWAY_PERIOD_S = 2.0
HEAD_SWAY_CYCLES = 2


async def _sway_head(
    *,
    update_interval_s: float = 0.1,
    ramp_s: float = 0.4,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Turn the head left/right during scripted policy preflight speech."""

    duration_s = HEAD_SWAY_CYCLES * HEAD_SWAY_PERIOD_S
    started = clock()
    while True:
        elapsed = clock() - started
        if elapsed >= duration_s:
            break
        envelope = min(1.0, elapsed / ramp_s) if ramp_s > 0 else 1.0
        yaw = HEAD_SWAY_YAW_DEG * math.sin(2.0 * math.pi * elapsed / HEAD_SWAY_PERIOD_S) * envelope
        await asyncio.to_thread(_set_head, (0.0, 0.0, yaw))
        await asyncio.sleep(update_interval_s)
    await asyncio.to_thread(_set_head, (0.0, 0.0, 0.0))


async def _stop_head_sway(task: asyncio.Task[None]) -> None:
    """Let scripted preflight sway finish, then return the head to neutral."""

    timeout_s = HEAD_SWAY_CYCLES * HEAD_SWAY_PERIOD_S + 1.0
    try:
        await asyncio.wait_for(task, timeout=timeout_s)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    await asyncio.to_thread(_set_head, (0.0, 0.0, 0.0))


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
    instructions_source: str | None,
    instructions_sha256: str | None,
    hf_voice: str,
    hf_realtime_ws_url: str,
    livekit_url: str,
    livekit_api_key: str,
    livekit_api_secret: str,
    livekit_token: str,
    livekit_room: str,
    livekit_agent_name: str,
    livekit_dispatch_agent: bool,
    camera_worker: Any | None,
    reachy_mini: Any | None,
    agent_profile: ComposedAgentProfile | None = None,
    tool_registry: ToolRegistry | None = None,
) -> Any:
    if backend == "s2s-local":
        tool_context = (
            ToolExecutionContext(
                profile_id=agent_profile.profile_id,
                visitor_session_id="pre-session",
                reference_store=agent_profile.reference_store,
                event_sink=event_sink,
            )
            if agent_profile is not None
            else None
        )
        return S2SRealtimeHandler(
            event_sink=event_sink,
            realtime_ws_url=hf_realtime_ws_url,
            instructions=instructions,
            instructions_source=instructions_source,
            instructions_sha256=instructions_sha256,
            voice=hf_voice,
            tool_registry=tool_registry,
            tool_context=tool_context,
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
    visitor_trigger_profile: str,
    run_id: str,
    detection_manager: LiveDetectionManager | None = None,
    rerun_publisher: LiveRerunPublisher | None = None,
    door_policy_coordinator: LiveDoorPolicyCoordinator | None = None,
) -> None:
    pipeline = (
        PerceptionPipeline(
            threshold=threshold,
            smooth=smooth,
            gestures=gestures,
            event_sink=diagnostic_sink,
            visitor_trigger_profile=visitor_trigger_profile,
            observation_mode="live",
            observation_run_id=run_id,
        )
        if perception_enabled
        else None
    )
    if pipeline is not None and gestures:
        pipeline.ensure_gesture_detector()
    if ready_event is not None:
        ready_event.set()
    fps = 1.0 / interval_s if interval_s > 0 else 5.0
    frame_index = 0
    while not stop_event.is_set():
        frame = camera_provider.get_latest_frame()
        if frame is not None:
            frame_ts = time.time()
            events: list[dict[str, Any]] = []
            people = 0
            tracks: list[dict[str, Any]] = []
            if pipeline is not None:
                events, people, tracks = pipeline.process(
                    frame,
                    bgr=True,
                    ts=frame_ts,
                    frame_index=frame_index,
                    timestamp_source="live_camera",
                )
            if rerun_publisher is not None or detection_manager is not None:
                frame_packet = FramePacket(
                    frame_index=frame_index,
                    frame_ts=frame_ts,
                    frame_bgr=frame.copy(),
                )
                if rerun_publisher is not None:
                    rerun_publisher.submit_frame(frame_packet)
                    if pipeline is not None and pipeline.last_observation is not None:
                        rerun_publisher.submit_visitor_observation(pipeline.last_observation)
                if detection_manager is not None:
                    if door_policy_coordinator is not None:
                        door_policy_coordinator.submit_frame(
                            frame_packet,
                            pipeline.last_observation if pipeline is not None else None,
                        )
                    detection_manager.submit(frame_packet)
            recorder.vision_frame(frame, people=people, tracks=tracks, events=events, fps=fps, ts=frame_ts)
            for event in events:
                await policy_engine.handle_event(
                    RuntimeEvent(kind=f"vision.{event['kind']}", source="official_runtime.vision", data=event)
                )
            frame_index += 1
        await asyncio.sleep(max(0.01, interval_s))


async def _broker_vision_loop(
    *,
    camera_provider: ReachyCameraFrameProvider,
    policy_event_sink: EventSink,
    recorder: ArtifactRecorder,
    diagnostic_sink: EventSink,
    stop_event: asyncio.Event,
    ready_event: asyncio.Event | None,
    capture_fps: float,
    recorder_queue_size: int,
    gesture_queue_size: int,
    policy_idle_s: float,
    perception_enabled: bool,
    threshold: float,
    smooth: int,
    gestures: bool,
    gesture_running_mode: str,
    wave_detection_mode: str,
    visitor_trigger_profile: str,
    run_id: str,
    detection_manager: LiveDetectionManager | None = None,
    rerun_publisher: LiveRerunPublisher | None = None,
    door_policy_coordinator: LiveDoorPolicyCoordinator | None = None,
) -> None:
    policy_pipeline: dict[str, PerceptionPipeline] = {}
    gesture_pipeline: dict[str, PerceptionPipeline] = {}

    def start_policy() -> None:
        if perception_enabled:
            policy_pipeline["pipeline"] = PerceptionPipeline(
                threshold=threshold,
                smooth=smooth,
                gestures=False,
                event_sink=diagnostic_sink,
                visitor_trigger_profile=visitor_trigger_profile,
                observation_mode="live",
                observation_run_id=run_id,
            )

    def process_policy(packet: FramePacket) -> None:
        pipeline = policy_pipeline.get("pipeline")
        events: list[dict[str, Any]] = []
        people = 0
        tracks: list[dict[str, Any]] = []
        if pipeline is not None:
            events, people, tracks = pipeline.process(
                packet.frame_bgr,
                bgr=True,
                ts=packet.frame_ts,
                frame_index=packet.frame_index,
                timestamp_source="live_camera_broker",
            )
        observation = pipeline.last_observation if pipeline is not None else None
        recorder.capture_vision_frame(
            people=people,
            tracks=tracks,
            events=events,
            ts=packet.frame_ts,
            source_frame_id=packet.frame_index,
        )
        if rerun_publisher is not None and observation is not None:
            rerun_publisher.submit_visitor_observation(observation)
        if detection_manager is not None:
            if door_policy_coordinator is not None:
                door_policy_coordinator.submit_frame(packet, observation)
            detection_manager.submit(packet)
        for event in events:
            policy_event_sink.emit(
                RuntimeEvent(
                    kind=f"vision.{event['kind']}",
                    source="official_runtime.vision_broker.policy",
                    data={
                        **event,
                        "source_frame_id": packet.frame_index,
                        "source_frame_ts": packet.frame_ts,
                    },
                    ts=packet.frame_ts,
                )
            )

    def start_gesture() -> None:
        pipeline = PerceptionPipeline(
            gestures=True,
            gesture_running_mode=gesture_running_mode,
            wave_detection_mode=wave_detection_mode,
            event_sink=diagnostic_sink,
            visitor_trigger_profile=visitor_trigger_profile,
            observation_mode="live",
            observation_run_id=run_id,
            gesture_only=True,
        )
        pipeline.ensure_gesture_detector()
        gesture_pipeline["pipeline"] = pipeline

    def process_gesture(packet: FramePacket) -> None:
        event = gesture_pipeline["pipeline"].process_gesture(
            packet.frame_bgr,
            ts=packet.frame_ts,
            frame_index=packet.frame_index,
        )
        if event is None:
            return
        policy_event_sink.emit(
            RuntimeEvent(
                kind=f"vision.{event['kind']}",
                source="official_runtime.vision_broker.gesture",
                data={
                    **event,
                    "source_frame_id": packet.frame_index,
                    "source_frame_ts": packet.frame_ts,
                },
                ts=packet.frame_ts,
            )
        )

    consumers: list[VisionConsumerSpec] = []
    if recorder.record_video_enabled:
        consumers.append(
            VisionConsumerSpec(
                name="recorder",
                callback=lambda packet: recorder.record_video_frame(
                    packet.frame_bgr,
                    fps=capture_fps,
                    ts=packet.frame_ts,
                    source_frame_id=packet.frame_index,
                ),
                mode="fifo",
                capacity=recorder_queue_size,
            )
        )
    if gestures:
        consumers.append(
            VisionConsumerSpec(
                name="gesture",
                callback=process_gesture,
                mode="fifo",
                capacity=gesture_queue_size,
                start_callback=start_gesture,
            )
        )
    if perception_enabled or recorder.capture_vision_enabled or detection_manager is not None:
        consumers.append(
            VisionConsumerSpec(
                name="policy",
                callback=process_policy,
                mode="latest",
                capacity=1,
                idle_after_s=policy_idle_s,
                start_callback=start_policy,
            )
        )
    if rerun_publisher is not None:
        consumers.append(
            VisionConsumerSpec(
                name="rerun",
                callback=rerun_publisher.submit_frame,
                mode="latest",
                capacity=1,
            )
        )
    if not consumers:
        raise RuntimeError("broker vision runtime has no enabled consumers")

    def broker_health(event: str, data: Any) -> None:
        recorder.realtime("vision.broker", event=event, **dict(data))

    runtime = BrokerVisionRuntime(
        frame_source=camera_provider.get_latest_frame,
        capture_fps=capture_fps,
        consumers=tuple(consumers),
        health_callback=broker_health,
    )
    try:
        await asyncio.to_thread(runtime.start)
        if ready_event is not None:
            ready_event.set()
        while not stop_event.is_set():
            if runtime.failure is not None:
                raise RuntimeError("broker vision worker failed") from runtime.failure
            await asyncio.sleep(0.1)
    finally:
        snapshot = await asyncio.to_thread(runtime.close)
        recorder.runtime_summary("vision_broker", snapshot)
        recorder.realtime("vision.broker.final", snapshot=snapshot)


class _AsyncPolicyEventSink:
    def __init__(self) -> None:
        self.engine: PolicyEngine | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue[RuntimeEvent | None] | None = None
        self.worker: asyncio.Task[None] | None = None
        self.errors: list[BaseException] = []
        self._closed = False
        self._submitted_events = 0
        self._handled_events = 0
        self._dropped_events = 0

    def bind(self, engine: PolicyEngine, loop: asyncio.AbstractEventLoop) -> None:
        self.engine = engine
        self.loop = loop
        self.queue = asyncio.Queue(maxsize=256)
        self.worker = loop.create_task(
            self._run(),
            name="official-runtime-policy-events",
        )

    def emit(self, event: RuntimeEvent) -> None:
        if self.engine is None or self.loop is None or self._closed:
            return
        self.loop.call_soon_threadsafe(self._enqueue, event)

    def _enqueue(self, event: RuntimeEvent) -> None:
        if self.queue is None or self._closed:
            return
        try:
            self.queue.put_nowait(event)
            self._submitted_events += 1
        except asyncio.QueueFull:
            self._dropped_events += 1
            self.errors.append(RuntimeError("policy event queue overflow"))

    async def _run(self) -> None:
        assert self.queue is not None
        assert self.engine is not None
        while True:
            event = await self.queue.get()
            try:
                if event is None:
                    return
                await self.engine.handle_event(event)
                self._handled_events += 1
            except BaseException as exc:  # noqa: BLE001
                self.errors.append(exc)
            finally:
                self.queue.task_done()

    async def flush(self) -> None:
        if self.queue is None:
            return
        await asyncio.sleep(0)
        await self.queue.join()

    async def drain(self) -> None:
        if self.queue is None or self.worker is None or self._closed:
            return
        await self.flush()
        self._closed = True
        self.queue.put_nowait(None)
        await self.worker

    def snapshot(self) -> dict[str, Any]:
        return {
            "submitted_events": self._submitted_events,
            "handled_events": self._handled_events,
            "dropped_events": self._dropped_events,
            "queue_capacity": self.queue.maxsize if self.queue is not None else 0,
            "queue_depth": self.queue.qsize() if self.queue is not None else 0,
            "errors": [repr(error) for error in self.errors],
            "closed": self._closed,
        }


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
    callbacks: list[Callable[[], None]] | None = None,
) -> None:
    def request_stop() -> None:
        stop_event.set()
        for callback in tuple(callbacks if callbacks is not None else ()):
            try:
                callback()
            except Exception:
                pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass


if __name__ == "__main__":
    cli()
