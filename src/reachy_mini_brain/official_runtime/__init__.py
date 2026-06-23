"""Isolated official-style runtime spike.

This package is intentionally not wired into the legacy reception daemon yet.
It models the official conversation app's stream/handler/policy shape so we
can test the new architecture offline before making it the robot path.
"""

from .artifacts import ArtifactRecorder
from .camera import camera_question, encode_bgr_frame_as_jpeg, register_camera_capabilities, set_head_tracking
from .capabilities import CapabilityRegistry, RuntimeContext
from .conversation_cues import ConversationCuePolicy, ConversationCuePolicySettings
from .env import DEFAULT_ENV_PATH, PROJECT_ROOT, load_project_env
from .events import CompositeEventSink, InMemoryEventSink, JsonlEventSink, RuntimeEvent
from .livekit_handler import LiveKitBackendConfig, LiveKitRealtimeHandler
from .livekit_room_bridge import LiveKitRoomBridge
from .moves import AntennaCueController, AntennaPulseMove, PlaybackMovementGate, queue_antenna_pulse
from .perception import ApproachTracker, GestureDetector, PerceptionPipeline, PersonDetector
from .policies import PolicyEngine, RulePolicy
from .policy_audio_cache import PolicyAudioCache
from .reception import ReceptionPolicy, ReceptionPolicySettings
from .robot_io import ReachyAudioSink, ReachyAudioSource, ReachyCameraFrameProvider, ReachyRobotSession
from .stream_runtime import CompositeRuntimeObserver, OfficialStyleStreamRuntime
from .wav_replay import WavAudioSink, WavAudioSource, run_wav_replay

__all__ = [
    "CapabilityRegistry",
    "ArtifactRecorder",
    "AntennaPulseMove",
    "AntennaCueController",
    "camera_question",
    "CompositeEventSink",
    "ConversationCuePolicy",
    "ConversationCuePolicySettings",
    "DEFAULT_ENV_PATH",
    "InMemoryEventSink",
    "JsonlEventSink",
    "LiveKitBackendConfig",
    "LiveKitRealtimeHandler",
    "LiveKitRoomBridge",
    "OfficialStyleStreamRuntime",
    "ApproachTracker",
    "PolicyEngine",
    "PolicyAudioCache",
    "PROJECT_ROOT",
    "CompositeRuntimeObserver",
    "encode_bgr_frame_as_jpeg",
    "GestureDetector",
    "PerceptionPipeline",
    "PersonDetector",
    "PlaybackMovementGate",
    "register_camera_capabilities",
    "queue_antenna_pulse",
    "set_head_tracking",
    "ReceptionPolicy",
    "ReceptionPolicySettings",
    "ReachyAudioSink",
    "ReachyAudioSource",
    "ReachyCameraFrameProvider",
    "ReachyRobotSession",
    "RulePolicy",
    "RuntimeContext",
    "RuntimeEvent",
    "WavAudioSink",
    "WavAudioSource",
    "load_project_env",
    "run_wav_replay",
]
