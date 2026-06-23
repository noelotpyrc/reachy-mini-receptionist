"""Legacy reception daemon — Phase A: control plane + lifecycle.

Status: legacy/fallback. The accepted product path is
``reachy_mini_brain.official_runtime`` plus the m1max local S2S backend. Keep
this module runnable for regression/reference until legacy removal is explicitly
approved.

A resident process that owns one live hardware Session and supervises two
INDEPENDENT worker loops, each gated by its own toggle:

  - vision : grab a frame every N seconds  (stub — detector/VLM land in Phase B)
  - voice  : continuous-listen buffer + periodic read
             (stub — agentic brain lands in Phase C)

This is the piece that replaces "Claude Code drives the robot": the daemon
stays alive, holds the WebRTC session warm, and flips vision/voice on and off
without tearing down the shared connection.

Control surface (Unix socket, same newline-JSON protocol as session.py):

    reception serve [--mock]      # run the daemon (blocks)
    reception status
    reception vision on|off
    reception voice  on|off
    reception shutdown

`--mock` swaps in a fake session (no SDK / no robot) so the state machine,
socket protocol, and lifecycle can be exercised on a dev machine. The real
Session is imported lazily and only when serving for real, so the client
commands and the mock never pull in the SDK.

Top-level imports are stdlib + click ONLY — keep it that way so this module
loads without the SDK present.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import click

SOCKET_PATH = "/tmp/reachy_mini_reception.sock"
ARTIFACTS = Path(__file__).resolve().parent.parent.parent / "artifacts"
DEFAULT_STT_MODEL = "medium"
DEFAULT_STT_LANGUAGE = "en"
DEFAULT_BATCH_MAX_WAIT = 1.5
BATCH_WAIT_POLL = 0.1

# Resting antenna pose (left, right) in degrees. Not (0, 0): the right antenna has a
# mechanical issue at zero, so home is a gentle symmetric outward tilt. Tuned live via
# the `antennas` command; used by reset() and every gesture's return-to-rest.
NEUTRAL_ANTENNAS = (-15.0, -15.0)

log = logging.getLogger("reception")


# ---------------------------------------------------------------------------
# Mock session — stand-in for Session on a machine with no robot / no SDK
# ---------------------------------------------------------------------------


class _FakeFrame:
    """Minimal frame stand-in: just enough for `frame.shape` / truthiness."""

    def __init__(self, shape):
        self.shape = shape


class MockSession:
    """Logs instead of touching hardware. Mirrors the Session methods the
    reception workers use: start/stop/status, get_frame, listen_start/read/stop.
    """

    def __init__(self):
        self._reads = 0
        self._audio_record_path = "artifacts/audio-mock.wav"
        self._audio_record_meta = "artifacts/audio-mock.jsonl"
        self._audio_record_run_id = None

    # lifecycle
    def start(self):
        log.info("mock session: start")

    def stop(self):
        log.info("mock session: stop")

    def status(self):
        return {"mock": True, "connected": True}

    # vision
    def get_frame(self):
        return _FakeFrame((480, 640, 3))

    # voice
    def listen_start(self, model="base", language="en"):
        log.info("mock session: listen_start")
        return "listening"

    def listen_read(self, timeout: float = 1.0):
        if timeout <= 0:
            return {"text": "", "buffer_duration": 0.0}
        # Emit fake speech every 3rd read so the text path is visible.
        time.sleep(min(timeout, 0.5))
        self._reads += 1
        if self._reads % 3 == 0:
            import numpy as np

            now = time.time()
            return {
                "utterance_id": self._reads // 3,
                "speech_start_ts": round(now - 2.4, 3),
                "speech_end_ts": round(now - 0.4, 3),
                "queued_ts": round(now - 0.35, 3),
                "stt_start_ts": round(now - 0.25, 3),
                "stt_done_ts": round(now, 3),
                "stt_latency": 0.25,
                "model": "mock",
                "language": "en",
                "text": "hello reachy",
                "buffer_duration": 2.0,
                "audio": np.zeros(int(2.0 * 16000), dtype=np.float32),
            }
        return {"text": "", "buffer_duration": 2.0}

    def listen_stop(self):
        log.info("mock session: listen_stop")
        return "stopped"

    def audio_record_start(self, path=None, run_id=None):
        log.info("mock session: audio_record_start")
        if path:
            self._audio_record_path = path
            self._audio_record_meta = str(Path(path).with_suffix(".jsonl"))
        self._audio_record_run_id = run_id
        return {
            "recording": True,
            "path": self._audio_record_path,
            "metadata": self._audio_record_meta,
            "samples": 0,
            "duration": 0.0,
            "chunks": 0,
            "run_id": self._audio_record_run_id,
        }

    def audio_record_stop(self):
        log.info("mock session: audio_record_stop")
        return {
            "recording": False,
            "path": self._audio_record_path,
            "metadata": self._audio_record_meta,
            "samples": 0,
            "duration": 0.0,
            "chunks": 0,
            "run_id": self._audio_record_run_id,
        }

    # react actions (greeting)
    def speak(self, text, voice="en_US-lessac-medium"):
        log.info("mock session: speak %r", text)

    def prerender(self, text, voice="en_US-lessac-medium"):
        log.info("mock session: prerender %r", text)

    def look(self, direction):
        log.info("mock session: look %s", direction)

    def antennas(self, left, right):
        log.info("mock session: antennas (%s, %s)", left, right)

    def move_head(self, pitch=0.0, roll=0.0, yaw=0.0, duration=1.0):
        log.info("mock session: move_head pitch=%s roll=%s yaw=%s", pitch, roll, yaw)

    def rotate_body(self, angle, duration=1.0):
        log.info("mock session: rotate_body %s", angle)


# ---------------------------------------------------------------------------
# Reception daemon — the state machine
# ---------------------------------------------------------------------------


class ReceptionDaemon:
    """Owns one session and two independent, toggleable worker threads.

    Each toggle starts/stops ONLY its own worker; neither touches the other
    or the shared session lifecycle. Toggle operations are idempotent.
    """

    def __init__(self, session, vision_interval: float = 0.2,
                 voice_interval: float = 1.5, perception: bool = False,
                 threshold: float = 0.5, gestures: bool = False,
                 greeting: str = "Welcome!",
                 farewell: str = "Goodbye! Have a nice day!",
                 wave_message: str = "Hi there!",
                 conversation_opener: str = "Hi! How can I help?",
                 conv_idle_timeout: float = 45.0, conv_max_duration: float = 480.0,
                 brain: bool = False, brain_model: str = "sonnet",
                 brain_backend: str = "claude", save_turns: bool = False,
                 stt_model: str = DEFAULT_STT_MODEL,
                 stt_language: str = DEFAULT_STT_LANGUAGE,
                 batch_max_wait: float = DEFAULT_BATCH_MAX_WAIT,
                 run_id: str | None = None, log_path: Path | None = None):
        self._run_id = run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self._log_path = Path(log_path) if log_path else None
        self._session = session
        self._vision_interval = vision_interval
        self._voice_interval = voice_interval
        self._perception_enabled = perception
        self._threshold = threshold
        self._gestures = gestures
        self._greeting = greeting
        self._farewell = farewell
        self._wave_message = wave_message
        self._conversation_opener = conversation_opener
        self._conv_idle_timeout = conv_idle_timeout
        self._conv_max_duration = conv_max_duration
        self._conversation_mode = False
        self._brain_enabled = brain
        self._brain_backend = brain_backend
        self._brain_model_requested = brain_model
        self._brain_model = self._resolve_brain_model(brain_backend, brain_model)
        self._save_turns = save_turns
        self._stt_model = stt_model
        self._stt_language = stt_language
        self._batch_max_wait = max(float(batch_max_wait), 0.0)
        self._turns_jsonl = None
        self._turns_manifest_idx: int | None = None
        self._turn_n = 0
        self._utterances_jsonl = None
        self._utterances_manifest_idx: int | None = None
        self._transcripts_jsonl = None
        self._transcripts_manifest_idx: int | None = None
        self._transcript_n = 0
        self._lock = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._artifact_counts: dict[str, int] = {}
        self._manifest_path = ARTIFACTS / "runs" / f"run-{self._run_id}.json"
        self._manifest = {
            "run_id": self._run_id,
            "started_ts": round(time.time(), 3),
            "pid": os.getpid(),
            "config": {
                "vision_interval": self._vision_interval,
                "voice_interval": self._voice_interval,
                "perception": self._perception_enabled,
                "threshold": self._threshold,
                "gestures": self._gestures,
                "brain": self._brain_enabled,
                "brain_model": self._brain_model,
                "brain_model_requested": self._brain_model_requested,
                "brain_backend": self._brain_backend,
                "save_turns": self._save_turns,
                "stt_model": self._stt_model,
                "stt_language": self._stt_language,
                "batch_max_wait": self._batch_max_wait,
            },
            "artifacts": {
                "log": ([{"path": str(self._log_path)}] if self._log_path else []),
                "events": [{
                    "path": str(ARTIFACTS / "events.jsonl"),
                    "run_id_field": True,
                    "mode": "append",
                }],
                "video": [],
                "capture": [],
                "audio": [],
                "utterances": [],
                "transcripts": [],
                "turns": [],
            },
        }

        self._vision_thread: threading.Thread | None = None
        self._vision_stop: threading.Event | None = None
        self._voice_thread: threading.Thread | None = None
        self._voice_stop: threading.Event | None = None

        # debug capture (records per-frame vision data for a manual test run)
        self._capturing = False
        self._capture_path: Path | None = None
        self._capture_frames = 0
        self._capture_events = 0
        self._capture_manifest_idx: int | None = None

        # video recording (persist the camera frames the vision worker grabs)
        self._recording = False
        self._record_path: Path | None = None
        self._record_writer = None
        self._record_frames = 0
        self._record_manifest_idx: int | None = None

        # raw audio recording (Cat-1 mic signal, owned by Session)
        self._audio_record_manifest_idx: int | None = None

        # live MJPEG stream of the vision worker's frames (view via an ssh tunnel)
        self._streaming = False
        self._latest_frame = None
        self._stream_server = None

        self._write_manifest()

    # --- run manifest ---

    @staticmethod
    def _resolve_brain_model(backend: str, requested: str) -> str:
        """Return the actual model used by the selected brain backend."""
        if backend == "pydantic":
            from reachy_mini_brain.brain import default_openrouter_model

            return default_openrouter_model()
        return requested

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    def _artifact_path(self, kind: str, suffix: str, *, directory: Path | None = None) -> Path:
        """Return a per-run artifact path with a stable counter: kind-run_id-01.ext."""
        self._artifact_counts[kind] = self._artifact_counts.get(kind, 0) + 1
        root = directory or ARTIFACTS
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{kind}-{self._run_id}-{self._artifact_counts[kind]:02d}{suffix}"

    def _write_manifest(self) -> None:
        """Persist the manifest; callers hold no lock so this can be used from workers."""
        with self._manifest_lock:
            self._manifest["updated_ts"] = round(time.time(), 3)
            self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._manifest, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
            tmp.replace(self._manifest_path)

    def _manifest_add_artifact(self, kind: str, **fields) -> int:
        with self._manifest_lock:
            items = self._manifest["artifacts"].setdefault(kind, [])
            rec = {"started_ts": round(time.time(), 3), **fields}
            items.append(rec)
            idx = len(items) - 1
        self._write_manifest()
        return idx

    def _manifest_update_artifact(self, kind: str, idx: int | None, **fields) -> None:
        if idx is None:
            return
        with self._manifest_lock:
            items = self._manifest["artifacts"].setdefault(kind, [])
            if idx >= len(items):
                return
            items[idx].update(fields)
            items[idx]["updated_ts"] = round(time.time(), 3)
        self._write_manifest()

    # --- lifecycle ---

    def start(self):
        log.info("run_id=%s manifest -> %s", self._run_id, self._manifest_path)
        self._session.start()
        # Warm the speech cache for the fixed lines so the first opener/greet/goodbye/wave has
        # no synthesis latency — cuts the wave->reaction startup lag.
        for line in (self._greeting, self._farewell, self._wave_message, self._conversation_opener):
            try:
                self._session.prerender(line)
            except Exception as e:  # noqa: BLE001
                log.warning("prerender failed: %s", e)

    def stop(self):
        """Stop both workers, then the session. Workers first so they never
        call into a torn-down session. Finalize record/capture AFTER the vision
        thread is joined (so no frame is mid-write) — a graceful shutdown must
        release the VideoWriter, or the mp4 is left unfinalized and unreadable."""
        self.vision_off()
        self.voice_off()
        self.audio_record_off()
        self.record_off()
        self.capture_off()
        self._manifest_update_artifact(
            "turns", self._turns_manifest_idx, status="closed",
            ended_ts=round(time.time(), 3), turns=self._turn_n,
        )
        self._manifest_update_artifact(
            "utterances", self._utterances_manifest_idx, status="closed",
            ended_ts=round(time.time(), 3), utterances=self._transcript_n,
        )
        self._manifest_update_artifact(
            "transcripts", self._transcripts_manifest_idx, status="closed",
            ended_ts=round(time.time(), 3), transcripts=self._transcript_n,
        )
        self._session.stop()
        self._manifest["ended_ts"] = round(time.time(), 3)
        self._write_manifest()

    # --- vision toggle ---

    def vision_on(self) -> str:
        with self._lock:
            if _alive(self._vision_thread):
                return "vision already on"
            self._vision_stop = threading.Event()
            self._vision_thread = threading.Thread(
                target=self._vision_loop, args=(self._vision_stop,),
                name="vision", daemon=True,
            )
            self._vision_thread.start()
            return "vision on"

    def vision_off(self) -> str:
        with self._lock:
            if not _alive(self._vision_thread):
                return "vision already off"
            self._vision_stop.set()
            t = self._vision_thread
        t.join(timeout=10)
        return "vision off"

    def _vision_loop(self, stop: threading.Event):
        log.info("vision: worker started (interval=%ss, perception=%s)",
                 self._vision_interval, self._perception_enabled)
        pipe = self._make_perception() if self._perception_enabled else None
        while not stop.is_set():
            # Pause perception while the robot speaks — RF-DETR contends with the audio
            # push thread (CPU/GIL) and makes speech choppy. Vision idles for the few
            # seconds of a greeting/reply, then resumes. (First pass; the proper fix is
            # to run perception in its own OS process so it never has to pause.)
            if getattr(self._session, "_speaking", False):
                stop.wait(0.1)
                continue
            try:
                frame = self._session.get_frame()
                if frame is None:
                    log.info("vision: no frame yet")
                else:
                    self._latest_frame = frame  # published for the MJPEG stream
                    if self._recording:
                        self._write_video(frame)
                    if pipe is not None and hasattr(frame, "ndim"):
                        events, n, tracks = pipe.process(frame, bgr=True)
                        if events:
                            log.info("vision: %d person(s) | APPROACH %s", n, events)
                        else:
                            log.info("vision: %d person(s)", n)
                        if self._capturing:
                            self._write_capture(n, tracks, events)
                    else:
                        log.info("vision: frame ok %s", tuple(frame.shape))
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                log.warning("vision: error %s", e)
            stop.wait(self._vision_interval)
        log.info("vision: worker stopped")

    def _make_perception(self):
        try:
            from reachy_mini_brain.perception import PerceptionPipeline

            log.info("vision: loading perception (RF-DETR)...")
            p = PerceptionPipeline(
                threshold=self._threshold,
                gestures=self._gestures,
                run_id=self._run_id,
            )
            log.info("vision: perception ready")
            return p
        except Exception as e:  # noqa: BLE001
            log.warning("vision: perception unavailable (%s) — frame-log only", e)
            return None

    # --- capture toggle (debug: record per-frame vision data for a test run) ---

    def capture_on(self) -> str:
        """Start recording every vision frame's tracks/decisions to a fresh file."""
        with self._lock:
            self._capture_path = self._artifact_path("capture", ".jsonl")
            self._capture_path.parent.mkdir(parents=True, exist_ok=True)
            self._capture_path.write_text("")
            self._capture_frames = 0
            self._capture_events = 0
            self._capturing = True
            self._capture_manifest_idx = self._manifest_add_artifact(
                "capture", path=str(self._capture_path), status="open"
            )
        log.info("capture: started -> %s", self._capture_path)
        return f"capturing -> {self._capture_path}"

    def capture_off(self) -> dict:
        """Stop recording; return where the file is and what it caught."""
        with self._lock:
            self._capturing = False
            summary = {
                "path": str(self._capture_path) if self._capture_path else None,
                "frames": self._capture_frames,
                "events": self._capture_events,
            }
        log.info("capture: stopped (%s frames, %s events)",
                 summary["frames"], summary["events"])
        self._manifest_update_artifact(
            "capture", self._capture_manifest_idx, status="closed",
            ended_ts=round(time.time(), 3), frames=summary["frames"],
            events=summary["events"],
        )
        self._capture_manifest_idx = None
        return summary

    def _write_capture(self, n: int, tracks: list, events: list):
        rec = {
            "run_id": self._run_id,
            "ts": round(time.time(), 2),
            "n": n,
            "tracks": tracks,
            "events": events,
        }
        try:
            with open(self._capture_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            self._capture_frames += 1
            self._capture_events += len(events)
        except Exception as e:  # noqa: BLE001
            log.warning("capture: write error %s", e)

    # --- record toggle (persist the camera frames to an mp4) ---

    def record_on(self) -> str:
        """Record the frames the vision worker grabs to an mkv (needs vision on).
        Matroska (not mp4) so a hard kill / battery-off keeps the footage up to the
        crash — mp4 needs a trailing moov index written at release() and is otherwise
        unreadable. Same mp4v codec, same size. Frame rate follows --vision-interval."""
        with self._lock:
            if self._recording:  # don't clobber an in-progress recording's writer
                return f"already recording -> {self._record_path} ({self._record_frames} frames so far)"
            self._record_path = self._artifact_path("video", ".mkv")
            self._record_path.parent.mkdir(parents=True, exist_ok=True)
            self._record_writer = None  # lazy-created on first frame (needs w/h)
            self._record_frames = 0
            self._recording = True
            fps = 1.0 / self._vision_interval if self._vision_interval else 5.0
            self._record_manifest_idx = self._manifest_add_artifact(
                "video", path=str(self._record_path), status="open", fps=round(fps, 2)
            )
        log.info("record: started -> %s (~%.1f fps)", self._record_path, fps)
        return f"recording -> {self._record_path}  (vision must be ON; ~{fps:.1f} fps)"

    def record_off(self) -> dict:
        with self._lock:
            self._recording = False
            writer, self._record_writer = self._record_writer, None
            summary = {"path": str(self._record_path) if self._record_path else None,
                       "frames": self._record_frames}
        if writer is not None:
            writer.release()
        log.info("record: stopped (%s frames) -> %s", summary["frames"], summary["path"])
        self._manifest_update_artifact(
            "video", self._record_manifest_idx, status="closed",
            ended_ts=round(time.time(), 3), frames=summary["frames"],
        )
        self._record_manifest_idx = None
        return summary

    def _write_video(self, frame):
        try:
            if self._record_writer is None:
                import cv2
                h, w = frame.shape[:2]
                fps = max(1.0, 1.0 / self._vision_interval) if self._vision_interval else 5.0
                self._record_writer = cv2.VideoWriter(
                    str(self._record_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
            self._record_writer.write(frame)
            self._record_frames += 1
        except Exception as e:  # noqa: BLE001
            log.warning("record: write error %s", e)

    # --- live MJPEG stream (debug: view what vision sees, over an ssh tunnel) ---

    def stream_on(self) -> str:
        """Serve the vision worker's latest frame as MJPEG on localhost:8090 (needs vision on).
        View via tunnel: `ssh -L 8090:localhost:8090 <m1max>` then http://localhost:8090."""
        port = 8090
        with self._lock:
            if self._streaming:
                return f"already streaming on :{port}"
            import cv2
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

            daemon = self

            class _Handler(BaseHTTPRequestHandler):
                def log_message(self, *a):  # keep the daemon log quiet
                    pass

                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    while daemon._streaming:
                        frame = daemon._latest_frame
                        ok = False
                        if frame is not None:
                            ok, jpg = cv2.imencode(".jpg", frame)
                        if not ok:
                            time.sleep(0.1)
                            continue
                        try:
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                            self.wfile.write(jpg.tobytes())
                            self.wfile.write(b"\r\n")
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        time.sleep(0.15)

            self._stream_server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
            self._stream_server.daemon_threads = True
            self._streaming = True
            threading.Thread(target=self._stream_server.serve_forever, name="stream", daemon=True).start()
        log.info("stream: MJPEG on 127.0.0.1:%d", port)
        return f"streaming on :{port} — ssh -L {port}:localhost:{port} <m1max>, then http://localhost:{port}"

    def stream_off(self) -> str:
        with self._lock:
            self._streaming = False
            srv, self._stream_server = self._stream_server, None
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        log.info("stream: stopped")
        return "stream off"

    # --- voice toggle ---

    def voice_on(self, conversation: bool = False) -> str:
        with self._lock:
            if _alive(self._voice_thread):
                return "voice already on"
            self._conversation_mode = conversation
            self._voice_stop = threading.Event()
            self._voice_thread = threading.Thread(
                target=self._voice_loop, args=(self._voice_stop,),
                name="voice", daemon=True,
            )
            self._voice_thread.start()
            return "voice on" + (" (conversation)" if conversation else "")

    def voice_off(self) -> str:
        with self._lock:
            if not _alive(self._voice_thread):
                return "voice already off"
            self._voice_stop.set()
            t = self._voice_thread
        t.join(timeout=15)
        return "voice off"

    def _voice_loop(self, stop: threading.Event):
        log.info("voice: worker started (interval=%ss, brain=%s, conversation=%s)",
                 self._voice_interval, self._brain_enabled, self._conversation_mode)
        brain = self._make_brain() if self._brain_enabled else None
        if brain is not None:
            brain.prewarm()  # spawn the claude process now — it initializes while the visitor
            # speaks their first words, so the FIRST reply isn't slowed by process startup.
        self._session.listen_start(model=self._stt_model, language=self._stt_language)
        start_ts = last_heard = time.monotonic()
        pending_utterances: dict[int, dict] = {}
        cue_stop: threading.Event | None = None
        cue_kind: str | None = None

        def start_cue(kind: str) -> threading.Event:
            nonlocal cue_stop, cue_kind
            if cue_stop is not None and not cue_stop.is_set() and cue_kind == kind:
                return cue_stop
            stop_cue()
            cue_stop = threading.Event()
            cue_kind = kind
            target = self._listen_ack_animate if kind == "listening" else self._think_animate
            threading.Thread(target=target, args=(cue_stop,), daemon=True).start()
            return cue_stop

        def stop_cue() -> None:
            nonlocal cue_stop, cue_kind
            if cue_stop is not None:
                cue_stop.set()
                cue_stop = None
                cue_kind = None

        def drain_activity() -> None:
            for event in self._drain_listen_activity():
                self._track_pending_utterance(pending_utterances, event)
                self._log_listen_activity(event)
                if brain is not None:
                    start_cue("listening")

        try:
            while not stop.is_set():
                # Conversation auto-end: idle (talker silent) OR a hard max-duration cap.
                # FIRST PASS: idle resets on ANY transcript, so background noise heard-as-text
                # can hold it open — the max cap bounds that. Speaker-aware close (reset only
                # on the enrolled talker's voice) is the planned v2.
                if self._conversation_mode:
                    now = time.monotonic()
                    if now - last_heard > self._conv_idle_timeout:
                        log.info("voice: conversation ended (idle %.0fs)", now - last_heard)
                        break
                    if now - start_ts > self._conv_max_duration:
                        log.info("voice: conversation ended (max cap %.0fs)", now - start_ts)
                        break
                try:
                    drain_activity()

                    # Poll frequently so VAD-queued activity can start the visible
                    # processing cue before STT finishes.
                    res = self._session.listen_read(timeout=0.1)
                    drain_activity()
                    if not self._is_transcript_event(res):
                        continue

                    batch, batch_meta = self._collect_transcript_batch(
                        res, pending_utterances, stop=stop, drain_activity=drain_activity,
                    )
                    for item in batch:
                        self._save_transcript_artifacts(item)
                        self._log_transcript(item)

                    usable = [item for item in batch if item.get("text", "").strip()]
                    if not usable:
                        continue

                    last_heard = time.monotonic()
                    brain_input = self._format_brain_input(usable)
                    heard_summary = self._format_heard_summary(usable)
                    if brain is not None:
                        start_cue("thinking")
                        try:
                            brain_received_ts = round(time.time(), 3)
                            brain_start = time.monotonic()
                            reply = brain.respond(brain_input)
                            brain_done_ts = round(time.time(), 3)
                            brain_latency_s = round(time.monotonic() - brain_start, 3)
                            log.info("voice: reply: %r", reply)
                            self._session.speak(reply)  # antennas auto-stop when voice starts
                            if self._save_turns:
                                transcripts = [
                                    self._transcript_metadata(item) for item in usable
                                ]
                                self._add_brain_received_metadata(
                                    transcripts, brain_received_ts,
                                )
                                self._save_turn(
                                    self._concat_batch_audio(usable),
                                    heard_summary,
                                    reply,
                                    metadata={
                                        "batch_size": len(usable),
                                        "brain_input": brain_input,
                                        "brain_received_ts": brain_received_ts,
                                        "brain_done_ts": brain_done_ts,
                                        "brain_latency_s": brain_latency_s,
                                        "transcripts": transcripts,
                                        **batch_meta,
                                    },
                                )
                        finally:
                            stop_cue()
                    else:
                        stop_cue()
                except Exception as e:  # noqa: BLE001
                    stop_cue()
                    log.warning("voice: error %s", e)
        finally:
            stop_cue()
            try:
                self._session.listen_stop()
            except Exception as e:  # noqa: BLE001
                log.warning("voice: listen_stop error %s", e)
            self._conversation_mode = False
            log.info("voice: worker stopped")

    def _make_brain(self):
        try:
            if self._brain_backend == "pydantic":
                from reachy_mini_brain.brain import PydanticBrain

                brain = PydanticBrain(model=self._brain_model)
                log.info("voice: loading brain (pydantic-ai/openrouter, model=%s)", brain.model)
                return brain
            from reachy_mini_brain.brain import ReceptionBrain

            log.info("voice: loading brain (claude -p, model=%s)", self._brain_model)
            return ReceptionBrain(model=self._brain_model)
        except Exception as e:  # noqa: BLE001
            log.warning("voice: brain unavailable (%s) — transcript-log only", e)
            return None

    def _drain_listen_activity(self, max_items: int = 16) -> list[dict]:
        read = getattr(self._session, "listen_activity_read", None)
        if read is None:
            return []
        events = []
        while len(events) < max_items:
            try:
                event = read(timeout=0.0)
            except Exception:  # noqa: BLE001
                return events
            if not event:
                break
            events.append(event)
        return events

    def _log_listen_activity(self, event: dict) -> None:
        if event.get("type") != "utterance_queued":
            return
        uid = event.get("utterance_id", "?")
        dur = event.get("buffer_duration") or 0.0
        age = self._delta(time.time(), event.get("speech_end_ts"))
        age_text = f" age={age:.1f}s" if age is not None else ""
        log.info("voice: queued u%s %.1fs%s -> visible listening cue", uid, float(dur), age_text)

    @staticmethod
    def _track_pending_utterance(pending: dict[int, dict], event: dict) -> None:
        if event.get("type") != "utterance_queued":
            return
        uid = event.get("utterance_id")
        if uid is None:
            return
        try:
            pending[int(uid)] = event
        except (TypeError, ValueError):
            return

    @staticmethod
    def _is_transcript_event(res: dict) -> bool:
        return isinstance(res, dict) and (
            res.get("utterance_id") is not None or bool(res.get("error"))
        )

    def _collect_transcript_batch(
        self,
        first: dict,
        pending_utterances: dict[int, dict],
        *,
        stop: threading.Event,
        drain_activity,
        max_items: int = 16,
    ) -> tuple[list[dict], dict]:
        """Collect ready transcripts and wait only for utterances VAD already queued."""
        batch = self._drain_transcript_batch(first, max_items=max_items)
        self._mark_transcripts_complete(pending_utterances, batch)

        waited = 0.0
        timed_out: list[int] = []
        if pending_utterances and self._batch_max_wait > 0:
            wait_start = time.monotonic()
            deadline = wait_start + self._batch_max_wait
            log.info(
                "voice: waiting for pending transcript(s) %s (cap %.1fs)",
                self._format_pending_ids(pending_utterances), self._batch_max_wait,
            )
            while pending_utterances and len(batch) < max_items and not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = sorted(pending_utterances)
                    for uid in timed_out:
                        pending_utterances.pop(uid, None)
                    log.info(
                        "voice: batch wait timed out after %.1fs; releasing pending %s",
                        self._batch_max_wait, self._format_id_list(timed_out),
                    )
                    break

                drain_activity()
                res = self._session.listen_read(timeout=min(BATCH_WAIT_POLL, remaining))
                drain_activity()
                if not self._is_transcript_event(res):
                    continue

                remaining_slots = max_items - len(batch)
                more = self._drain_transcript_batch(res, max_items=remaining_slots)
                batch.extend(more)
                self._mark_transcripts_complete(pending_utterances, more)

            waited = time.monotonic() - wait_start
        elif pending_utterances:
            timed_out = sorted(pending_utterances)
            for uid in timed_out:
                pending_utterances.pop(uid, None)

        meta = {
            "batch_wait_s": round(waited, 3),
            "batch_wait_timeout": bool(timed_out),
            "batch_timed_out_utterance_ids": timed_out,
        }
        return sorted(batch, key=self._transcript_sort_key), meta

    def _drain_transcript_batch(self, first: dict, max_items: int = 16) -> list[dict]:
        """Return the first transcript plus any immediately queued transcripts."""
        batch = [first]
        while len(batch) < max_items:
            res = self._session.listen_read(timeout=0.0)
            if not self._is_transcript_event(res):
                break
            batch.append(res)
        return sorted(batch, key=self._transcript_sort_key)

    @staticmethod
    def _mark_transcripts_complete(pending: dict[int, dict], batch: list[dict]) -> None:
        for item in batch:
            uid = item.get("utterance_id")
            if uid is None:
                continue
            try:
                pending.pop(int(uid), None)
            except (TypeError, ValueError):
                continue

    @classmethod
    def _format_pending_ids(cls, pending: dict[int, dict]) -> str:
        return cls._format_id_list(sorted(pending))

    @staticmethod
    def _format_id_list(ids: list[int]) -> str:
        return ",".join(f"u{uid}" for uid in ids) or "none"

    @staticmethod
    def _transcript_sort_key(item: dict) -> tuple:
        return (
            item.get("speech_start_ts") or item.get("queued_ts") or item.get("stt_done_ts") or 0,
            item.get("utterance_id") or 0,
        )

    @staticmethod
    def _delta(end, start) -> float | None:
        try:
            return float(end) - float(start)
        except (TypeError, ValueError):
            return None

    def _log_transcript(self, item: dict) -> None:
        uid = item.get("utterance_id", "?")
        dur = item.get("buffer_duration") or 0.0
        age = self._delta(time.time(), item.get("speech_end_ts"))
        queue_wait = self._delta(item.get("stt_start_ts"), item.get("queued_ts"))
        stt_latency = item.get("stt_latency")
        parts = [f"u{uid}", f"{float(dur):.1f}s"]
        if age is not None:
            parts.append(f"age={age:.1f}s")
        if queue_wait is not None:
            parts.append(f"queue={queue_wait:.1f}s")
        if stt_latency is not None:
            parts.append(f"stt={float(stt_latency):.1f}s")

        text = item.get("text", "")
        if item.get("error"):
            log.warning("voice: stt error %s: %s", " ".join(parts), item["error"])
        elif text.strip():
            log.info("voice: heard %s: %r", " ".join(parts), text)
        else:
            log.info("voice: transcript empty %s", " ".join(parts))

    def _format_brain_input(self, batch: list[dict]) -> str:
        ordered = sorted(batch, key=self._transcript_sort_key)
        if len(ordered) == 1:
            text = ordered[0].get("text", "").strip()
            age = self._delta(time.time(), ordered[0].get("speech_end_ts"))
            if age is not None and age > 5.0:
                return (
                    f"The visitor said this {age:.1f} seconds ago: {text}\n"
                    "Reply naturally if it is still relevant."
                )
            return text

        lines = []
        for item in ordered:
            text = item.get("text", "").strip()
            ts = item.get("speech_start_ts")
            label = time.strftime("%H:%M:%S", time.localtime(float(ts))) if ts else "unknown"
            age = self._delta(time.time(), item.get("speech_end_ts"))
            age_text = f", ended {age:.1f}s ago" if age is not None else ""
            lines.append(f"[{label}{age_text}] {text}")
        return (
            "The visitor said these utterances in chronological order. Use the timestamps "
            "only for ordering and reply naturally to the latest relevant request:\n"
            + "\n".join(lines)
        )

    def _format_heard_summary(self, batch: list[dict]) -> str:
        texts = [item.get("text", "").strip() for item in sorted(batch, key=self._transcript_sort_key)]
        return " / ".join(text for text in texts if text)

    @staticmethod
    def _concat_batch_audio(batch: list[dict]):
        import numpy as np

        chunks = []
        silence = np.zeros(int(0.2 * 16000), dtype=np.float32)
        for item in sorted(batch, key=ReceptionDaemon._transcript_sort_key):
            audio = item.get("audio")
            if audio is None:
                continue
            a = np.asarray(audio, dtype=np.float32)
            a = a[:, 0] if a.ndim > 1 else a.reshape(-1)
            if chunks:
                chunks.append(silence)
            chunks.append(a)
        if not chunks:
            return None
        return np.concatenate(chunks)

    @staticmethod
    def _metadata_value(value):
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {k: ReceptionDaemon._metadata_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [ReceptionDaemon._metadata_value(v) for v in value]
        try:
            import numpy as np

            if isinstance(value, np.generic):
                return value.item()
        except Exception:  # noqa: BLE001
            pass
        return str(value)

    def _transcript_metadata(self, item: dict) -> dict:
        return {
            k: self._metadata_value(v)
            for k, v in item.items()
            if k != "audio"
        }

    def _add_brain_received_metadata(self, transcripts: list[dict], brain_received_ts: float) -> None:
        """Annotate turn transcript metadata with when the batch was handed to the brain."""
        for rec in transcripts:
            rec["brain_received_ts"] = brain_received_ts
            after_speech = self._delta(brain_received_ts, rec.get("speech_end_ts"))
            after_stt = self._delta(brain_received_ts, rec.get("stt_done_ts"))
            if after_speech is not None:
                rec["brain_received_after_speech_end_s"] = round(after_speech, 3)
            if after_stt is not None:
                rec["brain_received_after_stt_done_s"] = round(after_stt, 3)

    def _save_transcript_artifacts(self, item: dict) -> None:
        """Persist transcript JSONL always; persist utterance WAVs when --save-turns is on."""
        try:
            self._transcript_n += 1
            rec = self._transcript_metadata(item)
            rec.update({"run_id": self._run_id, "n": self._transcript_n, "ts": round(time.time(), 3)})

            if self._save_turns and item.get("audio") is not None:
                try:
                    import soundfile as sf

                    d = ARTIFACTS / "utterances"
                    d.mkdir(parents=True, exist_ok=True)
                    if self._utterances_jsonl is None:
                        self._utterances_jsonl = d / f"utterances-{self._run_id}.jsonl"
                        self._utterances_manifest_idx = self._manifest_add_artifact(
                            "utterances", path=str(self._utterances_jsonl), status="open"
                        )
                    wav = d / f"utterance-{self._run_id}-{self._transcript_n:03d}.wav"
                    sf.write(str(wav), item["audio"], 16000)
                    urec = dict(rec)
                    urec.update({"wav": wav.name, "path": str(wav)})
                    with open(self._utterances_jsonl, "a", encoding="utf-8") as f:
                        f.write(json.dumps(urec) + "\n")
                    self._manifest_update_artifact(
                        "utterances", self._utterances_manifest_idx, status="open",
                        utterances=self._transcript_n, latest_wav=str(wav),
                    )
                    rec["utterance_wav"] = str(wav)
                except Exception as e:  # noqa: BLE001
                    log.warning("save_utterance_artifact error %s", e)

            d = ARTIFACTS / "transcripts"
            d.mkdir(parents=True, exist_ok=True)
            if self._transcripts_jsonl is None:
                self._transcripts_jsonl = d / f"transcripts-{self._run_id}.jsonl"
                self._transcripts_manifest_idx = self._manifest_add_artifact(
                    "transcripts", path=str(self._transcripts_jsonl), status="open"
                )
            with open(self._transcripts_jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            self._manifest_update_artifact(
                "transcripts", self._transcripts_manifest_idx, status="open",
                transcripts=self._transcript_n, latest_utterance_id=item.get("utterance_id"),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("save_transcript_artifacts error %s", e)

    def _save_turn(self, audio, heard: str, reply: str, metadata: dict | None = None) -> None:
        """Debug capture (--save-turns): save each turn's utterance WAV + the heard STT text
        and the brain reply, so off replies can be attributed to STT vs brain (listen to the
        wav, compare to `heard`). Records to artifacts/turns/turns-<ts>.jsonl + per-turn wavs."""
        if audio is None:
            return
        try:
            import soundfile as sf

            d = ARTIFACTS / "turns"
            d.mkdir(parents=True, exist_ok=True)
            if self._turns_jsonl is None:
                self._turns_jsonl = d / f"turns-{self._run_id}.jsonl"
                self._turns_manifest_idx = self._manifest_add_artifact(
                    "turns", path=str(self._turns_jsonl), status="open"
                )
            self._turn_n += 1
            wav = d / f"turn-{self._run_id}-{self._turn_n:03d}.wav"
            sf.write(str(wav), audio, 16000)
            rec = {"ts": time.time(), "n": self._turn_n, "dur": round(len(audio) / 16000.0, 2),
                   "heard": heard, "reply": reply, "wav": wav.name, "run_id": self._run_id}
            if metadata:
                rec.update(self._metadata_value(metadata))
            with open(self._turns_jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            self._manifest_update_artifact(
                "turns", self._turns_manifest_idx, status="open",
                turns=self._turn_n, latest_wav=str(wav),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("save_turn error %s", e)

    # --- status ---

    def status(self) -> dict:
        st = {
            "run_id": self._run_id,
            "manifest": str(self._manifest_path),
            "vision": "on" if _alive(self._vision_thread) else "off",
            "voice": "on" if _alive(self._voice_thread) else "off",
        }
        try:
            st["session"] = self._session.status()
        except Exception as e:  # noqa: BLE001
            st["session"] = f"error: {e}"
        return st

    # --- react (the alert engine triggers this) ---

    def react(self) -> str:
        """Greet an approaching visitor."""
        if self._conversation_mode:
            log.info("react: suppressed (conversation active)")
            return "suppressed (in conversation)"
        return self._express(self._greeting, "react: greeted visitor", "reacted")

    def reset(self) -> str:
        """Reset head + body + antennas to a neutral 'home' pose (no speech/gesture)."""
        self._session.move_head(pitch=0.0, roll=0.0, yaw=0.0, duration=0.8)
        self._session.rotate_body(0.0, duration=0.8)
        self._session.antennas(*NEUTRAL_ANTENNAS)
        log.info("reset: head/body/antennas to neutral")
        return "reset: head + body + antennas neutral"

    def antennas(self, left: float = 0.0, right: float = 0.0) -> str:
        """Set antenna angles directly (degrees, positive = up). For live calibration
        of the resting pose; the value chosen here becomes NEUTRAL_ANTENNAS in code."""
        self._session.antennas(float(left), float(right))
        log.info("antennas -> left=%s right=%s", left, right)
        return f"antennas: left={left} right={right}"

    def farewell(self) -> str:
        """Say goodbye to a departing visitor."""
        if self._conversation_mode:
            log.info("farewell: suppressed (conversation active)")
            return "suppressed (in conversation)"
        return self._express(self._farewell, "farewell: said goodbye", "farewelled")

    def wave_back(self) -> str:
        """Acknowledge a wave — a DISTINCT response from the approach greeting so the
        two are easy to tell apart when testing wave detection."""
        return self._express(self._wave_message, "wave_back: acknowledged a wave", "waved back")

    def say(self, text: str, cache: bool = False) -> str:
        """Diagnostic: speak arbitrary text through the daemon-owned session."""
        self._session.speak(text, cache=cache)
        log.info("say: spoke diagnostic line")
        return "said"

    def start_conversation(self) -> str:
        """Wave-triggered: BEGIN a conversation — speak an opener, then start the voice/brain
        loop (which auto-ends on idle or the max-duration cap). Idempotent while one is active.
        Needs --brain + a keychain-authed context for claude -p (e.g. the daemon run from tmux)."""
        if _alive(self._voice_thread):
            return "already in conversation"
        self._express(self._conversation_opener, "conversation: opened", "opened")
        return self.voice_on(conversation=True)

    def _express(self, message: str, done_log: str, result: str) -> str:
        """Flick antennas, speak, reset antennas. Deliberately does NOT move the head:
        the camera rides on the head, so any glance would tilt/shift every video frame.
        Antennas are separate joints (not in the camera view) so they're safe to keep."""
        for action in (
            lambda: self._session.antennas(20, 20),
            lambda: self._session.speak(message, cache=True),
            lambda: self._session.antennas(*NEUTRAL_ANTENNAS),
        ):
            try:
                action()
            except Exception as e:  # noqa: BLE001
                log.warning("express: action error %s", e)
        log.info(done_log)
        return result

    def _think_animate(self, stop_evt: threading.Event) -> None:
        """Wiggle the antennas to signal 'thinking' during the heard->reply gap.

        Fills the dead time (brain call + TTS synth) so the robot doesn't look frozen.
        Stops the instant reply audio starts (session._speaking flips True) or stop_evt
        is set, then resets antennas to neutral. Antennas only — the camera rides on the
        head, so we never move it mid-turn."""
        poses = ((25, 10), (10, 25))  # gentle alternating sway
        i = 0
        while not stop_evt.is_set() and not getattr(self._session, "_speaking", False):
            try:
                self._session.antennas(*poses[i % len(poses)])
            except Exception:  # noqa: BLE001
                pass
            i += 1
            stop_evt.wait(0.3)
        try:
            self._session.antennas(*NEUTRAL_ANTENNAS)
        except Exception:  # noqa: BLE001
            pass

    def _listen_ack_animate(self, stop_evt: threading.Event) -> None:
        """Symmetric antenna pulse: the robot heard a complete utterance and is transcribing."""
        poses = ((18, 18), (6, 6))
        i = 0
        while not stop_evt.is_set() and not getattr(self._session, "_speaking", False):
            try:
                self._session.antennas(*poses[i % len(poses)])
            except Exception:  # noqa: BLE001
                pass
            i += 1
            stop_evt.wait(0.22)
        try:
            self._session.antennas(*NEUTRAL_ANTENNAS)
        except Exception:  # noqa: BLE001
            pass

    # --- raw audio recording ---

    def audio_record_on(self) -> dict:
        """Start recording raw continuous mic audio (Cat-1) through the shared session mic loop."""
        if self._audio_record_manifest_idx is not None:
            return self._session.audio_record_start()
        path = self._artifact_path("audio", ".wav")
        self._audio_record_manifest_idx = self._manifest_add_artifact(
            "audio", path=str(path), metadata=str(path.with_suffix(".jsonl")), status="open"
        )
        summary = self._session.audio_record_start(str(path), run_id=self._run_id)
        log.info("audio_record: started -> %s", summary.get("path"))
        return summary

    def audio_record_off(self) -> dict:
        """Stop raw continuous mic audio recording."""
        summary = self._session.audio_record_stop()
        log.info("audio_record: stopped (%s samples) -> %s",
                 summary.get("samples"), summary.get("path"))
        self._manifest_update_artifact(
            "audio", self._audio_record_manifest_idx, status="closed",
            ended_ts=round(time.time(), 3), samples=summary.get("samples"),
            duration=summary.get("duration"), chunks=summary.get("chunks"),
        )
        self._audio_record_manifest_idx = None
        return summary


def _alive(t: threading.Thread | None) -> bool:
    return t is not None and t.is_alive()


# ---------------------------------------------------------------------------
# Socket server — keeps the daemon alive, accepts control commands
# ---------------------------------------------------------------------------

# daemon methods callable over the socket
_COMMANDS = {"vision_on", "vision_off", "voice_on", "voice_off", "status", "react",
             "farewell", "reset", "wave_back", "start_conversation",
             "capture_on", "capture_off", "record_on", "record_off",
             "stream_on", "stream_off", "audio_record_on", "audio_record_off",
             "antennas", "say"}


def _send(conn: socket.socket, obj: dict):
    conn.sendall((json.dumps(obj) + "\n").encode())


def _handle(daemon: ReceptionDaemon, conn: socket.socket) -> bool:
    """Handle one connection. Returns False if the server should stop."""
    try:
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        if not data:
            return True

        msg = json.loads(data.decode().strip())
        method = msg.get("method", "")
        params = msg.get("params") or {}

        if method == "shutdown":
            _send(conn, {"ok": True, "result": "shutting down"})
            return False
        if method not in _COMMANDS:
            _send(conn, {"ok": False, "error": f"unknown command: {method}"})
            return True

        result = getattr(daemon, method)(**params)
        _send(conn, {"ok": True, "result": result})
    except Exception as e:  # noqa: BLE001
        try:
            _send(conn, {"ok": False, "error": str(e)})
        except Exception:
            pass
    finally:
        conn.close()
    return True


def serve_daemon(mock: bool, vision_interval: float, voice_interval: float,
                 perception: bool = False, threshold: float = 0.5,
                 gestures: bool = False,
                 brain: bool = False, brain_model: str = "sonnet",
                 brain_backend: str = "claude", save_turns: bool = False,
                 stt_model: str = DEFAULT_STT_MODEL,
                 stt_language: str = DEFAULT_STT_LANGUAGE,
                 batch_max_wait: float = DEFAULT_BATCH_MAX_WAIT,
                 run_id: str | None = None, log_path: Path | None = None):
    """Start the reception daemon + control socket (blocks until shutdown)."""
    if mock:
        session = MockSession()
    else:
        # Lazy import: only here do we pull in the SDK-heavy Session.
        from reachy_mini_brain.session import Session

        session = Session()

    daemon = ReceptionDaemon(
        session, vision_interval=vision_interval, voice_interval=voice_interval,
        perception=perception, threshold=threshold, gestures=gestures,
        brain=brain, brain_model=brain_model, brain_backend=brain_backend,
        save_turns=save_turns, stt_model=stt_model, stt_language=stt_language,
        batch_max_wait=batch_max_wait,
        run_id=run_id,
        log_path=log_path,
    )

    log.info("starting session%s...", " (mock)" if mock else "")
    daemon.start()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(1)
    server.settimeout(1.0)

    running = True

    def _sig(_s, _f):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    log.info("reception daemon ready on %s (vision=off, voice=off)", SOCKET_PATH)

    while running:
        try:
            conn, _ = server.accept()
            if not _handle(daemon, conn):
                running = False
        except socket.timeout:
            continue
        except OSError:
            break

    log.info("shutting down...")
    daemon.stop()
    server.close()
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    log.info("reception daemon stopped")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _client(method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    """Send one control command to the running daemon."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(SOCKET_PATH)
    sock.sendall((json.dumps({"method": method, "params": params or {}}) + "\n").encode())
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    sock.close()
    return json.loads(data.decode().strip())


def _run_client(method: str, params: dict | None = None):
    try:
        result = _client(method, params)
    except (FileNotFoundError, ConnectionRefusedError):
        click.echo(
            "Error: reception daemon not running. "
            "Start it with: reception serve",
            err=True,
        )
        raise SystemExit(1)
    if result.get("ok"):
        r = result.get("result")
        click.echo(json.dumps(r, indent=2) if isinstance(r, dict) else r)
    else:
        click.echo(f"Error: {result.get('error')}", err=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
def cli():
    """Reachy Mini reception daemon (Phase A: control plane)."""
    pass


@cli.command()
@click.option("--mock", is_flag=True, help="Use a fake session (no SDK/robot).")
@click.option("--vision-interval", default=0.2,
              help="Seconds between frame grabs (~5 fps). The approach/depart geometry is "
                   "calibrated for this — 2.0 (0.5 fps) stretches reset_absent 8s->80s and breaks greet/goodbye.")
@click.option("--voice-interval", default=1.5,
              help="Seconds between mic reads — lower = faster turn-taking (VAD endpointing is the deeper fix).")
@click.option("--perception/--no-perception", default=False,
              help="Run the RF-DETR person/approach pipeline in the vision worker.")
@click.option("--threshold", default=0.5, help="Detector confidence threshold.")
@click.option("--gestures/--no-gestures", default=False,
              help="Also run MediaPipe wave detection (Open_Palm) in the vision worker.")
@click.option("--brain/--no-brain", default=False,
              help="Route heard speech to the claude -p receptionist brain and speak the reply.")
@click.option("--brain-model", default="sonnet", help="claude backend model (sonnet/haiku/opus).")
@click.option("--brain-backend", type=click.Choice(["claude", "pydantic"]), default="claude",
              help="Brain backend: claude -p (default) or pydantic-ai over OpenRouter.")
@click.option("--save-turns/--no-save-turns", default=False,
              help="Debug: save each turn's utterance WAV + heard/reply to artifacts/turns/ "
                   "(to attribute off replies to STT vs brain).")
@click.option("--stt-model", default=DEFAULT_STT_MODEL, show_default=True,
              help="faster-whisper model for live STT, e.g. medium, large-v3-turbo, large-v3.")
@click.option("--stt-language", default=DEFAULT_STT_LANGUAGE, show_default=True,
              help='STT language code, or "auto" for language detection.')
@click.option("--batch-max-wait", default=DEFAULT_BATCH_MAX_WAIT, show_default=True,
              help="Max seconds to wait for known queued utterances before sending a brain batch.")
def serve(mock, vision_interval, voice_interval, perception, threshold, gestures, brain,
          brain_model, brain_backend, save_turns, stt_model, stt_language, batch_max_wait):
    """Run the reception daemon (blocks until `shutdown` or Ctrl-C)."""
    # Durable log: the daemon owns a timestamped file under artifacts/logs/ (never /tmp,
    # which the OS cleans), in addition to stderr. Survives restarts; never overwritten.
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    logfile = ARTIFACTS / "logs" / f"reception-{run_id}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(threadName)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(logfile)],
    )
    log.info("durable log -> %s", logfile)
    log.info("run_id -> %s", run_id)
    serve_daemon(mock, vision_interval, voice_interval, perception, threshold,
                 gestures, brain, brain_model, brain_backend, save_turns,
                 stt_model, stt_language, batch_max_wait,
                 run_id=run_id, log_path=logfile)


@cli.command()
@click.argument("state", type=click.Choice(["on", "off"]))
def vision(state):
    """Toggle the vision worker on or off."""
    _run_client(f"vision_{state}")


@cli.command()
@click.argument("state", type=click.Choice(["on", "off"]))
def voice(state):
    """Toggle the voice worker on or off."""
    _run_client(f"voice_{state}")


@cli.command()
def react():
    """Trigger the robot's greeting reaction (normally called by the alert engine)."""
    _run_client("react")


@cli.command()
def reset():
    """Reset the robot pose: head + body + antennas to neutral (no speech)."""
    _run_client("reset")


@cli.command()
@click.option("--left", type=float, required=True, help="Left antenna angle (deg, +=up)")
@click.option("--right", type=float, required=True, help="Right antenna angle (deg, +=up)")
def antennas(left, right):
    """Set antenna angles directly (live calibration of the resting pose)."""
    _run_client("antennas", {"left": left, "right": right})


@cli.command()
def farewell():
    """Trigger the robot's goodbye (normally the alert engine fires this on departure)."""
    _run_client("farewell")


@cli.command()
def wave():
    """Trigger the wave acknowledgment (manual; standalone "Hi there!", no conversation)."""
    _run_client("wave_back")


@cli.command()
@click.argument("text")
def say(text):
    """Speak arbitrary diagnostic text through the running daemon."""
    _run_client("say", {"text": text})


@cli.command()
def converse():
    """Begin a conversation (opener + voice loop) — what a wave now triggers."""
    _run_client("start_conversation")


@cli.command()
@click.argument("state", type=click.Choice(["on", "off"]))
def capture(state):
    """Record per-frame vision data to artifacts/capture-*.jsonl for a test run."""
    _run_client(f"capture_{state}")


@cli.command()
@click.argument("state", type=click.Choice(["on", "off"]))
def record(state):
    """Record the camera to artifacts/video-*.mkv (needs vision on)."""
    _run_client(f"record_{state}")


@cli.command("audio-record")
@click.argument("state", type=click.Choice(["on", "off"]))
def audio_record(state):
    """Record raw mic audio to artifacts/audio-*.wav + .jsonl sidecar."""
    _run_client(f"audio_record_{state}")


@cli.command()
@click.argument("state", type=click.Choice(["on", "off"]))
def stream(state):
    """Toggle a live MJPEG camera stream on localhost:8090 (needs vision on)."""
    _run_client(f"stream_{state}")


@cli.command()
def status():
    """Show vision/voice toggle state and session health."""
    _run_client("status")


@cli.command()
def shutdown():
    """Stop the reception daemon."""
    _run_client("shutdown")


if __name__ == "__main__":
    cli()
