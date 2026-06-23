"""Conversation latency cue policy for the official-runtime path."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .capabilities import CapabilityRegistry, RuntimeContext
from .events import RuntimeEvent


@dataclass(slots=True)
class ConversationCuePolicySettings:
    """Configuration for visible thinking cues during backend latency."""

    enabled: bool = True
    start_capability: str = "start_thinking_cue"
    stop_capability: str = "stop_thinking_cue"
    min_start_interval_s: float = 0.25
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)


class ConversationCuePolicy:
    """Start a visible cue after user input and stop it before assistant audio.

    This policy owns no STT, LLM, memory, TTS, or conversation behavior. It only
    maps runtime/backend lifecycle events to movement capabilities.
    """

    name = "conversation_cue"

    def __init__(self, settings: ConversationCuePolicySettings | None = None) -> None:
        self.settings = settings or ConversationCuePolicySettings()
        self._thinking_active = False
        self._robot_speaking = False
        self._last_start_ts = 0.0

    def start(self, context: RuntimeContext, capabilities: CapabilityRegistry) -> None:
        context.event_sink.emit(RuntimeEvent(kind="policy.conversation_cue_started", source=self.name))

    async def stop(self, context: RuntimeContext, capabilities: CapabilityRegistry) -> None:
        await self._stop_cue(context, capabilities, reason="policy_stop")
        context.event_sink.emit(RuntimeEvent(kind="policy.conversation_cue_stopped", source=self.name))

    async def handle_event(
        self,
        event: RuntimeEvent,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> None:
        if not self.settings.enabled:
            return
        if _is_robot_audio_started(event):
            self._robot_speaking = True
            await self._stop_cue(context, capabilities, reason=_event_reason(event))
            return
        if _is_robot_audio_done(event):
            self._robot_speaking = False
            await self._stop_cue(context, capabilities, reason=_event_reason(event))
            return
        if _is_user_turn_ready(event):
            await self._start_cue(context, capabilities, event)

    async def _start_cue(
        self,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
        event: RuntimeEvent,
    ) -> None:
        if self._thinking_active:
            self._policy_event(context, "start_suppressed", reason="already_thinking", event_kind=event.kind)
            return
        if self._robot_speaking:
            self._policy_event(context, "start_suppressed", reason="robot_speaking", event_kind=event.kind)
            return
        now = self.settings.clock()
        if now - self._last_start_ts < self.settings.min_start_interval_s:
            self._policy_event(context, "start_suppressed", reason="min_start_interval", event_kind=event.kind)
            return
        if self.settings.start_capability not in capabilities.names():
            self._policy_event(context, "start_unavailable", capability=self.settings.start_capability)
            return
        result = await capabilities.invoke(self.settings.start_capability, context, reason=_event_reason(event))
        if result is False:
            self._policy_event(context, "start_declined", capability=self.settings.start_capability)
            return
        self._thinking_active = True
        self._last_start_ts = now
        self._policy_event(context, "thinking_started", event_kind=event.kind)

    async def _stop_cue(
        self,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
        *,
        reason: str,
    ) -> None:
        if not self._thinking_active and reason != "policy_stop":
            return
        if self.settings.stop_capability not in capabilities.names():
            self._policy_event(context, "stop_unavailable", capability=self.settings.stop_capability, reason=reason)
            self._thinking_active = False
            return
        await capabilities.invoke(self.settings.stop_capability, context, reason=reason)
        if self._thinking_active:
            self._policy_event(context, "thinking_stopped", reason=reason)
        self._thinking_active = False

    @staticmethod
    def _policy_event(context: RuntimeContext, kind: str, **data: Any) -> None:
        context.event_sink.emit(RuntimeEvent(kind=f"policy.conversation_cue.{kind}", source="conversation_cue", data=data))


def _is_user_turn_ready(event: RuntimeEvent) -> bool:
    if event.kind == "assistant.thinking.started":
        return True
    return _is_final_user_transcript(event)


def _is_robot_audio_started(event: RuntimeEvent) -> bool:
    return event.kind == "assistant.audio.started"


def _is_robot_audio_done(event: RuntimeEvent) -> bool:
    if event.kind in {"assistant.audio.done", "runtime.stopped", "livekit.handler.stopped"}:
        return True
    return False


def _is_final_user_transcript(event: RuntimeEvent) -> bool:
    kind = _normalized_kind(event.kind)
    if kind not in {
        "conversation.item.input_audio_transcription.completed",
        "gemini.user_transcription_completed",
        "livekit.room.transcription",
        "backend.transcript.final",
    }:
        return False
    if event.data.get("final") is False:
        return False
    role = event.data.get("role")
    if role not in (None, "", "user", "transcript", "user_transcript"):
        return False
    text = event.data.get("transcript")
    if text is None:
        text = event.data.get("text")
    if text is None:
        return kind != "livekit.room.transcription"
    return isinstance(text, str) and bool(text.strip())


def _normalized_kind(kind: str) -> str:
    for prefix in ("hf.realtime.", "realtime."):
        if kind.startswith(prefix):
            return kind.removeprefix(prefix)
    return kind


def _event_reason(event: RuntimeEvent) -> str:
    return event.data.get("event_type") or event.kind
