"""Application-owned tool execution for the raw S2S Realtime client."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from jsonschema import SchemaError, ValidationError
from jsonschema.validators import validator_for

from .agent_profile import ReferenceStore
from .events import EventSink, RuntimeEvent


class MemoryStore(Protocol):
    """Future durable-memory boundary; no production implementation exists yet."""

    async def search(
        self,
        *,
        profile_id: str,
        visitor_id: str,
        query: str,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]: ...


@dataclass(slots=True)
class ToolExecutionContext:
    profile_id: str
    visitor_session_id: str
    reference_store: ReferenceStore
    event_sink: EventSink
    visitor_id: str | None = None
    memory_store: MemoryStore | None = None
    cancellation: asyncio.Event = field(default_factory=asyncio.Event)


ToolCallback = Callable[
    [ToolExecutionContext, dict[str, Any]], Any | Awaitable[Any]
]
AuthorizationPolicy = Callable[[ToolExecutionContext], bool]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    callback: ToolCallback
    authorization: AuthorizationPolicy | None = None
    timeout_s: float = 5.0
    max_result_bytes: int = 64 * 1024
    create_response: bool = True


@dataclass(frozen=True, slots=True)
class ToolCallMetadata:
    response_id: str
    output_index: int
    call_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "response_id": self.response_id,
            "output_index": self.output_index,
            "call_id": self.call_id,
        }


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    output: str
    create_response: bool
    ok: bool
    category: str


class ToolRegistry:
    """Validate and execute only explicitly registered tools."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._validators: dict[str, Any] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name:
            raise ValueError("tool name must not be empty")
        if definition.name in self._definitions:
            raise ValueError(f"tool already registered: {definition.name}")
        if definition.timeout_s <= 0:
            raise ValueError("tool timeout must be positive")
        if definition.max_result_bytes <= 0:
            raise ValueError("tool result limit must be positive")
        validator_class = validator_for(definition.parameters)
        try:
            validator_class.check_schema(definition.parameters)
        except SchemaError as exc:
            raise ValueError(
                f"tool {definition.name!r} parameters are not valid JSON Schema"
            ) from exc
        self._definitions[definition.name] = definition
        self._validators[definition.name] = validator_class(definition.parameters)

    def names(self) -> list[str]:
        return sorted(self._definitions)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.parameters,
            }
            for definition in self._definitions.values()
        ]

    async def execute(
        self,
        *,
        name: str,
        arguments: Any,
        context: ToolExecutionContext,
        metadata: ToolCallMetadata,
    ) -> ToolExecutionResult:
        started = time.monotonic()
        common = {**metadata.to_dict(), "tool_name": name}
        self._emit(context, "agent.tool.execution_started", **common)
        try:
            definition = self._definitions.get(name)
            if definition is None:
                return self._error(
                    context,
                    common,
                    started,
                    category="unknown_tool",
                    message="tool is not allowlisted",
                )
            if context.cancellation.is_set():
                raise asyncio.CancelledError
            if not isinstance(arguments, str):
                return self._error(
                    context,
                    common,
                    started,
                    category="invalid_arguments",
                    message="arguments must be a JSON string",
                )
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError:
                return self._error(
                    context,
                    common,
                    started,
                    category="invalid_arguments",
                    message="arguments are not valid JSON",
                )
            if not isinstance(decoded, dict):
                return self._error(
                    context,
                    common,
                    started,
                    category="invalid_arguments",
                    message="arguments must decode to an object",
                )
            try:
                self._validators[name].validate(decoded)
            except ValidationError:
                return self._error(
                    context,
                    common,
                    started,
                    category="invalid_arguments",
                    message="arguments do not match the tool schema",
                )
            if definition.authorization is not None and not definition.authorization(
                context
            ):
                return self._error(
                    context,
                    common,
                    started,
                    category="unauthorized",
                    message="tool is not authorized for this session",
                )

            pending = definition.callback(context, decoded)
            if inspect.isawaitable(pending):
                value = await asyncio.wait_for(pending, timeout=definition.timeout_s)
            else:
                value = pending
            output = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            result_bytes = len(output.encode("utf-8"))
            if result_bytes > definition.max_result_bytes:
                return self._error(
                    context,
                    common,
                    started,
                    category="result_too_large",
                    message="tool result exceeds its configured size limit",
                )
            self._emit(
                context,
                "agent.tool.execution_completed",
                **common,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                result_bytes=result_bytes,
            )
            return ToolExecutionResult(
                output=output,
                create_response=definition.create_response,
                ok=True,
                category="completed",
            )
        except asyncio.CancelledError:
            context.cancellation.set()
            self._emit(
                context,
                "agent.tool.execution_cancelled",
                **common,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )
            raise
        except TimeoutError:
            return self._error(
                context,
                common,
                started,
                category="timeout",
                message="tool execution timed out",
            )
        except Exception:
            return self._error(
                context,
                common,
                started,
                category="tool_error",
                message="tool execution failed",
            )

    @staticmethod
    def _emit(context: ToolExecutionContext, kind: str, **data: Any) -> None:
        context.event_sink.emit(
            RuntimeEvent(
                kind=kind,
                source="official_runtime.realtime_tools",
                data={
                    "profile_id": context.profile_id,
                    "visitor_session_id": context.visitor_session_id,
                    **data,
                },
            )
        )

    def _error(
        self,
        context: ToolExecutionContext,
        common: dict[str, Any],
        started: float,
        *,
        category: str,
        message: str,
    ) -> ToolExecutionResult:
        output = json.dumps({"error": {"category": category, "message": message}})
        self._emit(
            context,
            "agent.tool.execution_failed",
            **common,
            category=category,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            result_bytes=len(output.encode("utf-8")),
        )
        return ToolExecutionResult(
            output=output,
            create_response=True,
            ok=False,
            category=category,
        )


def build_reference_tool_registry(reference_store: ReferenceStore) -> ToolRegistry:
    registry = ToolRegistry()

    async def catalog(
        context: ToolExecutionContext, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return await asyncio.to_thread(context.reference_store.catalog)

    registry.register(
        ToolDefinition(
            name="reference_catalog",
            description=(
                "Return the complete catalog of approved on-demand visitor-safe "
                "references. Call with an empty object when the needed information "
                "is not already in the profile context."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            callback=catalog,
        )
    )

    on_demand_ids = reference_store.on_demand_ids()
    if on_demand_ids:

        async def read(
            context: ToolExecutionContext, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            return await asyncio.to_thread(
                context.reference_store.read,
                arguments["reference_id"],
            )

        registry.register(
            ToolDefinition(
                name="reference_read",
                description=(
                    "Read one approved on-demand visitor-safe reference by its "
                    "catalog ID. Use this only when the profile context does not "
                    "already contain the answer."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference_id": {
                            "type": "string",
                            "enum": on_demand_ids,
                            "description": "An exact ID returned by reference_catalog.",
                        }
                    },
                    "required": ["reference_id"],
                    "additionalProperties": False,
                },
                callback=read,
            )
        )
    return registry


SendPayload = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class _PendingCall:
    name: str
    arguments: Any
    output_index: int


@dataclass(slots=True)
class _ToolBatch:
    calls: dict[str, _PendingCall] = field(default_factory=dict)
    cancellation: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class RealtimeToolCoordinator:
    """Execute completed-response tool calls sequentially and in output order."""

    FOLLOW_UP_METADATA_KEY = "reachy_tool_follow_up"

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        context: ToolExecutionContext,
        send: SendPayload,
    ) -> None:
        self._registry = registry
        self._context = replace(context, cancellation=asyncio.Event())
        self._send = send
        self._active_response_id: str | None = None
        self._batches: dict[str, _ToolBatch] = {}
        self._added_indices: dict[tuple[str, str], int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._follow_up_sequence = 0

    def set_visitor_session_id(self, visitor_session_id: str) -> None:
        self._context.visitor_session_id = visitor_session_id

    def handle_event(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        event_type = event.get("type")
        if event_type == "response.created":
            response_id = _response_id(event)
            if response_id:
                self._cancel_other_batches(response_id, reason="new_response")
                self._active_response_id = response_id
            return
        if event_type == "input_audio_buffer.speech_started":
            self._cancel_all(reason="user_speech_started")
            return
        if event_type == "response.output_item.added":
            item = event.get("item")
            response_id = _response_id(event) or self._active_response_id
            output_index = event.get("output_index")
            if (
                isinstance(item, dict)
                and item.get("type") == "function_call"
                and isinstance(response_id, str)
                and isinstance(item.get("call_id"), str)
                and isinstance(output_index, int)
            ):
                self._added_indices[(response_id, item["call_id"])] = output_index
            return
        if event_type == "response.function_call_arguments.done":
            response_id = _response_id(event) or self._active_response_id
            if isinstance(response_id, str) and response_id:
                self._record_call(response_id, event)
            return
        if event_type == "response.done":
            self._handle_response_done(event)

    def _record_call(self, response_id: str, call: dict[str, Any]) -> None:
        call_id = call.get("call_id")
        name = call.get("name")
        if not isinstance(call_id, str) or not call_id:
            return
        if not isinstance(name, str) or not name:
            name = "<unnamed>"
        batch = self._batches.setdefault(response_id, _ToolBatch())
        if call_id in batch.calls:
            return
        raw_index = call.get("output_index")
        output_index = (
            raw_index
            if isinstance(raw_index, int)
            else self._added_indices.get((response_id, call_id), len(batch.calls))
        )
        batch.calls[call_id] = _PendingCall(
            name=name,
            arguments=call.get("arguments"),
            output_index=output_index,
        )
        self._emit(
            "agent.tool.queued",
            response_id=response_id,
            output_index=output_index,
            call_id=call_id,
            tool_name=name,
        )

    def _handle_response_done(self, event: dict[str, Any]) -> None:
        response = event.get("response")
        if not isinstance(response, dict):
            return
        response_id = response.get("id")
        if not isinstance(response_id, str) or not response_id:
            return
        if response_id == self._active_response_id:
            self._active_response_id = None
        status = response.get("status")
        if status != "completed":
            self._cancel_batch(response_id, reason=f"response_{status or 'unknown'}")
            return
        for output_index, item in enumerate(response.get("output") or []):
            if isinstance(item, dict) and item.get("type") == "function_call":
                enriched = dict(item)
                enriched.setdefault("output_index", output_index)
                self._record_call(response_id, enriched)
        batch = self._batches.get(response_id)
        if batch is None or not batch.calls or batch.task is not None:
            return
        batch.task = asyncio.create_task(
            self._process_batch(response_id, batch),
            name=f"realtime-tools-{response_id}",
        )
        self._tasks.add(batch.task)
        batch.task.add_done_callback(self._tasks.discard)

    async def _process_batch(self, response_id: str, batch: _ToolBatch) -> None:
        context = replace(self._context, cancellation=batch.cancellation)
        create_response = False
        try:
            ordered = sorted(
                batch.calls.items(),
                key=lambda item: (item[1].output_index, item[0]),
            )
            for call_id, call in ordered:
                metadata = ToolCallMetadata(
                    response_id=response_id,
                    output_index=call.output_index,
                    call_id=call_id,
                )
                result = await self._registry.execute(
                    name=call.name,
                    arguments=call.arguments,
                    context=context,
                    metadata=metadata,
                )
                if self._closed or batch.cancellation.is_set():
                    return
                await self._send(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": result.output,
                        },
                    }
                )
                self._emit(
                    "agent.tool.result_submitted",
                    **metadata.to_dict(),
                    tool_name=call.name,
                    result_bytes=len(result.output.encode("utf-8")),
                    result_category=result.category,
                )
                create_response = create_response or result.create_response
            if create_response and not self._closed and not batch.cancellation.is_set():
                if self._active_response_id is not None:
                    self._emit(
                        "agent.tool.follow_up_suppressed",
                        response_id=response_id,
                        reason="active_response",
                    )
                    return
                self._follow_up_sequence += 1
                follow_up_id = f"reachy_tool_{self._follow_up_sequence}"
                await self._send(
                    {
                        "event_id": follow_up_id,
                        "type": "response.create",
                        "response": {
                            "metadata": {self.FOLLOW_UP_METADATA_KEY: follow_up_id}
                        },
                    }
                )
                self._emit(
                    "agent.tool.follow_up_requested",
                    response_id=response_id,
                    follow_up_event_id=follow_up_id,
                )
        except asyncio.CancelledError:
            batch.cancellation.set()
            self._emit(
                "agent.tool.batch_cancelled",
                response_id=response_id,
                reason="task_cancelled",
            )
            raise
        except Exception:
            self._emit(
                "agent.tool.coordinator_failed",
                response_id=response_id,
                category="transport_or_coordinator_error",
            )
        finally:
            self._batches.pop(response_id, None)

    def _cancel_other_batches(self, response_id: str, *, reason: str) -> None:
        for existing_id in tuple(self._batches):
            if existing_id != response_id:
                self._cancel_batch(existing_id, reason=reason)

    def _cancel_all(self, *, reason: str) -> None:
        for response_id in tuple(self._batches):
            self._cancel_batch(response_id, reason=reason)

    def _cancel_batch(self, response_id: str, *, reason: str) -> None:
        batch = self._batches.pop(response_id, None)
        if batch is None:
            return
        batch.cancellation.set()
        if batch.task is not None and not batch.task.done():
            batch.task.cancel()
        self._emit(
            "agent.tool.batch_cancelled",
            response_id=response_id,
            reason=reason,
        )

    async def close(self, *, reason: str = "connection_closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_all(reason=reason)
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _emit(self, kind: str, **data: Any) -> None:
        self._context.event_sink.emit(
            RuntimeEvent(
                kind=kind,
                source="official_runtime.realtime_tools",
                data={
                    "profile_id": self._context.profile_id,
                    "visitor_session_id": self._context.visitor_session_id,
                    **data,
                },
            )
        )


def _response_id(event: dict[str, Any]) -> str:
    response_id = event.get("response_id")
    if isinstance(response_id, str):
        return response_id
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        return response["id"]
    return ""
