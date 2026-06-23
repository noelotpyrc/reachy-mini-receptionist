"""Capability registry shared by policy controllers and realtime tools."""

from __future__ import annotations

import base64
import inspect
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from .events import EventSink, InMemoryEventSink, RuntimeEvent


CapabilityCallable = Callable[..., Any | Awaitable[Any]]


@dataclass(slots=True)
class RuntimeContext:
    """Shared context passed through runtime policies and capabilities."""

    event_sink: EventSink = field(default_factory=InMemoryEventSink)
    state: MutableMapping[str, Any] = field(default_factory=dict)


class CapabilityRegistry:
    """Registry for named robot/app actions.

    Capabilities are deliberately plain callables. A deterministic policy and a
    realtime model tool can invoke the same named action through this registry.
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityCallable] = {}

    def register(self, name: str, capability: CapabilityCallable) -> None:
        if not name:
            raise ValueError("Capability name must not be empty")
        if name in self._capabilities:
            raise ValueError(f"Capability already registered: {name}")
        self._capabilities[name] = capability

    def names(self) -> list[str]:
        return sorted(self._capabilities)

    async def invoke(self, name: str, context: RuntimeContext, **kwargs: Any) -> Any:
        try:
            capability = self._capabilities[name]
        except KeyError as exc:
            raise KeyError(f"Unknown capability: {name}") from exc

        context.event_sink.emit(
            RuntimeEvent(
                kind="capability.started",
                source=name,
                data={"args": _summarize_capability_value(dict(kwargs))},
            )
        )
        try:
            result = capability(context, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            context.event_sink.emit(
                RuntimeEvent(
                    kind="capability.failed",
                    source=name,
                    data={"error": repr(exc)},
                )
            )
            raise

        context.event_sink.emit(
            RuntimeEvent(
                kind="capability.completed",
                source=name,
                data={"result": _summarize_capability_value(result)},
            )
        )
        return result


def _summarize_capability_value(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """Return a log-safe capability payload summary."""

    if depth > 5:
        return f"<{type(value).__name__}>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if key in {"b64_im", "audio", "image"}:
            decoded_bytes = None
            try:
                decoded_bytes = len(base64.b64decode(value, validate=True))
            except Exception:
                pass
            return {"base64_chars": len(value), "decoded_bytes": decoded_bytes}
        if len(value) > 1000:
            return {"text": value[:1000], "truncated_chars": len(value) - 1000}
        return value
    if isinstance(value, dict):
        return {str(k): _summarize_capability_value(v, key=str(k), depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        limit = 20
        items = [_summarize_capability_value(item, depth=depth + 1) for item in value[:limit]]
        if len(value) > limit:
            return {"items": items, "truncated_items": len(value) - limit}
        return items
    if hasattr(value, "shape"):
        return {
            "type": type(value).__name__,
            "shape": list(getattr(value, "shape", [])),
            "dtype": str(getattr(value, "dtype", "")),
        }
    return repr(value)
