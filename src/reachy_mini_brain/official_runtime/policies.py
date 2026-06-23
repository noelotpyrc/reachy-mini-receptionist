"""Policy controller primitives for the isolated official-style refactor."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .capabilities import CapabilityRegistry, RuntimeContext
from .events import RuntimeEvent


PolicyResult = None | Awaitable[None]


class PolicyController(Protocol):
    """Deterministic or model-backed policy that reacts to runtime events."""

    name: str

    def start(
        self,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> PolicyResult:
        """Start the policy."""

    def stop(
        self,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> PolicyResult:
        """Stop the policy."""

    def handle_event(
        self,
        event: RuntimeEvent,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> PolicyResult:
        """Handle a runtime event."""


async def _maybe_await(result: PolicyResult) -> None:
    if inspect.isawaitable(result):
        await result


class PolicyEngine:
    """Fan out runtime events to registered policies."""

    def __init__(
        self,
        policies: list[PolicyController] | None = None,
        *,
        capabilities: CapabilityRegistry | None = None,
        context: RuntimeContext | None = None,
    ) -> None:
        self.policies = list(policies or [])
        self.capabilities = capabilities or CapabilityRegistry()
        self.context = context or RuntimeContext()

    def add(self, policy: PolicyController) -> None:
        self.policies.append(policy)

    async def start(self) -> None:
        for policy in self.policies:
            self.context.event_sink.emit(
                RuntimeEvent(kind="policy.started", source=policy.name)
            )
            await _maybe_await(policy.start(self.context, self.capabilities))

    async def stop(self) -> None:
        for policy in reversed(self.policies):
            await _maybe_await(policy.stop(self.context, self.capabilities))
            self.context.event_sink.emit(
                RuntimeEvent(kind="policy.stopped", source=policy.name)
            )

    async def handle_event(self, event: RuntimeEvent) -> None:
        for policy in self.policies:
            try:
                await _maybe_await(
                    policy.handle_event(event, self.context, self.capabilities)
                )
            except Exception as exc:
                self.context.event_sink.emit(
                    RuntimeEvent(
                        kind="policy.failed",
                        source=policy.name,
                        data={"event_kind": event.kind, "error": repr(exc)},
                    )
                )
                raise


@dataclass(slots=True)
class RulePolicy:
    """Minimal deterministic policy for early reception UX spikes."""

    name: str
    trigger_kind: str
    capability_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def start(
        self,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> None:
        return None

    def stop(
        self,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> None:
        return None

    async def handle_event(
        self,
        event: RuntimeEvent,
        context: RuntimeContext,
        capabilities: CapabilityRegistry,
    ) -> None:
        if event.kind != self.trigger_kind:
            return
        context.event_sink.emit(
            RuntimeEvent(
                kind="policy.triggered",
                source=self.name,
                data={
                    "event_kind": event.kind,
                    "capability": self.capability_name,
                },
            )
        )
        await capabilities.invoke(
            self.capability_name,
            context,
            event=event,
            **dict(self.arguments),
        )
