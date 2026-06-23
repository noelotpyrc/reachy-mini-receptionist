"""LiveKit room bridge for the isolated official-style runtime."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import numpy as np

from .events import EventSink, InMemoryEventSink, RuntimeEvent
from .livekit_handler import LiveKitBackendConfig
from .stream_runtime import AudioFrame, HandlerOutput
from .wav_replay import _as_int16_mono


class LiveKitRoomBridge:
    """Bridge official-style audio frames to a LiveKit room.

    This bridge is intentionally only the media/client side. A LiveKit agent must
    already be running and subscribed to the same room.
    """

    def __init__(
        self,
        config: LiveKitBackendConfig,
        *,
        event_sink: EventSink | None = None,
    ) -> None:
        self.config = config
        self.event_sink = event_sink or InMemoryEventSink()
        self._rtc: Any | None = None
        self._room: Any | None = None
        self._source: Any | None = None
        self._track: Any | None = None
        self._audio_tasks: list[asyncio.Task[None]] = []
        self._outputs: asyncio.Queue[HandlerOutput] = asyncio.Queue()
        self._started = False

    async def start(self) -> None:
        rtc, api = _import_livekit()
        self._rtc = rtc
        token = _resolve_token(self.config, api)
        self._validate_config(token)

        self._room = rtc.Room()
        self._install_room_handlers(self._room, rtc)
        self._emit("livekit.bridge.connecting", url=_redact(self.config.url), room_name=self.config.room_name)
        await self._room.connect(
            self.config.url,
            token,
            rtc.RoomOptions(auto_subscribe=True, connect_timeout=self.config.connect_timeout_s),
        )
        if self.config.dispatch_agent:
            await self._create_agent_dispatch(api)
            await self._wait_for_agent()

        self._source = rtc.AudioSource(
            self.config.input_sample_rate,
            1,
            queue_size_ms=self.config.input_queue_size_ms,
        )
        self._track = rtc.LocalAudioTrack.create_audio_track(self.config.track_name, self._source)
        publish_options = rtc.TrackPublishOptions()
        publish_options.source = 2  # LiveKit proto TrackSource.SOURCE_MICROPHONE.
        publication = await self._room.local_participant.publish_track(self._track, publish_options)
        self._started = True
        self._emit(
            "livekit.bridge.started",
            room_name=getattr(self._room, "name", self.config.room_name),
            local_identity=self.config.participant_name,
            track_sid=getattr(publication, "sid", ""),
            track_source="SOURCE_MICROPHONE",
        )

    async def _create_agent_dispatch(self, api: Any) -> None:
        if not (self.config.api_key and self.config.api_secret):
            raise RuntimeError("Agent dispatch requires LIVEKIT_API_KEY and LIVEKIT_API_SECRET.")
        async with api.LiveKitAPI(
            url=self.config.url,
            api_key=self.config.api_key,
            api_secret=self.config.api_secret,
        ) as lkapi:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    room=self.config.room_name,
                    agent_name=self.config.agent_name,
                )
            )
        self._emit(
            "livekit.agent.dispatch_created",
            room_name=self.config.room_name,
            agent_name=self.config.agent_name,
            dispatch_id=getattr(dispatch, "id", ""),
        )

    async def _wait_for_agent(self) -> None:
        if self._room is None:
            raise RuntimeError("LiveKit room is not connected")
        try:
            from livekit.agents.utils.participant import wait_for_agent
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "LiveKit agent utilities are not installed. Install with: "
                ".venv/bin/python -m pip install -e '.[livekit]'"
            ) from exc
        participant = await asyncio.wait_for(
            wait_for_agent(self._room, agent_name=self.config.agent_name),
            timeout=self.config.agent_ready_timeout_s,
        )
        self._emit(
            "livekit.agent.ready",
            agent_name=self.config.agent_name,
            participant_identity=getattr(participant, "identity", ""),
        )

    async def stop(self) -> None:
        for task in self._audio_tasks:
            task.cancel()
        for task in self._audio_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._audio_tasks.clear()

        if self._source is not None and hasattr(self._source, "aclose"):
            await self._source.aclose()
        if self._room is not None:
            await self._room.disconnect()
        self._started = False
        self._emit("livekit.bridge.stopped")

    async def send_audio(self, frame: AudioFrame) -> None:
        self._ensure_started()
        rtc = self._rtc
        if rtc is None or self._source is None:
            raise RuntimeError("LiveKit bridge is not initialized")
        sample_rate, audio = frame
        if sample_rate != self.config.input_sample_rate:
            raise ValueError(
                f"LiveKit bridge expected {self.config.input_sample_rate} Hz input, got {sample_rate} Hz. "
                "Resampling is not implemented in the bridge yet."
            )
        audio_i16 = _as_int16_mono(audio)
        lk_frame = rtc.AudioFrame(
            data=audio_i16.astype("<i2", copy=False).tobytes(),
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=int(audio_i16.shape[0]),
        )
        await self._source.capture_frame(lk_frame)
        self._emit(
            "livekit.bridge.audio_published",
            sample_rate=sample_rate,
            samples=int(audio_i16.shape[0]),
            duration_s=float(audio_i16.shape[0]) / float(sample_rate),
        )

    async def next_output(self) -> HandlerOutput:
        self._ensure_started()
        return await self._outputs.get()

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("LiveKitRoomBridge is not started")

    def _install_room_handlers(self, room: Any, rtc: Any) -> None:
        @room.on("connected")
        def _on_connected() -> None:
            self._emit("livekit.room.connected")

        @room.on("disconnected")
        def _on_disconnected(reason: Any) -> None:
            self._emit("livekit.room.disconnected", reason=str(reason))

        @room.on("participant_connected")
        def _on_participant_connected(participant: Any) -> None:
            self._emit(
                "livekit.room.participant_connected",
                identity=getattr(participant, "identity", ""),
                name=getattr(participant, "name", ""),
            )

        @room.on("track_subscribed")
        def _on_track_subscribed(track: Any, publication: Any, participant: Any) -> None:
            self._emit(
                "livekit.room.track_subscribed",
                participant_identity=getattr(participant, "identity", ""),
                track_name=getattr(track, "name", ""),
                track_sid=getattr(publication, "sid", ""),
            )
            if isinstance(track, rtc.RemoteAudioTrack):
                task = asyncio.create_task(
                    self._consume_audio_track(track, participant),
                    name=f"livekit-audio-{getattr(participant, 'identity', 'remote')}",
                )
                self._audio_tasks.append(task)

        @room.on("transcription_received")
        def _on_transcription_received(segments: list[Any], participant: Any, publication: Any) -> None:
            for segment in segments:
                payload = {
                    "role": "transcript",
                    "participant_identity": getattr(participant, "identity", ""),
                    "track_sid": getattr(publication, "sid", ""),
                    "id": getattr(segment, "id", ""),
                    "text": getattr(segment, "text", ""),
                    "final": bool(getattr(segment, "final", False)),
                    "start_time": getattr(segment, "start_time", None),
                    "end_time": getattr(segment, "end_time", None),
                    "language": getattr(segment, "language", ""),
                }
                self._outputs.put_nowait(payload)
                self._emit("livekit.room.transcription", **payload)

        @room.on("data_received")
        def _on_data_received(packet: Any) -> None:
            data = getattr(packet, "data", b"")
            try:
                content: Any = json.loads(data.decode("utf-8"))
            except Exception:
                try:
                    content = data.decode("utf-8", errors="replace")
                except Exception:
                    content = repr(data)
            payload = {
                "role": "data",
                "topic": getattr(packet, "topic", None),
                "content": content,
            }
            self._outputs.put_nowait(payload)
            self._emit("livekit.room.data", **payload)

    async def _consume_audio_track(self, track: Any, participant: Any) -> None:
        rtc = self._rtc
        if rtc is None:
            return
        stream = rtc.AudioStream.from_track(
            track=track,
            sample_rate=self.config.output_sample_rate,
            num_channels=1,
            frame_size_ms=self.config.output_frame_size_ms,
        )
        try:
            async for event in stream:
                frame = event.frame
                audio = np.asarray(frame.data, dtype=np.int16).copy()
                if self._should_suppress_output(audio):
                    continue
                await self._outputs.put((int(frame.sample_rate), audio))
                self._emit(
                    "livekit.bridge.audio_received",
                    participant_identity=getattr(participant, "identity", ""),
                    sample_rate=int(frame.sample_rate),
                    samples=int(audio.shape[0]),
                    duration_s=float(audio.shape[0]) / float(frame.sample_rate),
                )
        finally:
            await stream.aclose()

    def _emit(self, kind: str, **data: Any) -> None:
        self.event_sink.emit(RuntimeEvent(kind=kind, source="official_runtime.livekit_bridge", data=data))

    def _should_suppress_output(self, audio: np.ndarray) -> bool:
        if not self.config.suppress_silent_output or audio.size == 0:
            return False
        peak = int(np.max(np.abs(audio)))
        return peak <= self.config.silent_output_peak_threshold

    def _validate_config(self, token: str) -> None:
        if not self.config.url:
            raise RuntimeError("LIVEKIT_URL or --url is required")
        if not token:
            raise RuntimeError(
                "LiveKit token is required. Set LIVEKIT_TOKEN, or set LIVEKIT_API_KEY and LIVEKIT_API_SECRET."
            )


def _import_livekit() -> tuple[Any, Any]:
    try:
        from livekit import api, rtc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "LiveKit packages are not installed. Install with: "
            ".venv/bin/python -m pip install -e '.[livekit]'"
        ) from exc
    return rtc, api


def _resolve_token(config: LiveKitBackendConfig, api: Any) -> str:
    if config.token:
        return config.token
    if not (config.api_key and config.api_secret):
        return ""
    return (
        api.AccessToken(config.api_key, config.api_secret)
        .with_identity(config.participant_name)
        .with_name(config.participant_name)
        .with_grants(api.VideoGrants(room_join=True, room=config.room_name))
        .to_jwt()
    )


def _redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}...{value[-4:]}"
