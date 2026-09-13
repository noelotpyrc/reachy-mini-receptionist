"""Opt-in bounded AV probe, using the existing bidirectional SDK path.

No policy, LLM, model loading, OPS launch, or native-app playback is involved.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import wave
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


def configure_audio_send_chain(mode: str) -> None:
    """Select process-wide SDK compatibility before opening any connection."""
    if mode not in {"stock", "legacy"}:
        raise ValueError("Audio send chain must be stock or legacy")
    from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient

    sdk_version = version("reachy-mini")
    if mode == "legacy":
        if sdk_version != "1.10.0":
            raise RuntimeError("Legacy audio send chain is validated only with reachy-mini 1.10.0")
        from reachy_mini_brain.audio import _patch_bin_add_check

        _patch_bin_add_check()
        if not getattr(GstWebRTCClient, "_bin_add_patched", False):
            raise RuntimeError("Legacy audio send-chain override did not install")
    elif getattr(GstWebRTCClient, "_bin_add_patched", False):
        raise RuntimeError("Stock audio requested after legacy override; restart the service")
    LOGGER.info("av_probe audio_send_chain=%s sdk_version=%s", mode, sdk_version)


def load_probe_audio(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1 or wav.getcomptype() != "NONE":
            raise ValueError("Probe WAV must be uncompressed mono PCM16")
        rate = wav.getframerate()
        if rate != 16000 or not 0 < wav.getnframes() <= rate * 60:
            raise ValueError("Probe WAV must be 16 kHz and at most 60 seconds")
        samples = wav.readframes(wav.getnframes())
        if len(samples) != wav.getnframes() * 2:
            raise ValueError("Truncated probe WAV")
        return rate, samples


def make_av_probe(
    *, host: str, wav_path: Path, duration: float = 60,
    sdk_factory: Callable[[], Any] | None = None,
    audio_send_chain: str = "stock",
    lead_in_ms: float = 0,
) -> Callable:
    if not host or not 1 <= duration <= 120:
        raise ValueError("Explicit robot host and a duration between 1 and 120 seconds are required")
    if audio_send_chain not in {"stock", "legacy"}:
        raise ValueError("Audio send chain must be stock or legacy")
    if not 0 <= lead_in_ms <= 1000:
        raise ValueError("Probe audio lead-in must be between 0 and 1000 ms")
    sample_rate, pcm = load_probe_audio(wav_path)

    async def run(stop: asyncio.Event, ready: Callable[[], None]) -> None:
        if stop.is_set():
            return
        loop = asyncio.get_running_loop()
        thread_stop = threading.Event()

        def worker() -> None:
            import numpy as np

            if thread_stop.is_set():
                return
            factory = sdk_factory
            if factory is None:
                configure_audio_send_chain(audio_send_chain)
                from reachy_mini import ReachyMini

                def factory():
                    return ReachyMini(host=host, connection_mode="network", timeout=15)
            mini = factory()
            media = mini.media
            try:
                if thread_stop.is_set():
                    return
                if media.get_output_audio_samplerate() != sample_rate:
                    raise RuntimeError("SDK output sample rate does not match the probe WAV")
                started = time.monotonic()
                audio_count = video_count = 0
                next_report = started
                submitted = False
                samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
                lead_in_samples = round(sample_rate * lead_in_ms / 1000)
                if lead_in_samples:
                    samples = np.concatenate((np.zeros(lead_in_samples, dtype=np.float32), samples))
                while not thread_stop.is_set() and time.monotonic() - started < duration:
                    audio = media.get_audio_sample()
                    video = media.get_frame()
                    now = time.monotonic()
                    if thread_stop.is_set() or now - started >= duration:
                        break
                    audio_count += int(audio is not None and audio.size > 0)
                    video_count += int(video is not None and video.size > 0)
                    if not submitted and audio_count and video_count:
                        loop.call_soon_threadsafe(ready)
                        # The WAV is already complete. Like ReachyAudioSink,
                        # enqueue it once; SDK/GStreamer owns playback timing.
                        # Blocking input reads must not pace output chunks.
                        if not thread_stop.is_set():
                            push_start = time.monotonic()
                            media.push_audio_sample(samples)
                            submitted = True
                            LOGGER.info(
                                "av_probe audio_queued mode=sdk_paced samples=%d audio_s=%.3f "
                                "lead_in_ms=%.3f submit_ms=%.3f",
                                len(samples), len(samples) / sample_rate,
                                lead_in_samples / sample_rate * 1000,
                                (time.monotonic() - push_start) * 1000,
                            )
                    if now >= next_report:
                        LOGGER.info(
                            "av_probe elapsed_s=%.3f audio_samples=%d video_frames=%d submitted_audio_s=%.3f",
                            now - started, audio_count, video_count,
                            len(samples) / sample_rate if submitted else 0.0,
                        )
                        next_report = now + 1
                    thread_stop.wait(0.005)
                if not thread_stop.is_set() and not submitted:
                    raise RuntimeError("Probe ended without both audio and video input")
            finally:
                # Report cleanup failures; never claim a clean stop if flush failed.
                try:
                    media.audio.clear_player()
                finally:
                    try:
                        media.close()
                    finally:
                        mini.client.disconnect()

        task = asyncio.create_task(asyncio.to_thread(worker), name="reception-av-probe-worker")
        stop_waiter = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({task, stop_waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            thread_stop.set()
            stop_waiter.cancel()
            try:
                await stop_waiter
            except asyncio.CancelledError:
                pass
            # Cancellation of the control coroutine must not abandon SDK cleanup.
            await asyncio.shield(task)

    return run
