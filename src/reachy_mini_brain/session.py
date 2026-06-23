"""Legacy persistent session for Reachy Mini.

Status: legacy/fallback. The accepted product path is
``reachy_mini_brain.official_runtime`` plus the m1max local S2S backend. Keep
this module runnable for regression/reference until legacy removal is explicitly
approved.

Holds a single ReachyMini() SDK instance alive, keeping all channels
(vision, audio, motion) warm and accessible through one unified API.
Eliminates the 30-60s WebRTC cold start between interactions.

Two modes:
  1. In-process:  Session() used directly in Python
  2. Background:  `python -m reachy_mini_brain.session serve` keeps the
     session alive as a Unix-socket server.  Send commands with:
       `python -m reachy_mini_brain.session call speak "Hello"`
     or programmatically with send_command("speak", "Hello").
"""

from __future__ import annotations

import collections
import json
import multiprocessing as mp
import os
import queue
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import click
import numpy as np

from reachy_mini_brain import robot

# Apply macOS GStreamer patch before any ReachyMini() is created.
from reachy_mini_brain.audio import _patch_bin_add_check, push_audio_realtime

_patch_bin_add_check()

SOCKET_PATH = "/tmp/reachy_mini_session.sock"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ARTIFACTS_DIR = _PROJECT_ROOT / "artifacts"


# ---------------------------------------------------------------------------
# Session (in-process)
# ---------------------------------------------------------------------------


class Session:
    """Persistent connection to Reachy Mini — all channels through one object."""

    def __init__(self, warmup_audio: bool = True, warmup_video: bool = True):
        self._warmup_audio = warmup_audio
        self._warmup_video = warmup_video
        self._mini = None
        self._push_lock = threading.Lock()
        self._speaking = False  # True during speak(); mic listener + vision pause on it
        self._speech_cache: dict = {}  # rendered audio for fixed lines (speak(cache=True))
        self._listen_thread: threading.Thread | None = None
        self._listen_stop: threading.Event | None = None
        self._listen_vad_enabled = False
        self._utterance_q = None
        self._transcript_q = None
        self._utterance_event_q: queue.Queue | None = None
        self._stt_proc: mp.Process | None = None
        self._listen_model = "medium"
        self._listen_language = "en"
        self._utterance_n = 0
        self._utterances_enqueued = 0
        self._utterances_dropped = 0
        self._transcripts_read = 0
        self._raw_audio_lock = threading.Lock()
        self._raw_audio_recording = False
        self._raw_audio_file = None
        self._raw_audio_meta = None
        self._raw_audio_path: Path | None = None
        self._raw_audio_meta_path: Path | None = None
        self._raw_audio_run_id: str | None = None
        self._raw_audio_started_ts: float | None = None
        self._raw_audio_samples = 0
        self._raw_audio_chunks = 0

    # --- Lifecycle ---

    def start(self) -> None:
        """Create SDK instance and warm up all channels."""
        from reachy_mini import ReachyMini

        robot.ensure_ready()
        robot._session_active = True

        print("  Creating SDK connection...", file=sys.stderr)
        self._mini = ReachyMini()

        if self._warmup_audio:
            print("  Warming up audio pipeline...", file=sys.stderr)
            if not self._wait_for_audio(timeout=60):
                print("  Warning: audio pipeline did not start", file=sys.stderr)
            else:
                time.sleep(1)  # let send chain finish (per conversation app)
                print("  Audio ready", file=sys.stderr)

        if self._warmup_video:
            print("  Warming up video pipeline...", file=sys.stderr)
            if not self._wait_for_video(timeout=60):
                print("  Warning: video pipeline did not start", file=sys.stderr)
            else:
                print("  Video ready", file=sys.stderr)

        print("  Session started", file=sys.stderr)

    def stop(self) -> None:
        """Graceful shutdown — close media pipelines and disconnect."""
        if self._mini is None:
            return
        # Stop recording/listening before tearing down the media pipeline.
        try:
            self.audio_record_stop()
        except Exception:
            pass
        try:
            self.listen_stop()
        except Exception:
            pass
        try:
            self._mini.media_manager.close()
        except Exception:
            pass
        try:
            self._mini.client.disconnect()
        except Exception:
            pass
        robot._session_active = False
        self._mini = None
        print("  Session stopped", file=sys.stderr)

    def status(self) -> dict:
        """Return health info."""
        return {
            "connected": self.is_connected,
            "audio_ready": self.is_audio_ready,
            "video_ready": self._mini is not None and self._mini.media.get_frame() is not None,
            "listening": self._listen_vad_enabled,
            "stt_worker_alive": self._stt_proc is not None and self._stt_proc.is_alive(),
            "utterances_enqueued": self._utterances_enqueued,
            "utterances_dropped": self._utterances_dropped,
            "transcripts_read": self._transcripts_read,
            "audio_recording": self._raw_audio_recording,
            "audio_record_path": str(self._raw_audio_path) if self._raw_audio_recording else None,
        }

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # --- Health ---

    @property
    def is_connected(self) -> bool:
        return self._mini is not None and self._mini.client.is_connected()

    @property
    def is_audio_ready(self) -> bool:
        if self._mini is None:
            return False
        appsrc = getattr(self._mini.media.audio, "_appsrc", None)
        return appsrc is not None

    # --- Vision ---

    def get_frame(self) -> np.ndarray | None:
        """Get the latest camera frame as a numpy array (BGR)."""
        self._check()
        return self._mini.media.get_frame()

    def take_photo(self, path: str = "") -> str:
        """Capture a frame and save as JPEG. Returns the path."""
        import cv2

        self._check()
        if not path:
            _ARTIFACTS_DIR.mkdir(exist_ok=True)
            path = str(_ARTIFACTS_DIR / "reachy_photo.jpg")
        frame = self._mini.media.get_frame()
        if frame is None:
            raise RuntimeError("No frame available from camera")
        cv2.imwrite(path, frame)
        return path

    # --- Audio ---

    def get_audio_sample(self) -> np.ndarray | None:
        """Get the latest audio sample from the robot mic."""
        self._check()
        return self._mini.media.get_audio_sample()

    def push_audio_sample(self, data: np.ndarray) -> None:
        """Push audio to the robot speaker (thread-safe)."""
        self._check()
        with self._push_lock:
            self._mini.media.push_audio_sample(data)

    def listen(
        self,
        duration: float = 5.0,
        model: str = "base",
        language: str = "en",
    ) -> str:
        """Record from robot mic and transcribe to text.

        Returns transcript string (empty if silence detected).
        """
        from reachy_mini_brain import stt

        self._check()

        chunks: list[np.ndarray] = []
        start = time.time()
        while time.time() - start < duration:
            sample = self._mini.media.get_audio_sample()
            if sample is not None:
                chunks.append(sample)
            time.sleep(0.01)

        if not chunks:
            return ""

        audio = np.concatenate(chunks)
        if audio.ndim > 1:
            audio = audio[:, 0]

        lang = None if language == "auto" else language
        return stt.transcribe_array(audio, sample_rate=16000, model_size=model, language=lang)

    def speak(self, text: str, voice: str = "en_US-lessac-medium", cache: bool = False) -> None:
        """Synthesize text and play through robot speaker. cache=True memoizes the rendered
        audio by text — use for FIXED lines (opener/greet/goodbye/wave) so they synthesize
        once and skip the synth on later plays; leave False for unique brain replies."""
        self._check()
        audio = self._render_speech(text, voice, cache)
        if audio.size == 0:
            return
        self._play_speech(audio)

    def _render_speech(self, text: str, voice: str, cache: bool):
        """text -> 16kHz mono float32 with the silence lead-in. Memoized when cache=True."""
        from reachy_mini_brain import tts

        key = (text, voice)
        if cache and key in self._speech_cache:
            return self._speech_cache[key]

        audio, sample_rate = tts.synthesize_array(text, voice=voice)
        if audio.size == 0:
            return audio
        # Resample to 16kHz
        if sample_rate != 16000:
            from scipy.signal import resample

            num_samples = int(len(audio) * 16000 / sample_rate)
            audio = resample(audio, num_samples).astype(np.float32)
        # Keep mono — MediaManager handles channel conversion
        if audio.ndim > 1:
            audio = audio[:, 0]
        # Prepend a short silence lead-in — the send chain swallows the first ~150ms
        # when it spins up, which was clipping the first syllable ("he" of "Hello").
        audio = np.concatenate([np.zeros(int(0.3 * 16000), dtype=np.float32), audio])
        if cache:
            self._speech_cache[key] = audio
        return audio

    def _play_speech(self, audio) -> None:
        # Flag playback so VAD ignores the robot's own voice and the vision worker
        # pauses RF-DETR. try/finally so a push error can't leave the system stuck
        # in speaking mode.
        self._speaking = True
        try:
            try:
                self._play_speech_file(audio)
            except Exception as e:  # noqa: BLE001
                print(f"  Warning: file playback failed, falling back to stream: {e}", file=sys.stderr)
                self._play_speech_stream(audio)
        finally:
            self._speaking = False

    def _play_speech_file(self, audio) -> None:
        """Play TTS via the robot daemon's local sound player, not WebRTC PCM streaming."""
        import tempfile

        import soundfile as sf

        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            sf.write(f.name, audio, 16000)
            self._mini.media.play_sound(f.name)
            time.sleep(len(audio) / 16000.0 + 0.4)

    def _play_speech_stream(self, audio) -> None:
        """Fallback: stream PCM through WebRTC appsrc."""
        push_audio_realtime(self.push_audio_sample, audio)
        time.sleep(0.5)

    def prerender(self, text: str, voice: str = "en_US-lessac-medium") -> None:
        """Synthesize + cache a FIXED line without playing it — call at startup to warm the
        speech cache so the first opener/greet/goodbye has no synth latency (cuts startup lag)."""
        self._render_speech(text, voice, cache=True)

    # --- Raw audio recording ---

    def audio_record_start(self, path: str | None = None, run_id: str | None = None) -> dict:
        """Start recording Cat-1 mic audio.

        Writes a 16 kHz mono float WAV plus a JSONL sidecar with wall-clock timestamps
        and sample offsets. The same mic loop also feeds VAD when listen_start() is
        active, so recording does not create a competing WebRTC audio reader.
        """
        self._check()
        with self._raw_audio_lock:
            if self._raw_audio_recording:
                return self._raw_audio_summary(running=True)

            if path is None:
                _ARTIFACTS_DIR.mkdir(exist_ok=True)
                path_obj = _ARTIFACTS_DIR / f"audio-{time.strftime('%H%M%S')}.wav"
            else:
                path_obj = Path(path)
                path_obj.parent.mkdir(parents=True, exist_ok=True)
            meta_path = path_obj.with_suffix(".jsonl")

            import soundfile as sf

            self._raw_audio_file = sf.SoundFile(
                str(path_obj), mode="w", samplerate=16000, channels=1, subtype="FLOAT"
            )
            self._raw_audio_meta = meta_path.open("w", encoding="utf-8")
            self._raw_audio_path = path_obj
            self._raw_audio_meta_path = meta_path
            self._raw_audio_run_id = run_id
            self._raw_audio_started_ts = time.time()
            self._raw_audio_samples = 0
            self._raw_audio_chunks = 0
            self._raw_audio_recording = True
            self._raw_audio_meta.write(json.dumps({
                "type": "start",
                "ts": round(self._raw_audio_started_ts, 3),
                "sample_rate": 16000,
                "channels": 1,
                "format": "wav-float32",
                "path": str(path_obj),
                "run_id": run_id,
            }) + "\n")
            self._raw_audio_meta.flush()

        self._ensure_listen_thread()
        return self._raw_audio_summary(running=True)

    def audio_record_stop(self) -> dict:
        """Stop Cat-1 mic recording and close the WAV/sidecar."""
        with self._raw_audio_lock:
            if not self._raw_audio_recording:
                return self._raw_audio_summary(running=False)

            summary = self._raw_audio_summary(running=False)
            if self._raw_audio_meta is not None:
                self._raw_audio_meta.write(json.dumps({
                    "type": "stop",
                    "ts": round(time.time(), 3),
                    "run_id": self._raw_audio_run_id,
                    "sample_end": self._raw_audio_samples,
                    "duration": round(self._raw_audio_samples / 16000.0, 2),
                }) + "\n")
                self._raw_audio_meta.close()
            if self._raw_audio_file is not None:
                self._raw_audio_file.close()

            self._raw_audio_recording = False
            self._raw_audio_file = None
            self._raw_audio_meta = None

        if not self._listen_vad_enabled:
            self._stop_listen_thread()
        return summary

    def _raw_audio_summary(self, running: bool) -> dict:
        return {
            "recording": running,
            "path": str(self._raw_audio_path) if self._raw_audio_path else None,
            "metadata": str(self._raw_audio_meta_path) if self._raw_audio_meta_path else None,
            "samples": self._raw_audio_samples,
            "duration": round(self._raw_audio_samples / 16000.0, 2),
            "chunks": self._raw_audio_chunks,
            "run_id": self._raw_audio_run_id,
        }

    def _write_raw_audio(self, sample, *, speaking: bool) -> None:
        """Append one mic sample chunk to the raw-audio WAV and timestamp sidecar."""
        if not self._raw_audio_recording:
            return
        audio = np.asarray(sample, dtype=np.float32)
        audio = audio[:, 0] if audio.ndim > 1 else audio.reshape(-1)
        with self._raw_audio_lock:
            if not self._raw_audio_recording or self._raw_audio_file is None:
                return
            sample_start = self._raw_audio_samples
            self._raw_audio_file.write(audio)
            self._raw_audio_samples += len(audio)
            self._raw_audio_chunks += 1
            if self._raw_audio_meta is not None:
                rec = {
                    "type": "chunk",
                    "ts": round(time.time(), 3),
                    "run_id": self._raw_audio_run_id,
                    "sample_start": sample_start,
                    "samples": int(len(audio)),
                    "speaking": bool(speaking),
                    "rms": round(float(np.sqrt(np.mean(audio**2))) if len(audio) else 0.0, 5),
                }
                self._raw_audio_meta.write(json.dumps(rec) + "\n")
                self._raw_audio_meta.flush()

    # --- Continuous listening ---

    def listen_start(self, model: str = "medium", language: str = "en") -> str:
        """Start continuous VAD-endpointed listening.

        A daemon thread runs Silero VAD over the mic stream and emits ONE COMPLETE
        utterance (speech-start..speech-end) at a time to an STT worker process. Call
        listen_read() to pull the next completed transcript record.
        """
        self._check()
        if self._listen_vad_enabled:
            return "already listening"

        from silero_vad import load_silero_vad, VADIterator

        vad_iter = VADIterator(
            load_silero_vad(onnx=True), sampling_rate=16000,
            threshold=0.5, min_silence_duration_ms=250,
        )

        ctx = mp.get_context("spawn")
        self._utterance_q = ctx.Queue(maxsize=128)
        self._transcript_q = ctx.Queue(maxsize=128)
        self._utterance_event_q = queue.Queue(maxsize=128)
        self._listen_model = model
        self._listen_language = language
        self._utterance_n = 0
        self._utterances_enqueued = 0
        self._utterances_dropped = 0
        self._transcripts_read = 0

        from reachy_mini_brain.stt_worker import stt_worker_main

        self._stt_proc = ctx.Process(
            target=stt_worker_main,
            args=(self._utterance_q, self._transcript_q, model, language),
            name="reachy-stt",
            daemon=True,
        )
        self._stt_proc.start()
        self._vad_iter = vad_iter
        self._listen_vad_enabled = True
        self._ensure_listen_thread()
        return "listening"

    def listen_read(self, timeout: float = 1.0) -> dict:
        """Block up to `timeout`s for the next transcript record.

        Returns the legacy {"text": str, "buffer_duration": float} fields plus timing
        metadata: utterance_id, speech_start_ts, speech_end_ts, queued_ts, stt_start_ts,
        stt_done_ts, stt_latency, model, language, and audio. Empty text with no
        utterance_id means no transcript arrived within the timeout.
        """
        if self._transcript_q is None:
            return {"text": "", "buffer_duration": 0.0}
        try:
            rec = self._transcript_q.get(timeout=timeout)
        except queue.Empty:
            return {"text": "", "buffer_duration": 0.0}
        self._transcripts_read += 1
        return rec

    def listen_stop(self) -> str:
        """Stop continuous background listening."""
        self._listen_vad_enabled = False
        self._stop_stt_worker()
        self._utterance_event_q = None
        if not self._raw_audio_recording:
            self._stop_listen_thread()
        return "stopped"

    def listen_activity_read(self, timeout: float = 0.0) -> dict:
        """Read the next lightweight listener activity event.

        These events are emitted when VAD queues an utterance for STT, before
        transcription is finished. They let the UI/gesture layer acknowledge that
        the robot heard a completed turn without waiting for STT latency.
        """
        q = self._utterance_event_q
        if q is None:
            return {}
        try:
            return q.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return {}

    def _stop_stt_worker(self) -> None:
        """Stop the STT worker process and release its queues."""
        proc = self._stt_proc
        in_q = self._utterance_q
        if proc is not None:
            try:
                if in_q is not None:
                    in_q.put_nowait(None)
            except Exception:  # noqa: BLE001
                pass
            proc.join(timeout=3)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2)
        for q in (self._utterance_q, self._transcript_q):
            try:
                if q is not None:
                    q.cancel_join_thread()
                    q.close()
            except Exception:  # noqa: BLE001
                pass
        self._stt_proc = None
        self._utterance_q = None
        self._transcript_q = None

    def _enqueue_utterance(self, windows: list[tuple[np.ndarray, float, float]],
                           *, reason: str) -> None:
        """Queue one VAD-endpointed utterance for STT without blocking mic capture."""
        if not windows or self._utterance_q is None:
            return
        audio = np.concatenate([w[0] for w in windows]).astype(np.float32, copy=False)
        if audio.size == 0:
            return

        self._utterance_n += 1
        speech_start_ts = windows[0][1]
        speech_end_ts = windows[-1][2]
        rec = {
            "utterance_id": self._utterance_n,
            "speech_start_ts": round(speech_start_ts, 3),
            "speech_end_ts": round(speech_end_ts, 3),
            "queued_ts": round(time.time(), 3),
            "buffer_duration": round(len(audio) / 16000.0, 2),
            "audio": audio,
            "vad_reason": reason,
        }
        try:
            self._utterance_q.put_nowait(rec)
            self._utterances_enqueued += 1
            event_q = self._utterance_event_q
            if event_q is not None:
                try:
                    event_q.put_nowait({
                        "type": "utterance_queued",
                        "utterance_id": rec["utterance_id"],
                        "speech_start_ts": rec["speech_start_ts"],
                        "speech_end_ts": rec["speech_end_ts"],
                        "queued_ts": rec["queued_ts"],
                        "buffer_duration": rec["buffer_duration"],
                        "vad_reason": rec["vad_reason"],
                    })
                except queue.Full:
                    pass
        except queue.Full:
            # Backpressure must not block the media/VAD loop. The counters make this
            # visible in status/logs; a future policy can persist dropped audio first.
            self._utterances_dropped += 1
        except Exception:  # noqa: BLE001
            self._utterances_dropped += 1

    def _ensure_listen_thread(self) -> None:
        """Start the shared mic loop if it is not already running."""
        if self._listen_thread is not None and self._listen_thread.is_alive():
            return
        self._listen_stop = threading.Event()
        self._listen_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._listen_thread.start()

    def _stop_listen_thread(self) -> None:
        """Stop the shared mic loop when neither VAD nor raw recording needs it."""
        if self._listen_stop is not None:
            self._listen_stop.set()
        if self._listen_thread is not None:
            self._listen_thread.join(timeout=10)
        self._listen_thread = None
        self._listen_stop = None

    def _listen_loop(self) -> None:
        """Background thread: raw mic fanout + optional Silero-VAD endpointer.

        Feeds the mic stream to VADIterator in 512-sample (32ms) windows, collects audio
        from speech-start to speech-end (with a short pre-roll so onsets aren't clipped),
        and puts each COMPLETE utterance on _utterance_q. Raw recording sees the mic
        chunks before VAD/STT interpretation and records robot-speaking chunks with a
        metadata flag. VAD still resets while the robot speaks (no self-hearing).
        """
        WIN = 512                              # Silero window @ 16kHz
        PREROLL = 8                            # ~256ms kept before speech start
        MIN_WINS = 8                           # ignore blips < ~0.25s
        MAX_WINS = int(15 * 16000 / WIN)       # force-emit after ~15s of continuous speech
        pre: collections.deque = collections.deque(maxlen=PREROLL)
        utt: list[tuple[np.ndarray, float, float]] = []
        collecting = False
        pending = np.zeros(0, dtype=np.float32)
        pending_start_ts: float | None = None
        while self._listen_stop is not None and not self._listen_stop.is_set():
            sample = self._mini.media.get_audio_sample()
            if sample is None:
                time.sleep(0.01)
                continue

            speaking = bool(self._speaking)
            self._write_raw_audio(sample, speaking=speaking)

            if speaking:                       # robot talking: reset VAD state
                if self._listen_vad_enabled:
                    self._vad_iter.reset_states()
                collecting, utt = False, []
                pending = np.zeros(0, dtype=np.float32)
                pending_start_ts = None
                pre.clear()
                time.sleep(0.02)
                continue

            if not self._listen_vad_enabled:
                collecting, utt = False, []
                pending = np.zeros(0, dtype=np.float32)
                pending_start_ts = None
                pre.clear()
                time.sleep(0.003)
                continue

            s = np.asarray(sample, dtype=np.float32)
            s = s[:, 0] if s.ndim > 1 else s.reshape(-1)
            sample_end_ts = time.time()
            sample_start_ts = sample_end_ts - (len(s) / 16000.0)
            if pending_start_ts is None:
                pending_start_ts = sample_start_ts
            pending = np.concatenate([pending, s])
            while len(pending) >= WIN:
                w = pending[:WIN]
                pending = pending[WIN:]
                w_start_ts = pending_start_ts if pending_start_ts is not None else time.time()
                w_end_ts = w_start_ts + (WIN / 16000.0)
                pending_start_ts = w_end_ts if len(pending) else None
                win = (w, w_start_ts, w_end_ts)
                try:
                    res = self._vad_iter(w, return_seconds=False)
                except Exception:  # noqa: BLE001
                    res = None
                if res and "start" in res:
                    collecting = True
                    utt = list(pre) + [win]
                    pre.clear()
                elif res and "end" in res:
                    if collecting:
                        utt.append(win)
                        if len(utt) >= MIN_WINS:
                            self._enqueue_utterance(utt, reason="vad_end")
                    collecting, utt = False, []
                elif collecting:
                    utt.append(win)
                    if len(utt) >= MAX_WINS:   # runaway cap
                        self._enqueue_utterance(utt, reason="max_duration")
                        collecting, utt = False, []
                        self._vad_iter.reset_states()
                else:
                    pre.append(win)
            time.sleep(0.003)

    # --- Motion ---

    def move_head(
        self,
        pitch: float = 0.0,
        roll: float = 0.0,
        yaw: float = 0.0,
        duration: float = 1.0,
    ) -> None:
        """Move head to target orientation (degrees)."""
        robot.goto(pitch=pitch, roll=roll, yaw=yaw, duration=duration)

    def set_target(self, **kwargs) -> None:
        """Set pose immediately (no interpolation). Angles in degrees."""
        robot.set_target(**kwargs)

    def look(self, direction: str) -> None:
        """Look in a preset direction: left, right, up, down, center."""
        presets = {
            "left": dict(yaw=30),
            "right": dict(yaw=-30),
            "up": dict(pitch=-20),
            "down": dict(pitch=20),
            "center": dict(),
        }
        if direction not in presets:
            raise ValueError(f"Unknown direction: {direction}")
        robot.goto(**presets[direction], duration=0.8)

    def nod(self) -> None:
        """Nod the head (yes gesture)."""
        for _ in range(2):
            robot.goto(pitch=15, duration=0.3)
            robot.goto(pitch=0, duration=0.3)

    def shake(self) -> None:
        """Shake the head (no gesture)."""
        robot.goto(yaw=20, duration=0.3)
        robot.goto(yaw=-20, duration=0.3)
        robot.goto(yaw=20, duration=0.3)
        robot.goto(yaw=0, duration=0.3)

    def antennas(self, left: float, right: float) -> None:
        """Set antenna positions (degrees). Positive = up."""
        robot.set_target(antennas=(left, right))

    def rotate_body(self, angle: float, duration: float = 1.0) -> None:
        """Rotate body to angle (degrees)."""
        robot.goto(body_yaw=angle, duration=duration)

    # --- State ---

    def get_state(self) -> dict:
        """Get full robot state."""
        return robot.get_state()

    # --- Internal ---

    def _check(self) -> None:
        if self._mini is None:
            raise RuntimeError("Session not started — call start() first")

    def _wait_for_audio(self, timeout: float = 60.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            sample = self._mini.media.get_audio_sample()
            if sample is not None and sample.size > 0:
                return True
            time.sleep(0.5)
        return False

    def _wait_for_video(self, timeout: float = 60.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            frame = self._mini.media.get_frame()
            if frame is not None:
                return True
            time.sleep(1)
        return False


# ---------------------------------------------------------------------------
# Session server — keeps Session alive, accepts commands over Unix socket
# ---------------------------------------------------------------------------

# Methods safe to call remotely (no numpy args/returns needed).
_REMOTE_METHODS = {
    "speak", "listen", "nod", "shake", "look", "take_photo",
    "move_head", "rotate_body", "antennas", "get_state", "status",
    "listen_start", "listen_read", "listen_stop",
    "audio_record_start", "audio_record_stop",
}

# Aliases for convenience
_METHOD_ALIASES = {
    "health": "status",
}


def _handle_connection(session: Session, conn: socket.socket) -> bool:
    """Handle one client connection. Returns False if server should stop."""
    try:
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            # Messages are newline-terminated JSON
            if b"\n" in data:
                break
        if not data:
            return True

        msg = json.loads(data.decode().strip())
        method = msg.get("method", "")
        args = msg.get("args", [])
        kwargs = msg.get("kwargs", {})

        if method == "stop":
            _send(conn, {"ok": True, "result": "stopping"})
            return False

        method = _METHOD_ALIASES.get(method, method)

        if method not in _REMOTE_METHODS:
            _send(conn, {"ok": False, "error": f"unknown method: {method}"})
            return True

        fn = getattr(session, method)
        result = fn(*args, **kwargs)

        result = _json_safe(result)
        _send(conn, {"ok": True, "result": result})

    except Exception as e:
        try:
            _send(conn, {"ok": False, "error": str(e)})
        except Exception:
            pass
    finally:
        conn.close()
    return True


def _send(conn: socket.socket, obj: dict) -> None:
    conn.sendall((json.dumps(obj) + "\n").encode())


def _json_safe(value):
    """Convert nested numpy values to lightweight JSON-safe placeholders."""
    if isinstance(value, np.ndarray):
        return f"<ndarray shape={value.shape} dtype={value.dtype}>"
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def serve_session() -> None:
    """Start a persistent session server on a Unix socket."""
    # Clean up stale socket
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    session = Session()
    session.start()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(1)
    server.settimeout(1.0)

    running = True

    def _sighandler(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _sighandler)
    signal.signal(signal.SIGTERM, _sighandler)

    print(f"Session server listening on {SOCKET_PATH}", file=sys.stderr)
    print("Send commands with: python -m reachy_mini_brain.session call <method> [args...]", file=sys.stderr)

    while running:
        try:
            conn, _ = server.accept()
            if not _handle_connection(session, conn):
                running = False
        except socket.timeout:
            continue
        except OSError:
            break

    session.stop()
    server.close()
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    print("Session server shut down", file=sys.stderr)


# ---------------------------------------------------------------------------
# Client — send a command to the running session server
# ---------------------------------------------------------------------------


def send_command(method: str, *args, timeout: float = 120.0, **kwargs) -> dict:
    """Send a command to the running session server.

    Returns {"ok": bool, "result": ..., "error": ...}.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(SOCKET_PATH)
    msg = json.dumps({"method": method, "args": list(args), "kwargs": kwargs}) + "\n"
    sock.sendall(msg.encode())

    data = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
        if b"\n" in data:
            break
    sock.close()
    return json.loads(data.decode().strip())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """Reachy Mini persistent session."""
    pass


@cli.command()
def serve():
    """Start the persistent session server (blocks until stopped)."""
    serve_session()


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("method")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def call(method, args):
    """Send a command to the running session server.

    Examples:
        call speak "Hello world"
        call listen 5
        call nod
        call look left
        call take_photo artifacts/pic.jpg
        call stop
    """
    parsed = []
    for a in args:
        try:
            parsed.append(float(a))
            # Keep as int if it's a whole number
            if parsed[-1] == int(parsed[-1]):
                parsed[-1] = int(parsed[-1])
        except ValueError:
            parsed.append(a)

    try:
        result = send_command(method, *parsed)
    except FileNotFoundError:
        click.echo("Error: session server not running. Start with: python -m reachy_mini_brain.session serve", err=True)
        raise SystemExit(1)
    except ConnectionRefusedError:
        click.echo("Error: session server not responding", err=True)
        raise SystemExit(1)

    if result.get("ok"):
        r = result.get("result")
        if r is not None:
            if isinstance(r, dict):
                click.echo(json.dumps(r, indent=2))
            else:
                click.echo(r)
    else:
        click.echo(f"Error: {result.get('error')}", err=True)
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
