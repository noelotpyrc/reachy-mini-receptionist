"""Reachy Mini robot IO adapters for the official-style runtime."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import numpy as np
from numpy.typing import NDArray

from reachy_mini_brain.audio_pacing import ROBOT_AUDIO_SAMPLE_RATE

from .stream_runtime import AudioFrame


class ReachyRobotSession:
    """Own one ReachyMini SDK instance for the live official-runtime path."""

    def __init__(
        self,
        *,
        host: str | None = None,
        warmup_audio: bool = True,
        warmup_video: bool = True,
        audio_timeout_s: float = 60.0,
        video_timeout_s: float = 60.0,
        robot_factory: Callable[[], Any] | None = None,
        milestone_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.host = host
        self.warmup_audio = warmup_audio
        self.warmup_video = warmup_video
        self.audio_timeout_s = audio_timeout_s
        self.video_timeout_s = video_timeout_s
        self.robot_factory = robot_factory
        self.milestone_callback = milestone_callback
        self.mini: Any | None = None

    def start(self) -> Any:
        """Start the daemon if needed, create SDK connection, and warm media."""

        if self.host:
            import os

            os.environ["REACHY_HOST"] = self.host
            self._milestone("robot_host_selected", host=self.host, connection_mode="network")
        else:
            self._milestone("robot_host_selected", host="sdk_discovery", connection_mode="discovery")

        from reachy_mini_brain import robot
        from reachy_mini_brain.audio import _patch_bin_add_check

        _patch_bin_add_check()
        self._milestone("robot_control_check_start")
        robot.ensure_ready()
        self._milestone("robot_control_ready")
        robot._session_active = True

        if self.robot_factory is not None:
            self._milestone("robot_sdk_connect_start", factory="custom")
            self.mini = self.robot_factory()
        else:
            from reachy_mini import ReachyMini

            kwargs: dict[str, Any] = {}
            if self.host:
                kwargs.update(host=self.host, connection_mode="network", timeout=15.0)
            self._milestone("robot_sdk_connect_start", kwargs=_public_kwargs(kwargs))
            self.mini = ReachyMini(**kwargs)
        self._milestone("robot_sdk_connected")

        if self.warmup_audio:
            self._milestone("robot_audio_warmup_start", timeout_s=self.audio_timeout_s)
            if not wait_for_audio(self.mini, timeout=self.audio_timeout_s):
                self._milestone("robot_audio_warmup_failed", timeout_s=self.audio_timeout_s)
                raise RuntimeError("Robot audio pipeline did not start before timeout.")
            self._milestone("robot_audio_warmup_ok")
        else:
            self._milestone("robot_audio_warmup_skipped")

        if self.warmup_video:
            self._milestone("robot_video_warmup_start", timeout_s=self.video_timeout_s)
            if not wait_for_video(self.mini, timeout=self.video_timeout_s):
                self._milestone("robot_video_warmup_failed", timeout_s=self.video_timeout_s)
                raise RuntimeError("Robot video pipeline did not start before timeout.")
            self._milestone("robot_video_warmup_ok")
        else:
            self._milestone("robot_video_warmup_skipped")
        return self.mini

    def stop(self) -> None:
        """Close SDK media/client resources without sending sleep/motion commands."""

        from reachy_mini_brain import robot

        self._milestone("robot_session_stop_start")
        mini = self.mini
        self.mini = None
        if mini is not None:
            media_manager = getattr(mini, "media_manager", None)
            close = getattr(media_manager, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            client = getattr(mini, "client", None)
            disconnect = getattr(client, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception:
                    pass
        robot._session_active = False
        self._milestone("robot_session_stop_done")

    def _milestone(self, name: str, **data: Any) -> None:
        callback = self.milestone_callback
        if not callable(callback):
            return
        try:
            callback(name, data)
        except Exception:
            pass


class ReachyAudioSource:
    """Audio source backed by ``mini.media.get_audio_sample()``."""

    def __init__(
        self,
        mini: Any,
        *,
        sample_rate: int = ROBOT_AUDIO_SAMPLE_RATE,
        poll_interval_s: float = 0.01,
        max_duration_s: float | None = None,
        stop_event: asyncio.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mini = mini
        self.sample_rate = sample_rate
        self.poll_interval_s = poll_interval_s
        self.max_duration_s = max_duration_s
        self.stop_event = stop_event
        self.clock = clock
        self._started_at: float | None = None

    async def read(self) -> AudioFrame | None:
        if self._started_at is None:
            self._started_at = self.clock()
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                return None
            if self.max_duration_s is not None and self.clock() - self._started_at >= self.max_duration_s:
                return None

            sample = self.mini.media.get_audio_sample()
            if sample is not None:
                audio = _as_int16_mono(sample)
                await asyncio.sleep(0)
                return self.sample_rate, audio
            await asyncio.sleep(self.poll_interval_s)


class ReachyAudioSink:
    """Audio sink backed by ``mini.media.push_audio_sample()``.

    This mirrors the official app's headless playback semantics: each handler
    audio tuple is converted/resampled once, pushed once, then control yields
    back to the event loop. The Reachy SDK/GStreamer layer owns live buffer
    timing.
    """

    def __init__(
        self,
        mini: Any,
        *,
        sample_rate: int = ROBOT_AUDIO_SAMPLE_RATE,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.mini = mini
        self.sample_rate = sample_rate
        self._closed = False

    async def write(self, frame: AudioFrame) -> None:
        if self._closed:
            raise RuntimeError("audio sink is closed")
        input_sample_rate, audio = frame
        output_sample_rate = _robot_output_sample_rate(self.mini, default=self.sample_rate)
        audio_f32 = _as_float32_mono(audio)

        if audio_f32.size == 0:
            await asyncio.sleep(0)
            return

        if input_sample_rate != output_sample_rate:
            audio_f32 = _resample_float32(audio_f32, input_sample_rate, output_sample_rate)
            if audio_f32.size == 0:
                await asyncio.sleep(0)
                return

        self.mini.media.push_audio_sample(np.ascontiguousarray(audio_f32, dtype=np.float32))
        await asyncio.sleep(0)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def close(self) -> None:
        self._closed = True
        await asyncio.sleep(0)


class ReachyCameraFrameProvider:
    """Camera frame provider backed by ``mini.media.get_frame()``."""

    def __init__(self, mini: Any) -> None:
        self.mini = mini
        self._head_tracking_enabled = False

    def get_latest_frame(self) -> NDArray[np.uint8] | None:
        frame = self.mini.media.get_frame()
        if frame is None:
            return None
        return np.asarray(frame).copy()

    def set_head_tracking_enabled(self, enabled: bool) -> None:
        # The full face-offset live loop is intentionally separate. This method
        # lets the capability path preserve state until that loop is wired.
        self._head_tracking_enabled = bool(enabled)

    @property
    def head_tracking_enabled(self) -> bool:
        return self._head_tracking_enabled


def wait_for_audio(mini: Any, *, timeout: float = 60.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        sample = mini.media.get_audio_sample()
        if sample is not None and getattr(sample, "size", 0) > 0:
            return True
        time.sleep(0.5)
    return False


def wait_for_video(mini: Any, *, timeout: float = 60.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        frame = mini.media.get_frame()
        if frame is not None and getattr(frame, "size", 0) > 0:
            return True
        time.sleep(0.5)
    return False


def _robot_output_sample_rate(mini: Any, *, default: int) -> int:
    getter = getattr(getattr(mini, "media", None), "get_output_audio_samplerate", None)
    if not callable(getter):
        return default
    sample_rate = getter()
    return int(sample_rate or default)


def _public_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key not in {"password", "secret", "token"}}


def _resample_float32(
    audio: NDArray[np.float32],
    input_sample_rate: int,
    output_sample_rate: int,
) -> NDArray[np.float32]:
    if input_sample_rate <= 0:
        input_sample_rate = output_sample_rate
    num_samples = int(len(audio) * output_sample_rate / input_sample_rate)
    if num_samples == 0:
        return np.empty(0, dtype=np.float32)
    from scipy.signal import resample

    return np.ascontiguousarray(resample(audio, num_samples), dtype=np.float32)


def _as_int16_mono(audio: NDArray[Any]) -> NDArray[np.int16]:
    arr = np.asarray(audio)
    if arr.ndim == 2:
        if arr.shape[1] > arr.shape[0]:
            arr = arr.T
        if arr.shape[1] > 1:
            arr = arr[:, 0]
        else:
            arr = arr[:, 0]
    if arr.dtype == np.int16:
        return np.ascontiguousarray(arr.reshape(-1), dtype=np.int16)
    if np.issubdtype(arr.dtype, np.floating):
        arr = np.clip(arr, -1.0, 1.0) * 32767.0
    else:
        arr = np.clip(arr, -32768, 32767)
    return np.ascontiguousarray(arr.reshape(-1), dtype=np.int16)


def _as_float32_mono(audio: NDArray[Any]) -> NDArray[np.float32]:
    arr = np.asarray(audio)
    if arr.ndim == 2:
        if arr.shape[1] > arr.shape[0]:
            arr = arr.T
        if arr.shape[1] > 1:
            arr = arr[:, 0]
        else:
            arr = arr[:, 0]
    if arr.dtype == np.float32:
        return np.ascontiguousarray(arr.reshape(-1), dtype=np.float32)
    if np.issubdtype(arr.dtype, np.integer):
        arr = arr.astype(np.float32) / 32768.0
    else:
        arr = arr.astype(np.float32)
    return np.ascontiguousarray(np.clip(arr.reshape(-1), -1.0, 1.0), dtype=np.float32)
