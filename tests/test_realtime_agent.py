import asyncio
import json
from pathlib import Path

import pytest

from reachy_mini_brain.official_runtime import (
    AgentProfileError,
    InMemoryEventSink,
    S2SRealtimeHandler,
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    build_reference_tool_registry,
    compose_agent_profile,
)
from reachy_mini_brain.official_runtime.realtime_tools import ToolCallMetadata


def _write_profile(
    root: Path,
    *,
    personality: str = "Be calm.",
    prompt_fact: str = "The clinic opens at nine.",
    include_on_demand: bool = True,
) -> None:
    root.mkdir(parents=True)
    (root / "instructions.txt").write_text("Help clinic visitors.\n", encoding="utf-8")
    (root / "personality.md").write_text(personality + "\n", encoding="utf-8")
    (root / "session_instructions.txt").write_text(
        "Use short spoken answers.\n", encoding="utf-8"
    )
    (root / "facts.md").write_text(prompt_fact + "\n", encoding="utf-8")
    references = {
        "clinic.facts": {
            "path": "facts.md",
            "title": "Clinic facts",
            "summary": "Common clinic facts.",
            "delivery": "prompt",
            "tags": ["clinic"],
            "audience": "visitor",
            "max_bytes": 4096,
        }
    }
    if include_on_demand:
        (root / "parking.md").write_text(
            "Parking is behind the building.\n", encoding="utf-8"
        )
        references["clinic.parking"] = {
            "path": "parking.md",
            "title": "Parking details",
            "summary": "Detailed parking directions.",
            "delivery": "on_demand",
            "tags": ["parking", "directions"],
            "audience": "visitor",
            "max_bytes": 4096,
        }
    import yaml

    (root / "reference_catalog.yaml").write_text(
        yaml.safe_dump({"version": 1, "references": references}, sort_keys=False),
        encoding="utf-8",
    )


def test_agent_profile_composes_private_overrides_without_path_provenance(tmp_path):
    public = tmp_path / "public"
    private = tmp_path / "private-secret-location"
    _write_profile(public, personality="Public personality.", prompt_fact="Public fact.")
    _write_profile(private, personality="Private personality.", prompt_fact="Private fact.")

    profile = compose_agent_profile(
        profile_id="clinic-test",
        public_dir=public,
        private_dir=private,
    )

    assert "Help clinic visitors." in profile.instructions
    assert "Private personality." in profile.instructions
    assert "Private fact." in profile.instructions
    assert "Public personality." not in profile.instructions
    assert "Public fact." not in profile.instructions
    assert profile.reference_store.read("clinic.parking")["content"].startswith(
        "Parking is behind"
    )
    provenance = profile.provenance()
    assert provenance["profile_id"] == "clinic-test"
    assert provenance["profile_source_ids"] == [
        "private:instructions.txt",
        "private:personality.md",
        "private:session_instructions.txt",
        "private:reference_catalog.yaml",
        "private:facts.md",
    ]
    assert str(private) not in json.dumps(provenance)
    assert "Private fact." not in json.dumps(provenance)


def test_agent_profile_rejects_reference_path_escape(tmp_path):
    public = tmp_path / "public"
    _write_profile(public)
    outside = tmp_path / "outside.md"
    outside.write_text("private\n", encoding="utf-8")
    catalog_path = public / "reference_catalog.yaml"
    catalog_path.write_text(
        """version: 1
references:
  clinic.escape:
    path: ../outside.md
    title: Escaped
    summary: Must not load.
    delivery: on_demand
    tags: [test]
    audience: visitor
    max_bytes: 4096
""",
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileError, match="outside its profile root"):
        compose_agent_profile(profile_id="clinic-test", public_dir=public)


def test_reference_tools_expose_only_catalog_ids(tmp_path):
    public = tmp_path / "public"
    _write_profile(public)
    profile = compose_agent_profile(profile_id="clinic-test", public_dir=public)
    registry = build_reference_tool_registry(profile.reference_store)

    schemas = registry.schemas()
    assert registry.names() == ["reference_catalog", "reference_read"]
    catalog_schema = next(
        schema for schema in schemas if schema["name"] == "reference_catalog"
    )
    assert catalog_schema["parameters"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    read_schema = next(schema for schema in schemas if schema["name"] == "reference_read")
    assert read_schema["parameters"]["properties"]["reference_id"]["enum"] == [
        "clinic.parking"
    ]
    assert "path" not in json.dumps(schemas)


def test_tool_registry_bounds_validation_timeout_errors_and_results(tmp_path):
    async def run():
        public = tmp_path / "public"
        _write_profile(public, include_on_demand=False)
        profile = compose_agent_profile(profile_id="clinic-test", public_dir=public)
        events = InMemoryEventSink()
        context = ToolExecutionContext(
            profile_id=profile.profile_id,
            visitor_session_id="visitor-1",
            reference_store=profile.reference_store,
            event_sink=events,
        )
        registry = ToolRegistry()

        async def execute(_context, arguments):
            mode = arguments["mode"]
            if mode == "timeout":
                await asyncio.sleep(1)
            if mode == "error":
                raise RuntimeError("private exception detail")
            if mode == "large":
                return "x" * 100
            return {"ok": True}

        registry.register(
            ToolDefinition(
                name="bounded",
                description="Bounded test tool.",
                parameters={
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["ok", "timeout", "error", "large"],
                        }
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
                callback=execute,
                timeout_s=0.01,
                max_result_bytes=32,
            )
        )
        metadata = ToolCallMetadata("resp-1", 0, "call-1")
        unknown = await registry.execute(
            name="missing",
            arguments="{}",
            context=context,
            metadata=metadata,
        )
        invalid = await registry.execute(
            name="bounded",
            arguments='{"mode":"invalid"}',
            context=context,
            metadata=metadata,
        )
        timeout = await registry.execute(
            name="bounded",
            arguments='{"mode":"timeout"}',
            context=context,
            metadata=metadata,
        )
        error = await registry.execute(
            name="bounded",
            arguments='{"mode":"error"}',
            context=context,
            metadata=metadata,
        )
        large = await registry.execute(
            name="bounded",
            arguments='{"mode":"large"}',
            context=context,
            metadata=metadata,
        )
        return unknown, invalid, timeout, error, large, events

    unknown, invalid, timeout, error, large, events = asyncio.run(run())

    assert [result.category for result in (unknown, invalid, timeout, error, large)] == [
        "unknown_tool",
        "invalid_arguments",
        "timeout",
        "tool_error",
        "result_too_large",
    ]
    serialized_events = json.dumps([event.data for event in events.events])
    assert "private exception detail" not in serialized_events


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
        item = await self.incoming.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        return json.dumps(item)

    async def close(self):
        self.closed = True
        await self.incoming.put(StopAsyncIteration)


async def _wait_until(predicate, *, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def test_s2s_tool_calls_execute_sequentially_in_output_order(tmp_path):
    async def run():
        public = tmp_path / "public"
        _write_profile(public, include_on_demand=False)
        profile = compose_agent_profile(profile_id="clinic-test", public_dir=public)
        events = InMemoryEventSink()
        registry = ToolRegistry()
        order = []

        async def ordered(_context, arguments):
            order.append(arguments["value"])
            return {"value": arguments["value"]}

        registry.register(
            ToolDefinition(
                name="ordered",
                description="Record execution order.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                callback=ordered,
            )
        )
        context = ToolExecutionContext(
            profile_id=profile.profile_id,
            visitor_session_id="pre-session",
            reference_store=profile.reference_store,
            event_sink=events,
        )
        websocket = _FakeWebSocket()

        async def connect_factory(_url):
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions=profile.instructions,
            event_sink=events,
            startup_timeout_s=1,
            connect_factory=connect_factory,
            tool_registry=registry,
            tool_context=context,
        )
        await websocket.incoming.put({"type": "session.created"})
        await handler.start_up()
        await handler.begin_conversation_session()
        await websocket.incoming.put(
            {"type": "response.created", "response": {"id": "resp-1"}}
        )
        await websocket.incoming.put(
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "output_index": 1,
                "item": {"type": "function_call", "call_id": "call-b"},
            }
        )
        await websocket.incoming.put(
            {
                "type": "response.function_call_arguments.done",
                "response_id": "resp-1",
                "call_id": "call-b",
                "name": "ordered",
                "arguments": '{"value":"b"}',
            }
        )
        await websocket.incoming.put(
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "output_index": 0,
                "item": {"type": "function_call", "call_id": "call-a"},
            }
        )
        await websocket.incoming.put(
            {
                "type": "response.function_call_arguments.done",
                "response_id": "resp-1",
                "call_id": "call-a",
                "name": "ordered",
                "arguments": '{"value":"a"}',
            }
        )
        await websocket.incoming.put(
            {
                "type": "response.done",
                "response": {
                    "id": "resp-1",
                    "status": "completed",
                    "output": [],
                },
            }
        )
        await _wait_until(
            lambda: any(item["type"] == "response.create" for item in websocket.sent[1:])
        )
        await handler.shutdown()
        return order, websocket.sent, events

    order, sent, events = asyncio.run(run())

    assert order == ["a", "b"]
    assert sent[0]["session"]["tools"][0]["name"] == "ordered"
    outputs = [
        item
        for item in sent
        if item["type"] == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]
    assert [item["item"]["call_id"] for item in outputs] == ["call-a", "call-b"]
    assert sent[-1]["type"] == "response.create"
    assert events.kinds().count("agent.tool.follow_up_requested") == 1


def test_s2s_tool_result_is_cancelled_on_new_user_speech(tmp_path):
    async def run():
        public = tmp_path / "public"
        _write_profile(public, include_on_demand=False)
        profile = compose_agent_profile(profile_id="clinic-test", public_dir=public)
        events = InMemoryEventSink()
        registry = ToolRegistry()
        started = asyncio.Event()

        async def blocked(_context, _arguments):
            started.set()
            await asyncio.Event().wait()

        registry.register(
            ToolDefinition(
                name="blocked",
                description="Wait until cancelled.",
                parameters={"type": "object", "additionalProperties": False},
                callback=blocked,
            )
        )
        context = ToolExecutionContext(
            profile_id=profile.profile_id,
            visitor_session_id="visitor-1",
            reference_store=profile.reference_store,
            event_sink=events,
        )
        websocket = _FakeWebSocket()

        async def connect_factory(_url):
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions=profile.instructions,
            event_sink=events,
            startup_timeout_s=1,
            connect_factory=connect_factory,
            tool_registry=registry,
            tool_context=context,
        )
        await websocket.incoming.put({"type": "session.created"})
        await handler.start_up()
        await websocket.incoming.put(
            {"type": "response.created", "response": {"id": "resp-old"}}
        )
        await websocket.incoming.put(
            {
                "type": "response.function_call_arguments.done",
                "response_id": "resp-old",
                "output_index": 0,
                "call_id": "call-old",
                "name": "blocked",
                "arguments": "{}",
            }
        )
        await websocket.incoming.put(
            {
                "type": "response.done",
                "response": {"id": "resp-old", "status": "completed", "output": []},
            }
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await websocket.incoming.put({"type": "input_audio_buffer.speech_started"})
        await _wait_until(
            lambda: "agent.tool.execution_cancelled" in events.kinds()
        )
        await handler.shutdown()
        return websocket.sent, events

    sent, events = asyncio.run(run())

    assert not any(
        item.get("item", {}).get("type") == "function_call_output" for item in sent
    )
    assert "agent.tool.batch_cancelled" in events.kinds()


def test_s2s_tool_result_cannot_cross_reconnected_session(tmp_path):
    async def run():
        public = tmp_path / "public"
        _write_profile(public, include_on_demand=False)
        profile = compose_agent_profile(profile_id="clinic-test", public_dir=public)
        events = InMemoryEventSink()
        registry = ToolRegistry()
        started = asyncio.Event()

        async def blocked(_context, _arguments):
            started.set()
            await asyncio.Event().wait()

        registry.register(
            ToolDefinition(
                name="blocked",
                description="Wait until the connection is replaced.",
                parameters={"type": "object", "additionalProperties": False},
                callback=blocked,
            )
        )
        context = ToolExecutionContext(
            profile_id=profile.profile_id,
            visitor_session_id="pre-session",
            reference_store=profile.reference_store,
            event_sink=events,
        )
        websockets = [_FakeWebSocket(), _FakeWebSocket()]
        connected = []

        async def connect_factory(_url):
            websocket = websockets.pop(0)
            connected.append(websocket)
            await websocket.incoming.put({"type": "session.created"})
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions=profile.instructions,
            event_sink=events,
            startup_timeout_s=1,
            connect_factory=connect_factory,
            reconnect_settle_s=0,
            tool_registry=registry,
            tool_context=context,
        )
        await handler.start_up()
        await handler.begin_conversation_session()
        first = connected[0]
        await first.incoming.put(
            {"type": "response.created", "response": {"id": "resp-old"}}
        )
        await first.incoming.put(
            {
                "type": "response.function_call_arguments.done",
                "response_id": "resp-old",
                "output_index": 0,
                "call_id": "call-old",
                "name": "blocked",
                "arguments": "{}",
            }
        )
        await first.incoming.put(
            {
                "type": "response.done",
                "response": {"id": "resp-old", "status": "completed", "output": []},
            }
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await handler.begin_conversation_session()
        second = connected[1]
        await asyncio.sleep(0)
        await handler.shutdown()
        return first.sent, second.sent, events

    first_sent, second_sent, events = asyncio.run(run())

    for sent in (first_sent, second_sent):
        assert not any(
            item.get("item", {}).get("type") == "function_call_output"
            for item in sent
        )
    cancelled = [
        event
        for event in events.events
        if event.kind == "agent.tool.batch_cancelled"
    ]
    assert cancelled
    assert cancelled[-1].data["visitor_session_id"] == "visitor-1"


def test_upstream_realtime_events_remain_available_to_artifacts():
    async def run():
        events = InMemoryEventSink()
        websocket = _FakeWebSocket()

        async def connect_factory(_url):
            return websocket

        handler = S2SRealtimeHandler(
            realtime_ws_url="ws://127.0.0.1:8765/v1/realtime",
            instructions="Test.",
            event_sink=events,
            startup_timeout_s=1,
            connect_factory=connect_factory,
        )
        await websocket.incoming.put({"type": "session.created"})
        await handler.start_up()
        await websocket.incoming.put(
            {
                "type": "response.output_audio_transcript.delta",
                "response_id": "resp-1",
                "item_id": "item-1",
                "delta": "Hello",
            }
        )
        await websocket.incoming.put(
            {
                "type": "response.output_item.added",
                "response_id": "resp-1",
                "output_index": 0,
                "item": {"type": "message", "id": "item-1"},
            }
        )
        await websocket.incoming.put(
            {
                "type": "response.done",
                "response": {
                    "id": "resp-1",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "item-tool",
                            "call_id": "call-secret",
                            "name": "reference_read",
                            "arguments": '{"reference_id":"private.value"}',
                        }
                    ],
                    "usage": {"input_tokens": 12, "output_tokens": 4},
                },
            }
        )
        await _wait_until(
            lambda: "hf.realtime.response.output_item.added" in events.kinds()
        )
        await handler.shutdown()
        return events

    events = asyncio.run(run())

    transcript = next(
        event
        for event in events.events
        if event.kind == "hf.realtime.response.output_audio_transcript.delta"
    )
    assert transcript.data["transcript"] == "Hello"
    assert "hf.realtime.response.output_item.added" in events.kinds()
    metadata = [
        event.data["metadata"]
        for event in events.events
        if event.kind == "hf.response.metadata"
    ][-1]
    assert metadata["usage"] == {"input_tokens": 12, "output_tokens": 4}
    assert metadata["output_summary"] == [
        {
            "output_index": 0,
            "type": "function_call",
            "id": "item-tool",
            "call_id": "call-secret",
            "name": "reference_read",
        }
    ]
    assert "private.value" not in json.dumps(metadata)
