"""Audio CLI tools for Reachy Mini.

Microphone input and speaker output go through the SDK's WebRTC pipeline
(same connection as camera). STT and TTS run locally on the Mac.

Key insight from Pollen's reference conversation app:
- start_recording/stop_recording/start_playing/stop_playing are NO-OPs for WebRTC
- Just call get_audio_sample() / push_audio_sample() directly
- The WebRTC pipeline needs warmup time (~5-10s) before audio flows
- Audio format: float32, 16kHz, 2 channels (interleaved)
- Push MONO data — MediaManager handles channel conversion

macOS GStreamer workaround:
- On macOS, Gst.Bin.add() returns None instead of True (PyGObject binding issue).
- The SDK checks `if not bin.add(elem)` which treats None as failure, aborting the
  audio send chain even though the element WAS added successfully.
- We patch the check to use `is False` instead of `not`.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import click
import numpy as np

from reachy_mini_brain import robot
from reachy_mini_brain.audio_pacing import (
    ROBOT_AUDIO_SAMPLE_RATE,
    WEBRTC_AUDIO_FRAME_MS,
    audio_frame_samples,
)

# Default save location: <project_root>/artifacts/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ARTIFACTS_DIR = _PROJECT_ROOT / "artifacts"


def _patch_bin_add_check():
    """Fix SDK's audio send chain for macOS GStreamer bindings.

    On macOS, Gst.Bin.add() returns None instead of True (PyGObject issue).
    The SDK's `if not bin.add(elem)` treats None as failure, aborting the send
    chain setup even though the element was actually added successfully.

    This patch replaces the `if not` check with `if ... is False`.
    """
    try:
        from reachy_mini.media.webrtc_client_gstreamer import GstWebRTCClient

        if getattr(GstWebRTCClient, "_bin_add_patched", False):
            return

        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        original = GstWebRTCClient._setup_audio_send_chain

        def _patched(self):
            """Patched: use `is False` instead of `not` for bin.add() check."""
            if self._audio_send_ready:
                return
            self._audio_send_ready = True

            self.logger.info("Setting up audio send chain...")
            if self._webrtcbin is None:
                self.logger.error("webrtcbin not found")
                self._audio_send_ready = False
                return

            webrtcbin_parent = self._webrtcbin.get_parent()

            # Find OPUS sink pad
            sink_pad = None
            pt = 96
            for pad in self._iterate_gst(self._webrtcbin.iterate_sink_pads()):
                if pad.is_linked():
                    continue
                caps = pad.query_caps(None)
                if caps and caps.get_size() > 0:
                    s = caps.get_structure(0)
                    enc = s.get_string("encoding-name")
                    if enc and enc.upper() == "OPUS":
                        sink_pad = pad
                        ok, val = s.get_int("payload")
                        if ok:
                            pt = val
                        self.logger.info(f"Found audio sink pad: {pad.get_name()}, pt={pt}")
                        break

            if sink_pad is None:
                self.logger.error("No OPUS sink pad found on webrtcbin")
                self._audio_send_ready = False
                return

            appsrc = Gst.ElementFactory.make("appsrc")
            appsrc.set_property("format", Gst.Format.TIME)
            appsrc.set_property("is-live", True)
            caps = Gst.Caps.from_string(
                f"audio/x-raw,format=F32LE,channels={self.CHANNELS},rate={self.SAMPLE_RATE},layout=interleaved"
            )
            appsrc.set_property("caps", caps)

            audioconvert = Gst.ElementFactory.make("audioconvert")
            audioresample = Gst.ElementFactory.make("audioresample")
            opusenc = Gst.ElementFactory.make("opusenc")
            opusenc.set_property("audio-type", "restricted-lowdelay")
            opusenc.set_property("frame-size", 10)
            rtpopuspay = Gst.ElementFactory.make("rtpopuspay")
            rtpopuspay.set_property("pt", pt)

            elems = (appsrc, audioconvert, audioresample, opusenc, rtpopuspay)

            target_bin = webrtcbin_parent if webrtcbin_parent else self._webrtcsrc
            for elem in elems:
                # FIX: use `is False` — on macOS, bin.add() returns None on success
                if target_bin.add(elem) is False:
                    self.logger.error(
                        f"Failed to add {elem.get_name()} to {target_bin.get_name()}"
                    )
                    self._audio_send_ready = False
                    return

            appsrc.link(audioconvert)
            audioconvert.link(audioresample)
            audioresample.link(opusenc)
            opusenc.link(rtpopuspay)

            src_pad = rtpopuspay.get_static_pad("src")
            link_result = src_pad.link_full(sink_pad, Gst.PadLinkCheck.NOTHING)
            if link_result != Gst.PadLinkReturn.OK:
                self.logger.error(f"Failed to link rtpopuspay to webrtcbin: {link_result}")
                self._audio_send_ready = False
                return

            for elem in elems:
                elem.sync_state_with_parent()

            self._appsrc = appsrc
            self.logger.info("Audio send chain ready (bidirectional audio enabled)")

        GstWebRTCClient._setup_audio_send_chain = _patched
        GstWebRTCClient._bin_add_patched = True
    except Exception as e:
        print(f"  Warning: could not patch audio send chain: {e}", file=sys.stderr)


# Apply patch at import time so it's in place before any ReachyMini() is created
_patch_bin_add_check()


def _wait_for_audio(mini, timeout: float = 60.0) -> bool:
    """Wait until the WebRTC audio pipeline starts producing samples.

    Returns True if audio is flowing, False if timeout.
    """
    start = time.time()
    while time.time() - start < timeout:
        sample = mini.media.get_audio_sample()
        if sample is not None and sample.size > 0:
            return True
        time.sleep(0.5)
    return False


def push_audio_realtime(
    push,
    audio: np.ndarray,
    *,
    sample_rate: int = ROBOT_AUDIO_SAMPLE_RATE,
    chunk_size: int | None = None,
    frame_duration_ms: int = WEBRTC_AUDIO_FRAME_MS,
    clock=time.monotonic,
    sleep=time.sleep,
) -> None:
    """Push PCM chunks at exact realtime pace.

    The WebRTC sender is sensitive to overfeeding. Live A/B testing on 2026-06-16
    confirmed that faster-than-realtime sending made robot playback choppy, while
    exact monotonic pacing at the configured frame duration was smooth.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if chunk_size is None:
        chunk_size = audio_frame_samples(sample_rate, frame_duration_ms=frame_duration_ms)
    elif chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    next_deadline = clock()
    for i in range(0, len(audio), chunk_size):
        chunk = np.ascontiguousarray(audio[i : i + chunk_size])
        push(chunk)
        next_deadline += len(chunk) / float(sample_rate)
        delay = next_deadline - clock()
        if delay > 0:
            sleep(delay)


@click.group()
def cli():
    pass


@cli.command()
@click.option("--duration", default=5.0, help="Recording duration in seconds")
@click.option(
    "--model", default="base",
    type=click.Choice(["tiny", "base", "small", "medium"]),
    help="Whisper model size (default: base)",
)
@click.option("--language", default="en", help="Language code (e.g. 'en'). Use 'auto' for auto-detect.")
@click.option("--save-wav", default=None, help="Also save raw audio to this WAV path")
def listen(duration, model, language, save_wav):
    """Record from robot mic and transcribe to text (STT)."""
    import soundfile as sf
    from reachy_mini import ReachyMini

    from reachy_mini_brain import stt

    robot.ensure_ready()

    with ReachyMini() as mini:
        # Wait for WebRTC audio pipeline to produce real samples
        print(f"  Waiting for audio pipeline...", file=sys.stderr)
        if not _wait_for_audio(mini):
            click.echo("Error: audio pipeline did not start (timeout)", err=True)
            raise SystemExit(1)

        print(f"  Recording {duration}s from robot mic...", file=sys.stderr)
        chunks: list[np.ndarray] = []
        start = time.time()
        while time.time() - start < duration:
            sample = mini.media.get_audio_sample()
            if sample is not None:
                chunks.append(sample)
            time.sleep(0.01)  # tight poll like reference app

    if not chunks:
        click.echo("Error: no audio captured", err=True)
        raise SystemExit(1)

    audio = np.concatenate(chunks)

    # Take first channel only (like reference app) — more reliable than mean
    if audio.ndim > 1:
        audio = audio[:, 0]

    print(f"  Captured {len(audio)} samples ({len(audio)/16000:.1f}s)", file=sys.stderr)

    # Optionally save raw audio
    if save_wav:
        sf.write(save_wav, audio, 16000)
        print(f"  Saved raw audio to {save_wav}", file=sys.stderr)

    # Transcribe
    lang = None if language == "auto" else language
    print(f"  Transcribing with whisper-{model} (lang={lang or 'auto'})...", file=sys.stderr)
    transcript = stt.transcribe_array(audio, sample_rate=16000, model_size=model, language=lang)
    click.echo(transcript)


@cli.command()
@click.argument("text")
@click.option("--voice", default="en_US-lessac-medium", help="Piper voice name")
def speak(text, voice):
    """Synthesize text and play through robot speaker (TTS)."""
    from reachy_mini import ReachyMini

    from reachy_mini_brain import tts

    robot.ensure_ready()

    # Synthesize locally
    print(f"  Synthesizing speech...", file=sys.stderr)
    audio, sample_rate = tts.synthesize_array(text, voice=voice)

    if audio.size == 0:
        click.echo("Error: TTS produced no audio", err=True)
        raise SystemExit(1)

    # Resample to 16kHz (robot expects 16kHz)
    if sample_rate != 16000:
        from scipy.signal import resample

        num_samples = int(len(audio) * 16000 / sample_rate)
        audio = resample(audio, num_samples).astype(np.float32)

    # Keep mono — MediaManager handles channel conversion (like reference app)
    if audio.ndim > 1:
        audio = audio[:, 0]

    # Push through WebRTC to robot speaker
    print(f"  Playing on robot speaker ({len(audio)} samples)...", file=sys.stderr)
    with ReachyMini() as mini:
        print(f"  Waiting for audio pipeline...", file=sys.stderr)
        if not _wait_for_audio(mini):
            click.echo("Error: audio pipeline did not start (timeout)", err=True)
            raise SystemExit(1)

        time.sleep(1)  # let send chain finish setup (like reference app)

        push_audio_realtime(mini.media.push_audio_sample, audio, sample_rate=ROBOT_AUDIO_SAMPLE_RATE)

        time.sleep(0.5)

    click.echo("done")


@cli.command()
@click.argument("path", type=click.Path(exists=True))
def play_sound(path):
    """Play a WAV file through the robot speaker."""
    import soundfile as sf
    from reachy_mini import ReachyMini

    robot.ensure_ready()

    audio, sample_rate = sf.read(path, dtype="float32")

    # Resample to 16kHz if needed
    if sample_rate != 16000:
        from scipy.signal import resample

        num_samples = int(len(audio) * 16000 / sample_rate)
        audio = resample(audio, num_samples).astype(np.float32)

    # Keep mono — MediaManager handles channel conversion
    if audio.ndim > 1:
        audio = audio[:, 0]

    print(f"  Playing {path} on robot speaker...", file=sys.stderr)
    with ReachyMini() as mini:
        print(f"  Waiting for audio pipeline...", file=sys.stderr)
        if not _wait_for_audio(mini):
            click.echo("Error: audio pipeline did not start (timeout)", err=True)
            raise SystemExit(1)

        time.sleep(1)

        push_audio_realtime(mini.media.push_audio_sample, audio, sample_rate=ROBOT_AUDIO_SAMPLE_RATE)

        time.sleep(0.5)

    click.echo("done")


@cli.command()
def diag():
    """Diagnostic: dump GStreamer pipeline structure and audio send chain state."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init([])

    from reachy_mini import ReachyMini

    print("--- Audio Pipeline Diagnostic ---")
    print(f"GStreamer version: {Gst.version_string()}")

    with ReachyMini() as mini:
        print(f"\nWaiting for audio pipeline...")
        if not _wait_for_audio(mini, timeout=30):
            print("ERROR: audio pipeline did not start")
            raise SystemExit(1)

        time.sleep(3)  # let send chain attempt to set up

        audio_backend = mini.media.audio
        print(f"\nBackend type: {type(audio_backend).__name__}")

        # Check key attributes
        appsrc = getattr(audio_backend, '_appsrc', 'MISSING')
        send_ready = getattr(audio_backend, '_audio_send_ready', 'MISSING')
        webrtcbin = getattr(audio_backend, '_webrtcbin', 'MISSING')
        pipeline = getattr(audio_backend, '_pipeline_record', 'MISSING')

        print(f"_appsrc: {appsrc}")
        print(f"_audio_send_ready: {send_ready}")
        print(f"_webrtcbin: {webrtcbin}")
        if webrtcbin and webrtcbin != 'MISSING':
            print(f"  webrtcbin name: {webrtcbin.get_name()}")
            parent = webrtcbin.get_parent()
            print(f"  webrtcbin parent: {parent.get_name() if parent else None}")
            grandparent = parent.get_parent() if parent else None
            print(f"  webrtcbin grandparent: {grandparent.get_name() if grandparent else None}")

        if pipeline and pipeline != 'MISSING':
            print(f"\nPipeline: {pipeline.get_name()}")
            state = pipeline.get_state(0)
            print(f"Pipeline state: {state.state.value_nick}")

            # List all elements in the pipeline recursively
            print(f"\nAll elements in pipeline:")
            iterator = pipeline.iterate_recurse()
            while True:
                result, elem = iterator.next()
                if result == Gst.IteratorResult.DONE:
                    break
                if result == Gst.IteratorResult.OK:
                    parent = elem.get_parent()
                    factory = elem.get_factory()
                    factory_name = factory.get_name() if factory else "?"
                    print(f"  {elem.get_name()} ({factory_name}) in {parent.get_name() if parent else '?'}")
                elif result == Gst.IteratorResult.RESYNC:
                    iterator.resync()

        # Test basic GStreamer element creation
        print(f"\n--- Element creation test ---")
        test_src = Gst.ElementFactory.make("appsrc")
        print(f"Gst.ElementFactory.make('appsrc'): {test_src} (name={test_src.get_name() if test_src else 'None'})")
        test_bin = Gst.Bin.new("test_diag_bin")
        result = test_bin.add(test_src) if test_src else False
        print(f"test_bin.add(appsrc): {result}")

    print("\n--- Done ---")


@cli.command()
def doa():
    """Get direction of arrival from robot mic array.

    Prints JSON: {"angle_degrees": ..., "speech_detected": true/false}
    Angle: 0 = left, 90 = front, 180 = right.

    Note: DoA requires ReSpeaker USB device connected locally.
    Over WiFi/WebRTC, this will likely fail.
    """
    from reachy_mini import ReachyMini

    robot.ensure_ready()

    with ReachyMini() as mini:
        result = mini.media.get_DoA()

    if result is None:
        click.echo(json.dumps({
            "error": "DoA not available (ReSpeaker USB not connected locally)",
        }))
        raise SystemExit(1)

    angle_rad, speech_detected = result
    data = {
        "angle_degrees": round(math.degrees(angle_rad), 1),
        "angle_radians": round(angle_rad, 3),
        "speech_detected": bool(speech_detected),
    }
    click.echo(json.dumps(data))


if __name__ == "__main__":
    cli()
