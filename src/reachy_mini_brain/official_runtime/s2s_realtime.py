"""Native OpenAI-compatible realtime handler for the local S2S backend."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .events import EventSink, InMemoryEventSink, RuntimeEvent
from .realtime_tools import (
    RealtimeToolCoordinator,
    ToolExecutionContext,
    ToolRegistry,
)
from .stream_runtime import AudioFrame, HandlerOutput


ConnectFactory = Callable[[str], Awaitable[Any]]
SleepFactory = Callable[[float], Awaitable[None]]


class S2SRealtimeHandler:
    """Small official-style handler that talks directly to the S2S websocket.

    The local speech-to-speech backend implements the same OpenAI-compatible
    realtime event shape the official app handler used. This class preserves the
    runtime contract and event names while removing the source checkout import.
    """

    SAMPLE_RATE = 16_000

    def __init__(
        self,
        *,
        realtime_ws_url: str,
        instructions: str,
        event_sink: EventSink | None = None,
        voice: str = "Sohee",
        input_sample_rate: int = SAMPLE_RATE,
        startup_timeout_s: float = 20.0,
        transcription_model: str = "gpt-4o-transcribe",
        transcription_language: str = "en",
        connect_factory: ConnectFactory | None = None,
        instructions_source: str | None = None,
        instructions_sha256: str | None = None,
        reconnect_settle_s: float = 0.2,
        sleep_factory: SleepFactory = asyncio.sleep,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolExecutionContext | None = None,
    ) -> None:
        if (tool_registry is None) != (tool_context is None):
            raise ValueError("tool_registry and tool_context must be configured together")
        self.realtime_ws_url = realtime_ws_url
        self.instructions = instructions
        self.instructions_source = instructions_source
        self.instructions_sha256 = instructions_sha256 or _sha256_text(instructions)
        self.event_sink = event_sink or InMemoryEventSink()
        self.voice = voice
        self.input_sample_rate = input_sample_rate
        self.startup_timeout_s = startup_timeout_s
        self.transcription_model = transcription_model
        self.transcription_language = transcription_language
        self.connect_factory = connect_factory
        self.reconnect_settle_s = max(0.0, reconnect_settle_s)
        self.sleep_factory = sleep_factory
        self.tool_registry = tool_registry
        self.tool_context = tool_context
        self._outputs: asyncio.Queue[HandlerOutput] = asyncio.Queue()
        self._connected_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._session_task: asyncio.Task[None] | None = None
        self._connection: Any | None = None
        self._first_audio_by_response: set[str] = set()
        self._connection_generation = 0
        self._conversation_generation = 0
        self._session_has_activity = False
        self._tool_coordinator: RealtimeToolCoordinator | None = None
        self._started = False

    async def start_up(self) -> None:
        async with self._lifecycle_lock:
            if self._connection is not None:
                self._started = True
                return
            await self._open_connection()
            self._started = True

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            await self._close_connection(clear_outputs=True)
            self._conversation_generation = 0
            self._started = False

    async def begin_conversation_session(self) -> dict[str, Any]:
        """Start an isolated visitor conversation on this long-lived handler."""

        async with self._lifecycle_lock:
            if not self._started:
                raise RuntimeError("S2S realtime websocket is not connected.")
            reconnected = (
                self._connection is None
                or self._conversation_generation > 0
                or self._session_has_activity
            )
            if self._connection is None:
                await self._open_connection()
            elif reconnected:
                await self._close_connection(clear_outputs=True)
                if self.reconnect_settle_s:
                    await self.sleep_factory(self.reconnect_settle_s)
                await self._open_connection()
            self._conversation_generation += 1
            if self._tool_coordinator is not None:
                self._tool_coordinator.set_visitor_session_id(
                    f"visitor-{self._conversation_generation}"
                )
            result = {
                "conversation_generation": self._conversation_generation,
                "connection_generation": self._connection_generation,
                "reconnected": reconnected,
            }
            self._emit("hf.session.conversation_started", **result)
            return result

    async def _open_connection(self) -> None:
        connection = await self._connect()
        self._connection = connection
        self._connected_event.clear()
        if self.tool_registry is not None and self.tool_context is not None:

            async def send_tool_payload(payload: dict[str, Any]) -> None:
                async with self._send_lock:
                    if self._connection is not connection:
                        raise RuntimeError(
                            "refusing to send a tool result across connections"
                        )
                    await connection.send(json.dumps(payload))
                    self._session_has_activity = True

            self._tool_coordinator = RealtimeToolCoordinator(
                registry=self.tool_registry,
                context=self.tool_context,
                send=send_tool_payload,
            )
        self._session_task = asyncio.create_task(self._receive_loop(), name="s2s-realtime-session")
        try:
            async with self._send_lock:
                await connection.send(json.dumps({"type": "session.update", "session": self._session_config()}))
            try:
                await asyncio.wait_for(self._connected_event.wait(), timeout=self.startup_timeout_s)
            except asyncio.TimeoutError as exc:
                if self._session_task.done():
                    await self._session_task
                raise RuntimeError("Timed out waiting for S2S realtime session to connect.") from exc
        except Exception:
            await self._close_connection(clear_outputs=False)
            raise
        self._connection_generation += 1
        self._session_has_activity = False
        self._record_session_snapshot()

    async def _close_connection(self, *, clear_outputs: bool) -> None:
        tool_coordinator = self._tool_coordinator
        self._tool_coordinator = None
        if tool_coordinator is not None:
            await tool_coordinator.close()
        session_task = self._session_task
        self._session_task = None
        connection = self._connection
        self._connection = None
        if connection is not None:
            close = getattr(connection, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        if session_task is not None:
            if not session_task.done():
                session_task.cancel()
            try:
                await session_task
            except asyncio.CancelledError:
                pass
        self._connected_event.clear()
        self._first_audio_by_response.clear()
        if clear_outputs:
            self._clear_outputs()

    def _clear_outputs(self) -> None:
        while not self._outputs.empty():
            try:
                self._outputs.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def receive(self, frame: AudioFrame) -> None:
        sample_rate, audio = frame
        audio_i16 = _as_int16_mono(audio)
        if sample_rate != self.input_sample_rate:
            audio_i16 = _resample_int16(audio_i16, sample_rate, self.input_sample_rate)
        if audio_i16.size == 0:
            return
        encoded = base64.b64encode(audio_i16.astype("<i2", copy=False).tobytes()).decode("ascii")
        await self._send({"type": "input_audio_buffer.append", "audio": encoded})

    async def emit(self) -> HandlerOutput:
        return await self._outputs.get()

    async def request_text_response(self, prompt: str) -> bool:
        if self._connection is None:
            return False
        await self._send_many(
            [
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    },
                },
                {"type": "response.create"},
            ]
        )
        return True

    async def request_speech(
        self,
        text: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> bool:
        """Synthesize exact text without creating an LLM turn."""
        if self._connection is None:
            return False
        payload: dict[str, Any] = {"type": "tts.create", "text": text}
        if metadata:
            payload["metadata"] = dict(metadata)
        # Deterministic policy speech has priority over an in-flight conversational
        # response. A no-op cancel is valid when the backend is already idle.
        await self._send_many([{"type": "response.cancel"}, payload])
        self._emit("hf.realtime.tts.requested", text=text, metadata=metadata or {})
        return True

    def copy(self) -> "S2SRealtimeHandler":
        return type(self)(
            realtime_ws_url=self.realtime_ws_url,
            instructions=self.instructions,
            event_sink=self.event_sink,
            voice=self.voice,
            input_sample_rate=self.input_sample_rate,
            startup_timeout_s=self.startup_timeout_s,
            transcription_model=self.transcription_model,
            transcription_language=self.transcription_language,
            connect_factory=self.connect_factory,
            instructions_source=self.instructions_source,
            instructions_sha256=self.instructions_sha256,
            reconnect_settle_s=self.reconnect_settle_s,
            sleep_factory=self.sleep_factory,
            tool_registry=self.tool_registry,
            tool_context=self.tool_context,
        )

    async def _connect(self) -> Any:
        if self.connect_factory is not None:
            return await self.connect_factory(self.realtime_ws_url)
        import websockets

        return await websockets.connect(self.realtime_ws_url, max_size=None)

    async def _send(self, payload: dict[str, Any]) -> None:
        await self._send_many([payload])

    async def _send_many(self, payloads: list[dict[str, Any]]) -> None:
        async with self._lifecycle_lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("S2S realtime websocket is not connected.")
            async with self._send_lock:
                for payload in payloads:
                    await connection.send(json.dumps(payload))
                    self._session_has_activity = True

    async def _receive_loop(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            async for raw in connection:
                event = _loads_event(raw)
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._emit("hf.realtime.error", error=repr(exc), event_type="handler.receive_loop_error")

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "session.created":
            self._connected_event.set()
        self._emit_realtime_event(event)
        if self._tool_coordinator is not None:
            self._tool_coordinator.handle_event(event)

        if event_type == "conversation.item.input_audio_transcription.completed":
            transcript = _event_text(event)
            if transcript:
                await self._outputs.put(
                    {
                        "role": "user",
                        "transcript": transcript,
                        "text": transcript,
                        "final": True,
                        "event_type": event_type,
                        "item_id": event.get("item_id"),
                    }
                )
            return

        if event_type == "conversation.item.input_audio_transcription.delta":
            transcript = _event_text(event)
            if transcript:
                await self._outputs.put(
                    {
                        "role": "user_partial",
                        "transcript": transcript,
                        "text": transcript,
                        "final": False,
                        "event_type": event_type,
                        "item_id": event.get("item_id"),
                    }
                )
            return

        if event_type == "response.output_audio.delta":
            frame = _decode_audio_delta(event)
            if frame is not None:
                response_id = _response_id(event)
                metadata = {
                    "event_type": event_type,
                    "response_id": response_id,
                    "sample_rate": self.SAMPLE_RATE,
                    "bytes": int(frame.nbytes),
                }
                if response_id and response_id not in self._first_audio_by_response:
                    self._first_audio_by_response.add(response_id)
                    self._record_response_metadata(response_id, {"phase": "first_audio_delta"})
                await self._outputs.put((self.SAMPLE_RATE, frame, metadata))
            return

        if event_type in {
            "response.created",
            "response.done",
            "response.output_audio.done",
            "response.output_audio_transcript.done",
        }:
            response_id = _response_id(event)
            if response_id:
                self._record_response_metadata(response_id, _response_metadata(event))

    def _session_config(self) -> dict[str, Any]:
        tools = self.tool_registry.schemas() if self.tool_registry is not None else []
        return {
            "type": "realtime",
            "instructions": self.instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": None},
                    "transcription": {
                        "model": self.transcription_model,
                        "language": self.transcription_language,
                    },
                    "turn_detection": {"type": "server_vad", "interrupt_response": True},
                },
                "output": {"format": {"type": "audio/pcm", "rate": None}, "voice": self.voice},
            },
            "tools": tools,
            "tool_choice": "auto",
        }

    def _record_session_snapshot(self) -> None:
        self.event_sink.emit(
            RuntimeEvent(
                kind="hf.session.snapshot",
                source="official_runtime.s2s_realtime",
                data={
                    "backend_provider": "s2s-local",
                    "realtime_ws_url": self.realtime_ws_url,
                    "voice": self.voice,
                    "sample_rate": self.SAMPLE_RATE,
                    "input_sample_rate": self.input_sample_rate,
                    "instructions_source": self.instructions_source,
                    "instructions_sha256": self.instructions_sha256,
                    "instructions_chars": len(self.instructions),
                    "transcription_model": self.transcription_model,
                    "transcription_language": self.transcription_language,
                    "connection_generation": self._connection_generation,
                    "profile_id": (
                        self.tool_context.profile_id
                        if self.tool_context is not None
                        else None
                    ),
                    "tool_names": (
                        self.tool_registry.names()
                        if self.tool_registry is not None
                        else []
                    ),
                },
            )
        )

    def _record_response_metadata(self, response_id: str, metadata: dict[str, Any]) -> None:
        self.event_sink.emit(
            RuntimeEvent(
                kind="hf.response.metadata",
                source="official_runtime.s2s_realtime",
                data={"response_id": response_id, "metadata": metadata},
            )
        )

    def _emit_realtime_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "unknown")
        data = _summarize_event(event)
        self._emit("hf.realtime.event", **data)
        self._emit(f"hf.realtime.{event_type}", **data)

    def _emit(self, kind: str, **data: Any) -> None:
        self.event_sink.emit(RuntimeEvent(kind=kind, source="official_runtime.s2s_realtime", data=data))


def _loads_event(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"type": "unknown", "value": value}
    return raw if isinstance(raw, dict) else {"type": "unknown", "value": repr(raw)}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _summarize_event(event: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {"event_type": event.get("type")}
    for key in (
        "event_id",
        "response_id",
        "item_id",
        "output_index",
        "content_index",
        "call_id",
        "name",
    ):
        if key in event:
            data[key] = event[key]
    session = event.get("session")
    if isinstance(session, dict) and isinstance(session.get("id"), str):
        data["backend_session_id"] = session["id"]
    response = event.get("response")
    if isinstance(response, dict):
        data["response_id"] = data.get("response_id") or response.get("id")
        data["response_status"] = response.get("status")
        output_summary = _response_output_summary(response)
        if output_summary:
            data["output_summary"] = output_summary
    text = _event_text(event)
    if text:
        data["transcript"] = text
        data["text"] = text
        data["final"] = event.get("type") == "conversation.item.input_audio_transcription.completed"
    delta = event.get("delta")
    if isinstance(delta, str):
        data["delta_bytes"] = len(delta)
    error = event.get("error")
    if error is not None:
        data["error"] = error
    return data


def _response_id(event: dict[str, Any]) -> str:
    response_id = event.get("response_id")
    if isinstance(response_id, str):
        return response_id
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        return response["id"]
    return ""


def _response_metadata(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    metadata = {"event_type": event_type}
    response = event.get("response")
    if isinstance(response, dict):
        metadata["status"] = response.get("status")
        if isinstance(response.get("metadata"), dict):
            metadata["request_metadata"] = response["metadata"]
        output_summary = _response_output_summary(response)
        if output_summary:
            metadata["output_summary"] = output_summary
        usage = response.get("usage")
        if isinstance(usage, dict):
            metadata["usage"] = _numeric_mapping(usage)
        status_details = response.get("status_details")
        if isinstance(status_details, dict):
            metadata["status_details"] = {
                key: status_details[key]
                for key in ("type", "reason", "code")
                if isinstance(status_details.get(key), (str, int, float, bool))
            }
    text = _event_text(event)
    if text:
        metadata["text"] = text
        metadata["transcript"] = text
    return metadata


def _response_output_summary(response: dict[str, Any]) -> list[dict[str, Any]]:
    summary = []
    for output_index, item in enumerate(response.get("output") or []):
        if not isinstance(item, dict):
            continue
        row = {"output_index": output_index, "type": item.get("type")}
        for key in ("id", "call_id", "name", "status"):
            if isinstance(item.get(key), (str, int, float, bool)):
                row[key] = item[key]
        summary.append(row)
    return summary


def _numeric_mapping(value: dict[str, Any], *, depth: int = 0) -> dict[str, Any]:
    if depth >= 4:
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, bool) or isinstance(item, (int, float)):
            result[str(key)] = item
        elif isinstance(item, dict):
            nested = _numeric_mapping(item, depth=depth + 1)
            if nested:
                result[str(key)] = nested
    return result


def _event_text(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    if event_type in {
        "conversation.item.input_audio_transcription.delta",
        "response.output_audio_transcript.delta",
    }:
        value = event.get("delta")
        if isinstance(value, str):
            return value.strip()
    for key in ("transcript", "text", "delta"):
        value = event.get(key)
        if isinstance(value, str) and key != "delta":
            return value.strip()
    response = event.get("response")
    if isinstance(response, dict):
        value = response.get("output_text") or response.get("text")
        if isinstance(value, str):
            return value.strip()
    if event.get("type") == "response.output_audio_transcript.done":
        value = event.get("transcript")
        if isinstance(value, str):
            return value.strip()
    return ""


def _decode_audio_delta(event: dict[str, Any]) -> NDArray[np.int16] | None:
    delta = event.get("delta")
    if not isinstance(delta, str) or not delta:
        return None
    try:
        audio_bytes = base64.b64decode(delta)
    except Exception:
        return None
    if not audio_bytes:
        return None
    return np.frombuffer(audio_bytes, dtype="<i2").astype(np.int16, copy=True)


def _as_int16_mono(audio: NDArray[Any]) -> NDArray[np.int16]:
    arr = np.asarray(audio)
    if arr.ndim == 2:
        if arr.shape[1] > arr.shape[0]:
            arr = arr.T
        arr = arr[:, 0]
    if arr.dtype == np.int16:
        return np.ascontiguousarray(arr.reshape(-1), dtype=np.int16)
    if np.issubdtype(arr.dtype, np.floating):
        arr = np.clip(arr, -1.0, 1.0) * 32767.0
    else:
        arr = np.clip(arr, -32768, 32767)
    return np.ascontiguousarray(arr.reshape(-1), dtype=np.int16)


def _resample_int16(audio: NDArray[np.int16], input_sample_rate: int, output_sample_rate: int) -> NDArray[np.int16]:
    if input_sample_rate <= 0:
        input_sample_rate = output_sample_rate
    if input_sample_rate == output_sample_rate:
        return audio
    num_samples = int(len(audio) * output_sample_rate / input_sample_rate)
    if num_samples <= 0:
        return np.empty(0, dtype=np.int16)
    from scipy.signal import resample

    resampled = resample(audio.astype(np.float32), num_samples)
    return np.ascontiguousarray(np.clip(resampled, -32768, 32767).astype(np.int16))
