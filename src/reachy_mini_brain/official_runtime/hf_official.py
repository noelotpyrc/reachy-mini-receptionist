"""Temporary adapter for the official app's Hugging Face realtime handler.

This keeps the live ported runtime usable today while the provider-specific
OpenAI-compatible realtime handler is still being ported into this repo.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from .events import EventSink, RuntimeEvent


def _default_official_app_src() -> Path:
    configured = os.getenv("REACHY_MINI_CONVERSATION_APP_SRC")
    if configured:
        return Path(configured)
    for candidate in (
        Path("/Users/noel/projects/reachy_mini_conversation_app/src"),
        Path("/Users/leon/projects/reachy_mini_conversation_app/src"),
    ):
        if candidate.is_dir():
            return candidate
    return Path("/Users/noel/projects/reachy_mini_conversation_app/src")


DEFAULT_OFFICIAL_APP_SRC = _default_official_app_src()


def build_hf_official_handler(
    *,
    event_sink: EventSink,
    official_app_src: Path = DEFAULT_OFFICIAL_APP_SRC,
    instructions: str,
    voice: str = "Sohee",
    connection_mode: str | None = None,
    realtime_ws_url: str | None = None,
    hf_token: str | None = None,
    camera_worker: Any | None = None,
    vision_processor: Any | None = None,
    movement_manager: Any | None = None,
    reachy_mini: Any | None = None,
) -> Any:
    """Build an official HF handler behind the official-runtime handler contract."""

    import sys

    if official_app_src and str(official_app_src) not in sys.path:
        sys.path.insert(0, str(official_app_src))

    os.environ["BACKEND_PROVIDER"] = "huggingface"
    if connection_mode:
        os.environ["HF_REALTIME_CONNECTION_MODE"] = connection_mode
    if realtime_ws_url:
        os.environ["HF_REALTIME_WS_URL"] = realtime_ws_url
    if hf_token:
        os.environ["HF_TOKEN"] = hf_token
    os.environ.setdefault("REACHY_MINI_CUSTOM_PROFILE", "clinic_receptionist")

    try:
        from reachy_mini_conversation_app.config import HF_BACKEND, config, refresh_runtime_config_from_env
        from reachy_mini_conversation_app.huggingface_realtime import HuggingFaceRealtimeHandler
        from reachy_mini_conversation_app.tools.core_tools import ToolDependencies
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Could not import the official Hugging Face handler. Set REACHY_MINI_CONVERSATION_APP_SRC "
            "to the official app src checkout and run from an environment with its dependencies."
        ) from exc

    refresh_runtime_config_from_env()
    config.BACKEND_PROVIDER = HF_BACKEND

    class LiveHuggingFaceRealtimeHandler(HuggingFaceRealtimeHandler):  # type: ignore[misc,valid-type]
        BACKEND_PROVIDER = HuggingFaceRealtimeHandler.BACKEND_PROVIDER
        SAMPLE_RATE = HuggingFaceRealtimeHandler.SAMPLE_RATE
        REFRESH_CLIENT_ON_RECONNECT = HuggingFaceRealtimeHandler.REFRESH_CLIENT_ON_RECONNECT
        AUDIO_INPUT_COST_PER_1M = HuggingFaceRealtimeHandler.AUDIO_INPUT_COST_PER_1M
        AUDIO_OUTPUT_COST_PER_1M = HuggingFaceRealtimeHandler.AUDIO_OUTPUT_COST_PER_1M
        TEXT_INPUT_COST_PER_1M = HuggingFaceRealtimeHandler.TEXT_INPUT_COST_PER_1M
        TEXT_OUTPUT_COST_PER_1M = HuggingFaceRealtimeHandler.TEXT_OUTPUT_COST_PER_1M
        IMAGE_INPUT_COST_PER_1M = HuggingFaceRealtimeHandler.IMAGE_INPUT_COST_PER_1M

        def __init__(self, *args: Any, live_instructions: str, live_event_sink: EventSink, **kwargs: Any) -> None:
            self._live_instructions = live_instructions
            self._live_event_sink = live_event_sink
            self._live_session_task: asyncio.Task[None] | None = None
            super().__init__(*args, **kwargs)

        async def start_up(self) -> None:
            await self._prepare_startup_credentials()
            self.client = await self._build_realtime_client()
            self._connected_event.clear()
            self._live_session_task = asyncio.create_task(
                self._run_realtime_session(),
                name="hf-official-live-session",
            )
            try:
                await asyncio.wait_for(self._connected_event.wait(), timeout=20.0)
            except asyncio.TimeoutError as exc:
                if self._live_session_task.done():
                    await self._live_session_task
                raise RuntimeError("Timed out waiting for official HF realtime session to connect.") from exc

        async def shutdown(self) -> None:
            await super().shutdown()
            if self._live_session_task is not None:
                if not self._live_session_task.done():
                    self._live_session_task.cancel()
                try:
                    await self._live_session_task
                except asyncio.CancelledError:
                    pass
                finally:
                    self._live_session_task = None

        def _get_session_instructions(self) -> str:
            return self._live_instructions

        def _get_active_tool_specs(self) -> list[dict[str, Any]]:
            # Keep the first live pass focused. Ported capabilities are wired in
            # our runtime, while official function-call tool plumbing remains a
            # follow-up item.
            return []

        async def request_text_response(self, prompt: str) -> bool:
            if not getattr(self, "connection", None):
                return False
            await self.connection.conversation.item.create(
                item={
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            )
            await self._safe_response_create()
            return True

        async def emit(self) -> Any:
            item = await super().emit()
            return _normalize_official_output(item)

        def copy(self) -> Any:
            return type(self)(
                self.deps,
                self.gradio_mode,
                self.instance_path,
                startup_voice=self._voice_override,
                live_instructions=self._live_instructions,
                live_event_sink=self._live_event_sink,
            )

    try:
        deps = ToolDependencies(
            reachy_mini=reachy_mini or _NoopReachyMini(),
            movement_manager=movement_manager or _NoopMovementManager(),
            camera_worker=camera_worker,
            vision_processor=vision_processor,
            reception_observer=_OfficialRealtimeObserver(event_sink),
        )
    except TypeError:
        deps = ToolDependencies(
            reachy_mini=reachy_mini or _NoopReachyMini(),
            movement_manager=movement_manager or _NoopMovementManager(),
            camera_worker=camera_worker,
            vision_processor=vision_processor,
        )
        try:
            setattr(deps, "reception_observer", _OfficialRealtimeObserver(event_sink))
        except Exception:
            pass
    return LiveHuggingFaceRealtimeHandler(
        deps,
        gradio_mode=False,
        instance_path=None,
        startup_voice=voice,
        live_instructions=instructions,
        live_event_sink=event_sink,
    )


class _OfficialRealtimeObserver:
    def __init__(self, event_sink: EventSink) -> None:
        self.event_sink = event_sink

    def record_realtime_event(self, kind: str, **data: Any) -> None:
        self.event_sink.emit(RuntimeEvent(kind=f"hf.realtime.{kind}", source="official_runtime.hf_official", data=data))

    def record_session_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.event_sink.emit(
            RuntimeEvent(kind="hf.session.snapshot", source="official_runtime.hf_official", data=snapshot)
        )

    def record_response_metadata(self, response_id: str, metadata: dict[str, Any]) -> None:
        self.event_sink.emit(
            RuntimeEvent(
                kind="hf.response.metadata",
                source="official_runtime.hf_official",
                data={"response_id": response_id, "metadata": metadata},
            )
        )


class _NoopMovementManager:
    def set_listening(self, _value: bool) -> None:
        return None

    def is_idle(self) -> bool:
        return False

    def queue_move(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def clear_move_queue(self) -> None:
        return None

    def set_moving_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _NoopReachyMini:
    media = None

    def get_current_head_pose(self) -> Any:
        return None

    def get_current_joint_positions(self) -> tuple[Any, Any]:
        return None, None


def _normalize_official_output(item: Any) -> Any:
    """Convert official app output objects into official-runtime-friendly values."""

    if item is None:
        return None
    args = getattr(item, "args", None)
    if args is not None:
        if len(args) == 1 and isinstance(args[0], dict):
            return args[0]
        return {"role": "additional_outputs", "content": list(args)}
    return item
