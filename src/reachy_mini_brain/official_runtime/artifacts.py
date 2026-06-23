"""Run-scoped artifact recording for official-runtime sessions."""

from __future__ import annotations
import json
import time
import uuid
import logging
import threading
from typing import Any, Mapping
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .events import RuntimeEvent


logger = logging.getLogger(__name__)


class ArtifactRecorder:
    """Persist runtime events, policy events, realtime events, video, and audio."""

    def __init__(
        self,
        root: str | Path,
        *,
        run_id: str | None = None,
        config: Mapping[str, Any] | None = None,
        record_audio: bool = False,
        record_video: bool = False,
        capture_vision: bool = False,
    ) -> None:
        self.root = Path(root).expanduser()
        self.run_id = run_id or f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.record_audio_enabled = record_audio
        self.record_video_enabled = record_video
        self.capture_vision_enabled = capture_vision

        self._lock = threading.RLock()
        self._closed = False
        self._counts: dict[str, int] = {}

        self._event_path = self._artifact_path("events", ".jsonl", subdir="events")
        self._policy_path = self._artifact_path("policies", ".jsonl", subdir="policies")
        self._realtime_path = self._artifact_path("realtime", ".jsonl", subdir="realtime")
        self._capture_path = self._artifact_path("capture", ".jsonl", subdir="capture")
        self._manifest_path = self.root / "runs" / f"run-{self.run_id}.json"

        self._video_path: Path | None = None
        self._video_writer: Any | None = None
        self._video_frames = 0
        self._video_fps = 5.0

        self._audio_files: dict[str, Any] = {}
        self._audio_meta_files: dict[str, Any] = {}
        self._audio_samples: dict[str, int] = {}
        self._audio_chunks: dict[str, int] = {}
        self._audio_paths: dict[str, Path] = {}
        self._response_streams: dict[str, str] = {}

        self._manifest: dict[str, Any] = {
            "run_id": self.run_id,
            "started_ts": round(time.time(), 3),
            "config": dict(config or {}),
            "session": {},
            "responses": {},
            "artifacts": {
                "events": [{"path": str(self._event_path), "run_id_field": True}],
                "policies": [{"path": str(self._policy_path), "run_id_field": True}],
                "realtime": [{"path": str(self._realtime_path), "run_id_field": True}],
                "capture": [],
                "video": [],
                "audio": [],
            },
        }
        if self.capture_vision_enabled:
            self._manifest["artifacts"]["capture"].append(
                {"path": str(self._capture_path), "status": "open", "started_ts": round(time.time(), 3)}
            )
        self._write_manifest()
        self.event("run.started", source="official_runtime.artifacts")

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    def emit(self, event: RuntimeEvent) -> None:
        """Record a generic runtime event.

        This makes the recorder usable anywhere an ``EventSink`` is accepted.
        Domain-specific audio/video data is captured through the explicit
        ``record_*`` methods so raw samples do not need to ride through JSONL
        event payloads.
        """

        data = _event_payload(event.data)
        self.event(event.kind, source=event.source, event_ts=round(event.ts, 3), **data)
        if event.kind.startswith("policy."):
            self._write_jsonl(
                self._policy_path,
                {
                    "type": event.kind.removeprefix("policy."),
                    "source": event.source,
                    "event_ts": round(event.ts, 3),
                    **data,
                },
            )
        elif event.kind.startswith(("realtime.", "livekit.", "backend.")):
            self.realtime(event.kind, source=event.source, event_ts=round(event.ts, 3), **data)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True

            if self._video_writer is not None:
                try:
                    self._video_writer.release()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("video writer close failed: %s", exc)
                self._video_writer = None
                for rec in self._manifest["artifacts"].get("video", []):
                    if rec.get("status") == "open":
                        rec.update(
                            {
                                "status": "closed",
                                "ended_ts": round(time.time(), 3),
                                "frames": self._video_frames,
                            }
                        )

            for stream_name in list(self._audio_meta_files):
                self._close_audio_stream(stream_name)

            for rec in self._manifest["artifacts"].get("capture", []):
                if rec.get("status") == "open":
                    rec.update({"status": "closed", "ended_ts": round(time.time(), 3)})

            self._manifest["ended_ts"] = round(time.time(), 3)
            self._write_manifest()

    def event(self, kind: str, *, source: str = "reception", **data: Any) -> None:
        self._write_jsonl(self._event_path, {"type": kind, "source": source, **data})

    def policy(self, kind: str, **data: Any) -> None:
        self._write_jsonl(self._policy_path, {"type": kind, **data})
        self.event(f"policy.{kind}", source="reception.policy", **data)

    def realtime(self, kind: str, **data: Any) -> None:
        self._write_jsonl(self._realtime_path, {"type": kind, **data})

    def record_realtime_event(self, kind: str, **data: Any) -> None:
        self.realtime(kind, **data)

    def session_snapshot(self, **data: Any) -> None:
        with self._lock:
            session = self._manifest.setdefault("session", {})
            if isinstance(session, dict):
                session.update({key: _jsonable(value) for key, value in data.items()})
            else:
                self._manifest["session"] = {key: _jsonable(value) for key, value in data.items()}
            self._write_manifest()
        self.realtime("session.snapshot", **data)

    def record_session_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.session_snapshot(**dict(snapshot))

    def response_metadata(self, response_id: str | None, **data: Any) -> None:
        if not response_id:
            return
        with self._lock:
            responses = self._manifest.setdefault("responses", {})
            if not isinstance(responses, dict):
                responses = {}
                self._manifest["responses"] = responses
            response = responses.setdefault(response_id, {})
            if isinstance(response, dict):
                response.update({key: _jsonable(value) for key, value in data.items()})
            else:
                responses[response_id] = {key: _jsonable(value) for key, value in data.items()}
            self._write_manifest()

    def record_response_metadata(self, response_id: str | None, metadata: Mapping[str, Any]) -> None:
        self.response_metadata(response_id, **dict(metadata))

    def message(self, role: str | None, content: Any) -> None:
        if isinstance(content, str):
            stored_content: Any = content if len(content) < 1000 else content[:1000] + "..."
        else:
            stored_content = f"<{type(content).__name__}>"
        self.realtime("message", role=role, content=stored_content)

    def record_output_message(self, message: Mapping[str, Any]) -> None:
        self.message(message.get("role") if isinstance(message.get("role"), str) else None, message.get("content"))

    def vision_frame(
        self,
        frame: NDArray[np.uint8],
        *,
        people: int,
        tracks: list[dict[str, Any]],
        events: list[dict[str, Any]],
        fps: float,
    ) -> None:
        if self.capture_vision_enabled:
            self._write_jsonl(
                self._capture_path,
                {
                    "type": "vision_frame",
                    "people": people,
                    "tracks": tracks,
                    "events": events,
                },
            )
        if self.record_video_enabled:
            self._write_video_frame(frame, fps=fps)

    def input_audio_frame(self, sample_rate: int, audio: NDArray[Any], *, forwarded: bool) -> None:
        if self.record_audio_enabled:
            self._write_audio_frame("input", sample_rate, audio, extra={"forwarded": forwarded})

    def record_input_audio_frame(self, sample_rate: int, audio: NDArray[Any], *, forwarded: bool = True) -> None:
        self.input_audio_frame(sample_rate, audio, forwarded=forwarded)

    def output_audio_frame(
        self,
        sample_rate: int,
        audio: NDArray[Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self.record_audio_enabled:
            extra = dict(metadata or {})
            self._write_audio_frame("output", sample_rate, audio, extra=extra)
            response_id = extra.get("response_id")
            if isinstance(response_id, str) and response_id:
                stream_name = self._response_stream_name(response_id)
                self._write_audio_frame(stream_name, sample_rate, audio, extra=extra)
                self.response_metadata(
                    response_id,
                    audio_stream=stream_name,
                    audio_path=str(self._audio_paths.get(stream_name, "")),
                    audio_metadata=str(self._audio_paths.get(stream_name, Path()).with_suffix(".jsonl"))
                    if stream_name in self._audio_paths
                    else "",
                )

    def record_output_audio_frame(
        self,
        sample_rate: int,
        audio: NDArray[Any],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.output_audio_frame(sample_rate, audio, metadata=metadata)

    def record_playback_cleared(self) -> None:
        self.realtime("playback_cleared")

    def _response_stream_name(self, response_id: str) -> str:
        stream_name = self._response_streams.get(response_id)
        if stream_name is not None:
            return stream_name
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in response_id).strip("_")
        if not safe:
            safe = f"unknown-{len(self._response_streams) + 1:02d}"
        stream_name = f"response-{safe[:80]}"
        self._response_streams[response_id] = stream_name
        return stream_name

    def _artifact_path(self, kind: str, suffix: str, *, subdir: str) -> Path:
        self._counts[kind] = self._counts.get(kind, 0) + 1
        path = self.root / subdir / f"{kind}-{self.run_id}-{self._counts[kind]:02d}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _write_manifest(self) -> None:
        with self._lock:
            self._manifest["updated_ts"] = round(time.time(), 3)
            self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self._manifest_path)

    def _write_jsonl(self, path: Path, rec: Mapping[str, Any]) -> None:
        if self._closed:
            return
        payload = {
            "run_id": self.run_id,
            "ts": round(time.time(), 3),
            **{key: _jsonable(value) for key, value in rec.items()},
        }
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, sort_keys=True) + "\n")

    def _write_video_frame(self, frame: NDArray[np.uint8], *, fps: float) -> None:
        try:
            import cv2
        except ImportError:
            self.event("video.unavailable", error="opencv-python is not installed")
            self.record_video_enabled = False
            return

        with self._lock:
            if self._video_writer is None:
                self._video_fps = max(1.0, float(fps))
                self._video_path = self._artifact_path("video", ".mkv", subdir="video")
                h, w = frame.shape[:2]
                self._video_writer = cv2.VideoWriter(
                    str(self._video_path),
                    cv2.VideoWriter_fourcc(*"mp4v"),
                    self._video_fps,
                    (w, h),
                )
                self._manifest["artifacts"]["video"].append(
                    {
                        "path": str(self._video_path),
                        "status": "open",
                        "fps": round(self._video_fps, 2),
                        "started_ts": round(time.time(), 3),
                    }
                )
                self._write_manifest()
            self._video_writer.write(frame)
            self._video_frames += 1

    def _write_audio_frame(
        self,
        stream_name: str,
        sample_rate: int,
        audio: NDArray[Any],
        *,
        extra: Mapping[str, Any],
    ) -> None:
        audio_1d = _mono_float32(audio)
        with self._lock:
            if stream_name not in self._audio_files:
                try:
                    import soundfile as sf
                except ImportError:
                    self.event("audio.unavailable", error="soundfile is not installed")
                    self.record_audio_enabled = False
                    return

                path = self._artifact_path(f"audio-{stream_name}", ".wav", subdir="audio")
                meta = path.with_suffix(".jsonl")
                self._audio_files[stream_name] = sf.SoundFile(
                    str(path),
                    mode="w",
                    samplerate=int(sample_rate),
                    channels=1,
                    subtype="FLOAT",
                )
                self._audio_meta_files[stream_name] = meta.open("w", encoding="utf-8")
                self._audio_samples[stream_name] = 0
                self._audio_chunks[stream_name] = 0
                self._audio_paths[stream_name] = path
                self._manifest["artifacts"]["audio"].append(
                    {
                        "stream": stream_name,
                        "path": str(path),
                        "metadata": str(meta),
                        "sample_rate": int(sample_rate),
                        "status": "open",
                        "started_ts": round(time.time(), 3),
                    }
                )
                self._write_manifest()

            start = self._audio_samples[stream_name]
            self._audio_files[stream_name].write(audio_1d)
            self._audio_samples[stream_name] += int(audio_1d.shape[0])
            self._audio_chunks[stream_name] += 1
            self._audio_meta_files[stream_name].write(
                json.dumps(
                    {
                        "run_id": self.run_id,
                        "ts": round(time.time(), 3),
                        "type": "chunk",
                        "sample_start": start,
                        "samples": int(audio_1d.shape[0]),
                        "rms": round(float(np.sqrt(np.mean(audio_1d**2))) if audio_1d.size else 0.0, 5),
                        **dict(extra),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            self._audio_meta_files[stream_name].flush()

    def _close_audio_stream(self, stream_name: str) -> None:
        meta = self._audio_meta_files.pop(stream_name, None)
        audio_file = self._audio_files.pop(stream_name, None)
        samples = self._audio_samples.get(stream_name, 0)
        if meta is not None:
            meta.write(
                json.dumps(
                    {
                        "run_id": self.run_id,
                        "ts": round(time.time(), 3),
                        "type": "stop",
                        "sample_end": samples,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            meta.close()
        if audio_file is not None:
            audio_file.close()
        for rec in self._manifest["artifacts"].get("audio", []):
            if rec.get("stream") == stream_name and rec.get("status") == "open":
                rec.update(
                    {
                        "status": "closed",
                        "ended_ts": round(time.time(), 3),
                        "samples": samples,
                        "chunks": self._audio_chunks.get(stream_name, 0),
                    }
                )


def _mono_float32(audio: NDArray[Any]) -> NDArray[np.float32]:
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim == 2:
        if arr.shape[0] == 1:
            arr = arr.reshape(-1)
        elif arr.shape[1] == 1:
            arr = arr[:, 0]
        elif arr.shape[0] < arr.shape[1]:
            arr = arr[0, :]
        else:
            arr = arr[:, 0]
    return np.ascontiguousarray(arr.reshape(-1), dtype=np.float32)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return {
            "type": "ndarray",
            "shape": list(value.shape),
            "dtype": str(value.dtype),
        }
    return str(value)


def _event_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    reserved = {"kind", "type", "source"}
    payload: dict[str, Any] = {}
    for key, value in data.items():
        safe_key = f"payload_{key}" if key in reserved else str(key)
        payload[safe_key] = value
    return payload
