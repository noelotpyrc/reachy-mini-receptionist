"""Runtime event primitives for the isolated official-style refactor."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Timestamped event emitted by the new runtime, policies, or capabilities."""

    kind: str
    source: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class EventSink(Protocol):
    """Receives runtime events."""

    def emit(self, event: RuntimeEvent) -> None:
        """Record or forward an event."""


class InMemoryEventSink:
    """Simple event sink used by tests and offline runtime spikes."""

    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


class CompositeEventSink:
    """Fan out events to several sinks."""

    def __init__(self, *sinks: EventSink) -> None:
        self.sinks = list(sinks)

    def emit(self, event: RuntimeEvent) -> None:
        for sink in self.sinks:
            sink.emit(event)


class JsonlEventSink:
    """Append runtime events to a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: RuntimeEvent) -> None:
        payload = {
            "ts": event.ts,
            "kind": event.kind,
            "source": event.source,
            "data": event.data,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True, default=_json_default) + "\n")


def _json_default(value: Any) -> Any:
    """Return a stable JSON fallback for third-party event payload objects."""

    if hasattr(value, "tolist"):
        return value.tolist()
    return repr(value)
