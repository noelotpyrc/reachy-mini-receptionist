"""Deterministic reception policy for the isolated official-style runtime."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .capabilities import CapabilityRegistry, RuntimeContext
from .events import RuntimeEvent


@dataclass(slots=True)
class ReceptionPolicySettings:
    """Configuration for deterministic clinic reception UX."""

    cooldown_s: float = 15.0
    greeting: str = "Welcome!"
    farewell: str = "Goodbye! Have a nice day!"
    conversation_opener: str = "Hi! How can I help?"
    conversation_idle_timeout_s: float = 45.0
    conversation_max_duration_s: float = 480.0
    audio_gate_until_wave: bool = True
    speech_capability: str = "speak_text"
    antenna_capability: str = "antenna_pulse"
    goodbye_tokens: tuple[str, ...] = ("goodbye", "bye", "that's all", "that is all")
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)


class ReceptionPolicy:
    """Reception state machine that reacts to vision and realtime transcript events."""

    name = "reception"

    def __init__(self, settings: ReceptionPolicySettings | None = None) -> None:
        self.settings = settings or ReceptionPolicySettings()
        self._conversation_active = False
        self._conversation_started_at: float | None = None
        self._last_conversation_activity: float | None = None
        self._last_action_ts: dict[str, float] = {}

    @property
    def conversation_active(self) -> bool:
        return self._conversation_active

    def should_forward_audio(self) -> bool:
        if not self.settings.audio_gate_until_wave:
            return True
        return self._conversation_active

    def start(self, context: RuntimeContext, capabilities: CapabilityRegistry) -> None:
        context.event_sink.emit(RuntimeEvent(kind="policy.reception_started", source=self.name))

    def stop(self, context: RuntimeContext, capabilities: CapabilityRegistry) -> None:
        context.event_sink.emit(RuntimeEvent(kind="policy.reception_stopped", source=self.name))

    async def handle_event(
        self,
        event: RuntimeEvent,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> None:
        kind = _event_kind(event)
        if kind == "tick":
            self._handle_tick(context)
            return
        if kind == "approach":
            await self._greet(event, context, capabilities)
            return
        if kind == "depart":
            await self._farewell(event, context, capabilities)
            return
        if kind == "wave":
            await self._open_conversation(event, context, capabilities)
            return
        transcript = _transcript_from_event(event)
        if transcript is not None:
            self._handle_user_transcript(transcript, context)

    async def _greet(
        self,
        event: RuntimeEvent,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> None:
        if self._conversation_active:
            self._policy_event(context, "greet_suppressed", reason="conversation_active", event_kind=event.kind)
            return
        if not self._cooldown_ready("approach"):
            self._policy_event(context, "cooldown_skip", event_kind=event.kind, action="greet")
            return
        self._policy_event(context, "greet", text=self.settings.greeting, event=event.data)
        await self._pulse(context, capabilities)
        await self._speak(context, capabilities, self.settings.greeting, reason="approach", event=event)

    async def _farewell(
        self,
        event: RuntimeEvent,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> None:
        if self._conversation_active:
            self._policy_event(context, "farewell_suppressed", reason="conversation_active", event_kind=event.kind)
            return
        if not self._cooldown_ready("depart"):
            self._policy_event(context, "cooldown_skip", event_kind=event.kind, action="farewell")
            return
        self._policy_event(context, "farewell", text=self.settings.farewell, event=event.data)
        await self._pulse(context, capabilities)
        await self._speak(context, capabilities, self.settings.farewell, reason="depart", event=event)

    async def _open_conversation(
        self,
        event: RuntimeEvent,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> None:
        cooldown_remaining = self._cooldown_remaining("wave")
        self._policy_event(
            context,
            "wave_received",
            event=event.data,
            conversation_active=self._conversation_active,
            cooldown_ready=cooldown_remaining <= 0,
            cooldown_remaining_s=round(cooldown_remaining, 3),
        )
        if self._conversation_active:
            self._policy_event(context, "conversation_already_active", event=event.data)
            return
        if not self._cooldown_ready("wave"):
            self._policy_event(context, "cooldown_skip", event_kind=event.kind, action="conversation_open")
            return
        now = self.settings.clock()
        self._conversation_active = True
        self._conversation_started_at = now
        self._last_conversation_activity = now
        self._policy_event(context, "conversation_opened", event=event.data, audio_gate_open=self.should_forward_audio())
        await self._pulse(context, capabilities)
        await self._speak(context, capabilities, self.settings.conversation_opener, reason="wave", event=event)

    def _handle_user_transcript(self, transcript: str, context: RuntimeContext) -> None:
        if not self._conversation_active:
            return
        self._last_conversation_activity = self.settings.clock()
        lowered = transcript.lower()
        if any(token in lowered for token in self.settings.goodbye_tokens):
            self._close_conversation(context, "explicit_goodbye")

    def _handle_tick(self, context: RuntimeContext) -> None:
        if not self._conversation_active or self._conversation_started_at is None:
            return
        now = self.settings.clock()
        last_activity = self._last_conversation_activity or self._conversation_started_at
        if now - last_activity > self.settings.conversation_idle_timeout_s:
            self._close_conversation(context, "idle_timeout")
            return
        if now - self._conversation_started_at > self.settings.conversation_max_duration_s:
            self._close_conversation(context, "max_duration")

    def _close_conversation(self, context: RuntimeContext, reason: str) -> None:
        if not self._conversation_active:
            return
        self._conversation_active = False
        self._conversation_started_at = None
        self._last_conversation_activity = None
        self._policy_event(context, "conversation_closed", reason=reason, audio_gate_open=self.should_forward_audio())

    async def _pulse(self, context: RuntimeContext, capabilities: CapabilityRegistry) -> None:
        if self.settings.antenna_capability not in capabilities.names():
            self._policy_event(context, "antenna_pulse_unavailable")
            return
        await capabilities.invoke(self.settings.antenna_capability, context)
        self._policy_event(context, "antenna_pulse")

    async def _speak(
        self,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
        text: str,
        *,
        reason: str,
        event: RuntimeEvent,
    ) -> None:
        if self.settings.speech_capability not in capabilities.names():
            self._policy_event(context, "speech_capability_unavailable", reason=reason, text=text)
            return
        await capabilities.invoke(
            self.settings.speech_capability,
            context,
            text=text,
            reason=reason,
            event=event,
        )
        self._policy_event(context, "speech_requested", reason=reason, text=text)

    def _cooldown_ready(self, action: str) -> bool:
        now = self.settings.clock()
        last = self._last_action_ts.get(action)
        if last is None:
            self._last_action_ts[action] = now
            return True
        if now - last < self.settings.cooldown_s:
            return False
        self._last_action_ts[action] = now
        return True

    def _cooldown_remaining(self, action: str) -> float:
        last = self._last_action_ts.get(action)
        if last is None:
            return 0.0
        return max(0.0, self.settings.cooldown_s - (self.settings.clock() - last))

    @staticmethod
    def _policy_event(context: RuntimeContext, kind: str, **data: Any) -> None:
        context.event_sink.emit(RuntimeEvent(kind=f"policy.{kind}", source="reception", data=data))


def _event_kind(event: RuntimeEvent) -> str:
    if event.kind.startswith("vision."):
        return event.kind.removeprefix("vision.")
    if event.kind == "runtime.tick":
        return "tick"
    value = event.data.get("kind") or event.data.get("type")
    return str(value) if value else event.kind


def _transcript_from_event(event: RuntimeEvent) -> str | None:
    kind = _normalized_realtime_kind(event.kind)
    transcript_kinds = {
        "conversation.item.input_audio_transcription.completed",
        "realtime.conversation.item.input_audio_transcription.completed",
        "gemini.user_transcription_completed",
        "realtime.gemini.user_transcription_completed",
        "livekit.room.transcription",
        "backend.transcript.final",
    }
    if kind not in transcript_kinds:
        return None
    if kind == "livekit.room.transcription" and event.data.get("final") is False:
        return None
    role = event.data.get("role")
    if role not in (None, "", "user", "transcript", "user_transcript"):
        return None
    text = event.data.get("transcript")
    if text is None:
        text = event.data.get("text")
    if not isinstance(text, str):
        return None
    return text.strip() or None


def _normalized_realtime_kind(kind: str) -> str:
    if kind.startswith("hf.realtime."):
        return kind.removeprefix("hf.realtime.")
    return kind
