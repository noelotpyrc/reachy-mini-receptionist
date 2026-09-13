import asyncio
import sys
import threading
import time
import types
import wave

import numpy as np
import pytest

from reachy_mini_brain.reception_av_probe import load_probe_audio, make_av_probe
from reachy_mini_brain import reception_av_probe as probe


@pytest.fixture
def audio_chain_modules(monkeypatch):
    class SDKClient:
        pass

    calls = []

    def install():
        calls.append("legacy")
        SDKClient._bin_add_patched = True

    sdk_module = types.ModuleType("reachy_mini.media.webrtc_client_gstreamer")
    sdk_module.GstWebRTCClient = SDKClient
    audio_module = types.ModuleType("reachy_mini_brain.audio")
    audio_module._patch_bin_add_check = install
    monkeypatch.setitem(sys.modules, sdk_module.__name__, sdk_module)
    monkeypatch.setitem(sys.modules, audio_module.__name__, audio_module)
    monkeypatch.setattr(probe, "version", lambda _: "1.10.0")
    return SDKClient, calls, audio_module


def test_stock_does_not_install_override(audio_chain_modules):
    sdk, calls, _ = audio_chain_modules
    probe.configure_audio_send_chain("stock")
    assert not calls
    assert not getattr(sdk, "_bin_add_patched", False)


def test_legacy_reuses_helper_and_stock_requires_restart(audio_chain_modules):
    sdk, calls, _ = audio_chain_modules
    probe.configure_audio_send_chain("legacy")
    assert calls == ["legacy"]
    assert sdk._bin_add_patched
    with pytest.raises(RuntimeError, match="restart the service"):
        probe.configure_audio_send_chain("stock")


def test_legacy_install_failure_is_not_silently_ignored(audio_chain_modules):
    _, _, helper = audio_chain_modules
    helper._patch_bin_add_check = lambda: None
    with pytest.raises(RuntimeError, match="did not install"):
        probe.configure_audio_send_chain("legacy")


def test_legacy_rejects_unvalidated_sdk(monkeypatch, audio_chain_modules):
    _, calls, _ = audio_chain_modules
    monkeypatch.setattr(probe, "version", lambda _: "1.11.0")
    with pytest.raises(RuntimeError, match="validated only"):
        probe.configure_audio_send_chain("legacy")
    assert not calls


def test_unknown_audio_chain_is_rejected_before_sdk_import():
    with pytest.raises(ValueError, match="stock or legacy"):
        probe.configure_audio_send_chain("unknown")


def write_wav(path, rate=16000, channels=1, seconds=0.1):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.full((int(rate * seconds), channels), 1234, dtype="<i2").tobytes())


class FakeMedia:
    def __init__(self):
        self.audio = self
        self.samples = []
        self.closed = False
        self.flushed = False

    def get_output_audio_samplerate(self):
        return 16000

    def get_audio_sample(self):
        return np.zeros(320, dtype=np.float32)

    def get_frame(self):
        return np.zeros((2, 2, 3), dtype=np.uint8)

    def push_audio_sample(self, samples):
        assert not self.closed
        self.samples.append(samples.copy())

    def clear_player(self):
        self.flushed = True

    def close(self):
        assert self.flushed
        self.closed = True


class FakeSDK:
    def __init__(self):
        self.media = FakeMedia()
        self.client = self
        self.disconnected = False

    def disconnect(self):
        self.disconnected = True


@pytest.mark.parametrize("mode", ["stock", "legacy"])
def test_send_chain_selected_before_real_sdk_factory(monkeypatch, tmp_path, mode):
    path = tmp_path / "speech.wav"
    write_wav(path)
    sdk = FakeSDK()
    order = []
    monkeypatch.setattr(probe, "configure_audio_send_chain", lambda selected: order.append(selected))
    module = types.ModuleType("reachy_mini")

    def connect(**kwargs):
        assert order == [mode]
        order.append("connect")
        return sdk

    module.ReachyMini = connect
    monkeypatch.setitem(sys.modules, "reachy_mini", module)
    runner = make_av_probe(host="test-robot", wav_path=path, duration=1, audio_send_chain=mode)
    asyncio.run(runner(asyncio.Event(), lambda: None))
    assert order == [mode, "connect"]
    assert len(sdk.media.samples) == 1
    assert sdk.disconnected


def test_probe_streams_speech_through_existing_sdk_and_cleans_up(tmp_path):
    path = tmp_path / "speech.wav"
    write_wav(path)
    sdk = FakeSDK()
    runner = make_av_probe(host="test-robot", wav_path=path, duration=1, sdk_factory=lambda: sdk)
    readiness = []
    asyncio.run(runner(asyncio.Event(), lambda: readiness.append(True)))
    assert readiness == [True]
    assert len(sdk.media.samples) == 1
    np.testing.assert_allclose(np.concatenate(sdk.media.samples), 1234 / 32768)
    assert sdk.media.flushed and sdk.media.closed and sdk.disconnected


def test_blocking_capture_does_not_trickle_output(tmp_path):
    path = tmp_path / "speech.wav"
    write_wav(path, seconds=0.8)
    sdk = FakeSDK()

    def slow_frame():
        time.sleep(0.02)
        return np.zeros((2, 2, 3), dtype=np.uint8)

    sdk.media.get_frame = slow_frame
    runner = make_av_probe(host="test-robot", wav_path=path, duration=1, sdk_factory=lambda: sdk)
    asyncio.run(runner(asyncio.Event(), lambda: None))
    # A single complete submission proves later capture waits cannot starve
    # the SDK's playback queue. This does not claim physical speaker delivery.
    assert len(sdk.media.samples) == 1
    assert sdk.media.samples[0].size == 12800
    np.testing.assert_array_equal(sdk.media.samples[0], np.full(12800, 1234 / 32768, dtype=np.float32))


def test_lead_in_prepends_silence_without_changing_wav_or_speech(tmp_path):
    path = tmp_path / "speech.wav"
    write_wav(path)
    original = path.read_bytes()
    sdk = FakeSDK()
    runner = make_av_probe(host="test-robot", wav_path=path, duration=1,
                          sdk_factory=lambda: sdk, lead_in_ms=300)
    asyncio.run(runner(asyncio.Event(), lambda: None))
    assert len(sdk.media.samples) == 1
    samples = sdk.media.samples[0]
    assert len(samples) == 4800 + 1600
    np.testing.assert_array_equal(samples[:4800], np.zeros(4800, dtype=np.float32))
    np.testing.assert_array_equal(samples[4800:], np.full(1600, 1234 / 32768, dtype=np.float32))
    assert path.read_bytes() == original
    assert sdk.media.flushed and sdk.disconnected


@pytest.mark.parametrize("lead_in_ms", [-1, 1001, float("nan"), float("inf")])
def test_invalid_lead_in_rejected_before_loading_wav(tmp_path, lead_in_ms):
    with pytest.raises(ValueError, match="lead-in"):
        make_av_probe(host="test-robot", wav_path=tmp_path / "missing.wav", lead_in_ms=lead_in_ms)


def test_stop_after_submission_flushes_and_prevents_more_output(tmp_path):
    path = tmp_path / "speech.wav"
    write_wav(path, seconds=2)
    sdk = FakeSDK()
    queued = threading.Event()
    original_push = sdk.media.push_audio_sample

    def push(samples):
        original_push(samples)
        queued.set()

    sdk.media.push_audio_sample = push

    async def exercise():
        stop = asyncio.Event()
        runner = make_av_probe(host="test-robot", wav_path=path, duration=5, sdk_factory=lambda: sdk)
        task = asyncio.create_task(runner(stop, lambda: None))
        async with asyncio.timeout(2):
            while not queued.is_set():
                await asyncio.sleep(0.005)
            stop.set()
            await task

    asyncio.run(exercise())
    assert len(sdk.media.samples) == 1
    assert sdk.media.flushed and sdk.media.closed and sdk.disconnected


def test_stop_during_blocking_capture_does_not_submit(tmp_path):
    path = tmp_path / "speech.wav"
    write_wav(path)
    sdk = FakeSDK()
    reading = threading.Event()
    release = threading.Event()

    def read():
        reading.set()
        assert release.wait(2)
        return np.zeros((2, 2, 3), dtype=np.uint8)

    sdk.media.get_frame = read

    async def exercise():
        stop = asyncio.Event()
        readiness = []
        runner = make_av_probe(host="test-robot", wav_path=path, duration=5, sdk_factory=lambda: sdk)
        task = asyncio.create_task(runner(stop, lambda: readiness.append(True)))
        async with asyncio.timeout(2):
            while not reading.is_set():
                await asyncio.sleep(0.005)
            stop.set()
            await asyncio.sleep(0.02)
            release.set()
            await task
        assert not readiness

    asyncio.run(exercise())
    assert not sdk.media.samples
    assert sdk.media.flushed and sdk.media.closed and sdk.disconnected


def test_probe_stop_during_sdk_startup_never_plays_audio(tmp_path):
    path = tmp_path / "speech.wav"
    write_wav(path)
    sdk = FakeSDK()
    connected = threading.Event()
    release = threading.Event()

    def factory():
        connected.set()
        assert release.wait(2)
        return sdk

    async def exercise():
        stop = asyncio.Event()
        ready = []
        runner = make_av_probe(host="test-robot", wav_path=path, sdk_factory=factory)
        task = asyncio.create_task(runner(stop, lambda: ready.append(True)))
        while not connected.is_set():
            await asyncio.sleep(0.005)
        stop.set()
        await asyncio.sleep(0.02)
        release.set()
        await task
        assert not ready

    asyncio.run(exercise())
    assert sdk.media.samples == []
    assert sdk.media.closed and sdk.disconnected


def test_probe_stopped_before_start_does_not_connect(tmp_path):
    path = tmp_path / "speech.wav"
    write_wav(path)
    runner = make_av_probe(host="test-robot", wav_path=path, sdk_factory=lambda: pytest.fail("Connected"))
    stop = asyncio.Event()
    stop.set()
    asyncio.run(runner(stop, lambda: pytest.fail("Ready")))


def test_probe_flush_error_still_closes_sdk(tmp_path):
    path = tmp_path / "speech.wav"
    write_wav(path)
    sdk = FakeSDK()

    def fail_flush():
        sdk.media.flushed = True
        raise RuntimeError("flush failed")

    sdk.media.clear_player = fail_flush
    runner = make_av_probe(host="test-robot", wav_path=path, duration=1, sdk_factory=lambda: sdk)
    with pytest.raises(RuntimeError, match="flush failed"):
        asyncio.run(runner(asyncio.Event(), lambda: None))
    assert sdk.media.closed and sdk.disconnected


@pytest.mark.parametrize("rate,channels", [(24000, 1), (16000, 2)])
def test_probe_rejects_unsupported_wav_before_connection(tmp_path, rate, channels):
    path = tmp_path / "speech.wav"
    write_wav(path, rate=rate, channels=channels)
    with pytest.raises(ValueError):
        load_probe_audio(path)
