"""Local browser audio review for official-runtime artifacts."""

from __future__ import annotations

import json
import math
import mimetypes
import re
import threading
import urllib.parse
import webbrowser
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Mapping

import click
import numpy as np

from .rerun_review import RunReview, TimelineRow, load_run_review


BACKEND_EVENT_TYPES = {
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_stopped",
    "conversation.item.input_audio_transcription.completed",
    "response.created",
    "response.done",
    "response.output_audio.done",
    "assistant.audio.started",
    "assistant.audio.done",
}

DEFAULT_RECOVERED_TEXT_MODEL = "m1max speech_to_speech parakeet-tdt"


@dataclass
class AudioReviewApp:
    payload: dict[str, Any]
    audio_files: dict[str, Path]
    audio_bytes: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class AlignedAudioReviewExport:
    output_dir: Path
    wav_path: Path
    labels_path: Path
    metadata_path: Path
    start_ts: float
    end_ts: float
    sample_rate: int
    placements: list[dict[str, Any]]
    labels: list[dict[str, Any]]
    alignment: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "wav_path": str(self.wav_path),
            "labels_path": str(self.labels_path),
            "metadata_path": str(self.metadata_path),
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_s": round(max(0.0, self.end_ts - self.start_ts), 3),
            "sample_rate": self.sample_rate,
            "channels": {"left": "input", "right": "output"},
            "alignment": self.alignment,
            "placements": self.placements,
            "labels": self.labels,
        }


@dataclass(frozen=True)
class RecoveredTextCandidate:
    response_id: str
    wav_path: Path
    metadata_path: Path
    sample_rate: int | None
    duration_s: float | None
    playback_start_ts: float | None
    playback_end_ts: float | None
    response_done_ts: float | None


@dataclass(frozen=True)
class RecoveredTextExport:
    path: Path
    rows: list[dict[str, Any]]
    candidate_count: int
    recovered_count: int
    skipped_existing_count: int
    empty_response_ids: list[str] = field(default_factory=list)


ResponseTextTranscriber = Callable[[RecoveredTextCandidate], str]


def build_audio_review_app(
    review: RunReview,
    aligned_export: AlignedAudioReviewExport | None = None,
    recovered_text: list[dict[str, Any]] | None = None,
) -> AudioReviewApp:
    """Convert a parsed run review into browser-facing JSON and local file mappings."""

    audio_files: dict[str, Path] = {}
    tracks: list[dict[str, Any]] = []
    chunks_by_stream: dict[str, list[dict[str, Any]]] = {}
    for chunk in review.audio_chunks:
        chunks_by_stream.setdefault(chunk.stream, []).append(
            {
                "ts": chunk.ts,
                "sample_start": chunk.sample_start,
                "samples": chunk.samples,
                "sample_rate": chunk.sample_rate,
                "rms": chunk.rms,
                "response_id": _response_id(chunk.data),
            }
        )

    playback_spans = _playback_spans_by_response(review.timeline)
    for stream in ("input", "output"):
        hint = next((item for item in review.audio_hints if item.stream == stream), None)
        if hint is None:
            continue
        audio_id = _safe_audio_id(stream, audio_files)
        audio_files[audio_id] = hint.wav_path
        track_chunks = chunks_by_stream.get(stream, [])
        track = {
            "id": audio_id,
            "stream": stream,
            "label": "Input mic" if stream == "input" else "Output speaker",
            "url": f"/audio/{audio_id}",
            "wav_path": str(hint.wav_path),
            "metadata_path": str(hint.metadata_path),
            "start_ts": hint.start_ts,
            "end_ts": hint.end_ts,
            "sample_start": hint.sample_start,
            "sample_end": hint.sample_end,
            "sample_rate": hint.sample_rate,
            "duration_s": _round_or_none(hint.duration_s),
            "chunks": track_chunks,
        }
        if stream == "output":
            track["segments"] = _output_segments(
                track_chunks,
                sample_rate=hint.sample_rate,
                playback_spans=playback_spans,
            )
        tracks.append(track)

    response_audio = []
    for hint in review.audio_hints:
        if not hint.stream.startswith("response-"):
            continue
        audio_id = _safe_audio_id(hint.stream, audio_files)
        audio_files[audio_id] = hint.wav_path
        playback_span = playback_spans.get(hint.response_id or "")
        response_audio.append(
            {
                "id": audio_id,
                "stream": hint.stream,
                "response_id": hint.response_id,
                "url": f"/audio/{audio_id}",
                "wav_path": str(hint.wav_path),
                "metadata_path": str(hint.metadata_path),
                "start_ts": hint.start_ts,
                "end_ts": hint.end_ts,
                "playback_start_ts": playback_span.get("start_ts") if playback_span else None,
                "playback_end_ts": playback_span.get("end_ts") if playback_span else None,
                "duration_s": _round_or_none(hint.duration_s),
                "sample_start": hint.sample_start,
                "sample_end": hint.sample_end,
                "sample_rate": hint.sample_rate,
            }
        )

    backend_events = [_backend_event_payload(row) for row in review.timeline if _is_backend_audio_event(row)]
    backend_events.sort(key=lambda item: item["ts"])
    markers = [_marker_payload(row) for row in review.timeline if row.lane == "markers"]
    turns = _turn_payloads(review)

    timeline_values = []
    for track in tracks:
        timeline_values.extend([track.get("start_ts"), track.get("end_ts")])
    timeline_values.extend(event["ts"] for event in backend_events)
    timeline_values.extend(marker["ts"] for marker in markers)
    start_ts, end_ts = _timeline_bounds(timeline_values)

    payload = {
        "run_id": review.run_id,
        "run_root": str(review.run_root),
        "manifest_path": str(review.manifest_path),
        "timeline": {"start_ts": start_ts, "end_ts": end_ts, "duration_s": round(max(0.0, end_ts - start_ts), 3)},
        "tracks": tracks,
        "backend_events": backend_events,
        "turns": turns,
        "markers": markers,
        "response_audio": response_audio,
    }
    if aligned_export is not None:
        audio_files["aligned"] = aligned_export.wav_path
        payload["aligned_audio"] = {
            "url": "/audio/aligned",
            "wav_path": str(aligned_export.wav_path),
            "labels_path": str(aligned_export.labels_path),
            "metadata_path": str(aligned_export.metadata_path),
            "sample_rate": aligned_export.sample_rate,
            "start_ts": aligned_export.start_ts,
            "end_ts": aligned_export.end_ts,
            "duration_s": round(max(0.0, aligned_export.end_ts - aligned_export.start_ts), 3),
            "channels": {"left": "input", "right": "output"},
            "alignment": aligned_export.alignment,
        }
        payload["timeline"] = {
            "start_ts": aligned_export.start_ts,
            "end_ts": aligned_export.end_ts,
            "duration_s": round(max(0.0, aligned_export.end_ts - aligned_export.start_ts), 3),
        }
        payload["lanes"] = _semantic_lanes_payload(
            review,
            start_ts=aligned_export.start_ts,
            end_ts=aligned_export.end_ts,
            recovered_text=recovered_text or [],
        )
    return AudioReviewApp(payload=payload, audio_files=audio_files)


def export_aligned_audio_review(
    review: RunReview,
    *,
    output_dir: Path | None = None,
    sample_rate: int | None = None,
) -> AlignedAudioReviewExport:
    """Export a sidecar-aligned stereo WAV and Audacity label file.

    Left channel is robot mic input. Right channel is robot speaker output. Input is placed on
    the recording sample clock anchored at the first input chunk. Output is placed per response,
    anchored at ``assistant.audio.started`` when present, so response gaps remain audible.
    """

    target_sample_rate = sample_rate or _dominant_sample_rate(review) or 16000
    output_dir = output_dir or review.run_root / "audio-review" / review.run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    placements, source_audio, alignment = _aligned_audio_placements(review, sample_rate=target_sample_rate)
    labels = _aligned_audio_labels(review)
    bounds = _aligned_timeline_bounds(placements, labels)
    start_ts, end_ts = bounds
    total_frames = max(1, int(math.ceil((end_ts - start_ts) * target_sample_rate)))
    canvas = np.zeros((total_frames, 2), dtype=np.float32)

    for placement in placements:
        source_key = str(placement["source_path"])
        source = source_audio[source_key]
        audio = source["audio"][placement["source_sample_start"] : placement["source_sample_end"]]
        audio = _resample_audio(audio, int(source["sample_rate"]), target_sample_rate)
        _mix_audio(canvas, audio, channel=int(placement["channel"]), anchor_ts=float(placement["start_ts"]), start_ts=start_ts, sample_rate=target_sample_rate)

    np.clip(canvas, -1.0, 1.0, out=canvas)
    stem = f"audio-review-{review.run_id}"
    wav_path = output_dir / f"{stem}.wav"
    labels_path = output_dir / f"{stem}.labels.txt"
    metadata_path = output_dir / f"{stem}.json"

    _write_stereo_pcm16_wav(wav_path, canvas, sample_rate=target_sample_rate)
    _write_audacity_labels(labels_path, labels, timeline_start_ts=start_ts)

    export = AlignedAudioReviewExport(
        output_dir=output_dir,
        wav_path=wav_path,
        labels_path=labels_path,
        metadata_path=metadata_path,
        start_ts=round(start_ts, 3),
        end_ts=round(end_ts, 3),
        sample_rate=target_sample_rate,
        placements=placements,
        labels=labels,
        alignment=alignment,
    )
    metadata_path.write_text(json.dumps(export.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return export


def recover_missing_response_text(
    review: RunReview,
    *,
    output_path: Path | None = None,
    overwrite: bool = False,
    transcribe: ResponseTextTranscriber | None = None,
    model: str = DEFAULT_RECOVERED_TEXT_MODEL,
) -> RecoveredTextExport:
    """Transcribe response WAVs whose backend assistant transcript is missing."""

    target_path = output_path or _default_recovered_text_sidecar(review)
    existing_rows = [] if overwrite else _load_recovered_text_sidecar(target_path)
    existing_response_ids = set(_recovered_text_by_response(existing_rows))
    all_candidates = _missing_response_text_candidates(review)
    candidates = [
        candidate
        for candidate in all_candidates
        if overwrite or candidate.response_id not in existing_response_ids
    ]
    skipped_existing_count = len(all_candidates) - len(candidates)
    if transcribe is None and candidates:
        transcribe = _m1max_parakeet_response_transcriber()

    rows = list(existing_rows)
    empty_response_ids: list[str] = []
    generated_on = datetime.now(timezone.utc).isoformat()
    for candidate in candidates:
        if not candidate.wav_path.exists():
            raise click.ClickException(f"response WAV is missing for {candidate.response_id}: {candidate.wav_path}")
        text = transcribe(candidate).strip() if transcribe is not None else ""
        if not text:
            empty_response_ids.append(candidate.response_id)
            continue
        rows.append(
            _recovered_text_row(
                review,
                candidate,
                text=text,
                model=model,
                generated_on=generated_on,
            )
        )

    if overwrite:
        rows = sorted(rows, key=_recovered_text_sort_key)
    _write_recovered_text_sidecar(target_path, rows)
    return RecoveredTextExport(
        path=target_path,
        rows=rows,
        candidate_count=len(all_candidates),
        recovered_count=len(candidates) - len(empty_response_ids),
        skipped_existing_count=skipped_existing_count,
        empty_response_ids=empty_response_ids,
    )


def serve_audio_review(app: AudioReviewApp, *, host: str, port: int, open_browser: bool) -> None:
    """Run the local blocking HTTP server."""

    handler_cls = make_audio_review_handler(app)
    server = ThreadingHTTPServer((host, port), handler_cls)
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/"
    click.echo(f"audio review: {url}")
    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("stopping audio review")
    finally:
        server.server_close()


def make_audio_review_handler(app: AudioReviewApp) -> type[BaseHTTPRequestHandler]:
    class AudioReviewHandler(BaseHTTPRequestHandler):
        server_version = "ReachyAudioReview/0.1"

        def do_GET(self) -> None:  # noqa: N802
            self._handle_request(send_body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle_request(send_body=False)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _handle_request(self, *, send_body: bool) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path in {"", "/"}:
                self._send_bytes(_HTML.encode("utf-8"), content_type="text/html; charset=utf-8", send_body=send_body)
                return
            if path == "/api/review":
                body = json.dumps(app.payload, sort_keys=True).encode("utf-8")
                self._send_bytes(body, content_type="application/json; charset=utf-8", send_body=send_body)
                return
            if path.startswith("/audio/"):
                audio_id = urllib.parse.unquote(path.removeprefix("/audio/"))
                self._send_audio(audio_id, send_body=send_body)
                return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def _send_bytes(self, body: bytes, *, content_type: str, send_body: bool) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _send_audio(self, audio_id: str, *, send_body: bool) -> None:
            path = app.audio_files.get(audio_id)
            if path is None or not path.exists() or not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "audio not found")
                return

            body = _browser_wav_bytes(app, audio_id, path)
            size = len(body)
            start, end = _parse_range(self.headers.get("Range"), size=size)
            if start is None or end is None:
                start, end = 0, size - 1
                status = HTTPStatus.OK
            else:
                status = HTTPStatus.PARTIAL_CONTENT
            length = max(0, end - start + 1)
            content_type = mimetypes.guess_type(path.name)[0] or "audio/wav"

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not send_body:
                return
            self.wfile.write(body[start : end + 1])

    return AudioReviewHandler


@click.command(context_settings={"show_default": True})
@click.argument("run_path", type=click.Path(path_type=Path))
@click.option("--run-id", help="Run id when RUN_PATH contains multiple manifests.")
@click.option("--output-dir", type=click.Path(path_type=Path), help="Directory for aligned WAV/label export.")
@click.option("--sample-rate", type=int, help="Aligned export sample rate.")
@click.option(
    "--recovered-text",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Optional JSONL sidecar with STT-recovered assistant response text. With --recover-missing-text, this is the output path.",
)
@click.option(
    "--recover-missing-text",
    is_flag=True,
    help="Transcribe missing assistant response WAVs with the m1max Parakeet STT handler, write the recovered-text sidecar, then exit.",
)
@click.option(
    "--overwrite-recovered-text",
    is_flag=True,
    help="Re-transcribe all missing assistant response WAVs instead of preserving existing sidecar rows.",
)
@click.option("--serve", is_flag=True, help="Start the experimental browser server instead of exporting files.")
@click.option("--host", default="127.0.0.1", help="Local bind host.")
@click.option("--port", default=8766, type=int, help="Local bind port. Use 0 for any free port.")
@click.option("--open/--no-open", "open_browser", default=True, help="Open the review page in the browser.")
@click.option("--json-output", is_flag=True, help="Print review JSON instead of starting the server.")
def cli(
    run_path: Path,
    run_id: str | None,
    output_dir: Path | None,
    sample_rate: int | None,
    recovered_text: Path | None,
    recover_missing_text: bool,
    overwrite_recovered_text: bool,
    serve: bool,
    host: str,
    port: int,
    open_browser: bool,
    json_output: bool,
) -> None:
    """Export an aligned audio review for one official-runtime run."""

    review = load_run_review(run_path, run_id=run_id)
    recovered_text_path = recovered_text or _default_recovered_text_sidecar(review)
    if recover_missing_text:
        export = recover_missing_response_text(
            review,
            output_path=recovered_text_path,
            overwrite=overwrite_recovered_text,
        )
        click.echo("recovered text sidecar written:")
        click.echo(f"  path: {export.path}")
        click.echo(f"  missing response wavs: {export.candidate_count}")
        click.echo(f"  recovered rows: {export.recovered_count}")
        if export.skipped_existing_count:
            click.echo(f"  skipped existing rows: {export.skipped_existing_count}")
        if export.empty_response_ids:
            click.echo(f"  empty transcriptions: {', '.join(export.empty_response_ids)}")
        return
    recovered_text_rows = _load_recovered_text_sidecar(recovered_text_path)
    if json_output:
        app = build_audio_review_app(review, recovered_text=recovered_text_rows)
        click.echo(json.dumps(app.payload, indent=2, sort_keys=True))
        return
    export = export_aligned_audio_review(review, output_dir=output_dir, sample_rate=sample_rate)
    if not serve:
        click.echo("aligned audio review exported:")
        click.echo(f"  wav: {export.wav_path}")
        click.echo(f"  labels: {export.labels_path}")
        click.echo(f"  metadata: {export.metadata_path}")
        click.echo("Open the WAV in Audacity, then import the labels file as a label track.")
        return
    app = build_audio_review_app(review, aligned_export=export, recovered_text=recovered_text_rows)
    serve_audio_review(app, host=host, port=port, open_browser=open_browser)


def main() -> None:
    cli()


def _is_backend_audio_event(row: TimelineRow) -> bool:
    normalized = _normalized_type(row.type)
    return normalized in BACKEND_EVENT_TYPES


def _browser_wav_bytes(app: AudioReviewApp, audio_id: str, path: Path) -> bytes:
    cached = app.audio_bytes.get(audio_id)
    if cached is not None:
        return cached
    try:
        import soundfile as sf

        audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
        arr = np.asarray(audio, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        elif arr.ndim == 2:
            if arr.shape[1] > 2:
                arr = arr[:, :2]
        else:
            arr = arr.reshape(-1, 1)
        max_abs = float(np.max(np.abs(arr))) if arr.size else 0.0
        if max_abs > 1.5:
            arr = arr / 32768.0
        arr = np.clip(arr, -1.0, 1.0)
        pcm16 = (arr * 32767.0).astype("<i2")
        buf = BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(int(arr.shape[1]))
            wav.setsampwidth(2)
            wav.setframerate(int(sample_rate))
            wav.writeframes(pcm16.tobytes())
        data = buf.getvalue()
    except Exception:  # noqa: BLE001
        data = path.read_bytes()
    app.audio_bytes[audio_id] = data
    return data


def _aligned_audio_placements(
    review: RunReview,
    *,
    sample_rate: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    placements: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    alignment: dict[str, Any] = {"input_anchor_mode": "unavailable"}

    input_hint = _audio_hint_for_stream(review, "input")
    if input_hint is not None:
        input_audio, input_sample_rate = _read_mono_float32(input_hint.wav_path)
        sources[str(input_hint.wav_path)] = {"audio": input_audio, "sample_rate": input_sample_rate}
        sample_start = max(0, int(input_hint.sample_start))
        sample_end = min(int(input_hint.sample_end), int(input_audio.shape[0]))
        if sample_end > sample_start:
            input_chunks = [chunk for chunk in review.audio_chunks if chunk.stream == "input"]
            anchor_ts, alignment = _input_anchor_from_first_speech_vad(
                review,
                chunks=input_chunks,
                sample_rate=input_sample_rate,
                fallback_start_ts=input_hint.start_ts,
            )
            placements.append(
                _audio_placement(
                    stream="input",
                    label="input mic",
                    channel=0,
                    source_path=input_hint.wav_path,
                    sample_start=sample_start,
                    sample_end=sample_end,
                    source_sample_rate=input_sample_rate,
                    target_sample_rate=sample_rate,
                    start_ts=float(anchor_ts),
                    anchor_mode=str(alignment.get("input_anchor_mode") or "unknown"),
                )
            )

    output_hint = _audio_hint_for_stream(review, "output")
    if output_hint is not None:
        output_audio, output_sample_rate = _read_mono_float32(output_hint.wav_path)
        sources[str(output_hint.wav_path)] = {"audio": output_audio, "sample_rate": output_sample_rate}
        output_chunks = [
            {
                "ts": chunk.ts,
                "sample_start": chunk.sample_start,
                "samples": chunk.samples,
                "sample_rate": chunk.sample_rate,
                "response_id": _response_id(chunk.data),
            }
            for chunk in review.audio_chunks
            if chunk.stream == "output"
        ]
        segments = _output_segments(
            output_chunks,
            sample_rate=output_hint.sample_rate or output_sample_rate,
            playback_spans=_playback_spans_by_response(review.timeline),
        )
        if segments:
            for segment in segments:
                sample_start = max(0, int(segment["sample_start"]))
                sample_end = min(int(segment["sample_end"]), int(output_audio.shape[0]))
                if sample_end <= sample_start:
                    continue
                response_id = segment.get("response_id")
                label = f"output speaker {response_id}" if response_id else "output speaker"
                placements.append(
                    _audio_placement(
                        stream="output",
                        label=label,
                        channel=1,
                        source_path=output_hint.wav_path,
                        sample_start=sample_start,
                        sample_end=sample_end,
                        source_sample_rate=output_sample_rate,
                        target_sample_rate=sample_rate,
                        start_ts=float(segment["start_ts"]),
                        response_id=response_id if isinstance(response_id, str) else None,
                    )
                )
        else:
            sample_start = max(0, int(output_hint.sample_start))
            sample_end = min(int(output_hint.sample_end), int(output_audio.shape[0]))
            if sample_end > sample_start:
                anchor_ts = output_hint.start_ts or 0.0
                placements.append(
                    _audio_placement(
                        stream="output",
                        label="output speaker",
                        channel=1,
                        source_path=output_hint.wav_path,
                        sample_start=sample_start,
                        sample_end=sample_end,
                        source_sample_rate=output_sample_rate,
                        target_sample_rate=sample_rate,
                        start_ts=float(anchor_ts),
                    )
                )

    if not placements:
        raise ValueError(f"run {review.run_id} has no usable input/output WAV placements")
    return sorted(placements, key=lambda item: (item["start_ts"], item["channel"], item["source_sample_start"])), sources, alignment


def _audio_placement(
    *,
    stream: str,
    label: str,
    channel: int,
    source_path: Path,
    sample_start: int,
    sample_end: int,
    source_sample_rate: int,
    target_sample_rate: int,
    start_ts: float,
    response_id: str | None = None,
    anchor_mode: str | None = None,
) -> dict[str, Any]:
    source_duration_s = max(0.0, (sample_end - sample_start) / float(source_sample_rate))
    target_duration_s = max(0.0, math.ceil(source_duration_s * target_sample_rate) / float(target_sample_rate))
    payload: dict[str, Any] = {
        "stream": stream,
        "label": label,
        "channel": channel,
        "source_path": str(source_path),
        "source_sample_start": sample_start,
        "source_sample_end": sample_end,
        "source_sample_rate": source_sample_rate,
        "target_sample_rate": target_sample_rate,
        "start_ts": round(start_ts, 3),
        "end_ts": round(start_ts + target_duration_s, 3),
        "duration_s": round(target_duration_s, 3),
        "start_s": None,
        "end_s": None,
    }
    if response_id:
        payload["response_id"] = response_id
    if anchor_mode:
        payload["anchor_mode"] = anchor_mode
    return payload


def _aligned_audio_labels(review: RunReview) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    for turn in _turn_payloads(review):
        index = int(turn["index"])
        transcript = str(turn.get("transcript") or "").strip()
        speech_start = _float_value(turn.get("speech_start_ts")) or float(turn["transcript_ts"])
        speech_stop = _float_value(turn.get("speech_stop_ts")) or max(speech_start, float(turn["transcript_ts"]))
        _append_label(labels, speech_start, speech_stop, f"turn {index:02d} input: {transcript}")

        first_audio = _float_value(turn.get("first_audio_ts"))
        response_done = _float_value(turn.get("response_done_ts"))
        audio_done = _float_value(turn.get("audio_done_ts"))
        backend_wait_end = first_audio if first_audio is not None else response_done
        if backend_wait_end is not None:
            _append_label(labels, float(turn["transcript_ts"]), backend_wait_end, f"turn {index:02d} backend wait")
        if first_audio is not None:
            response_id = turn.get("response_id")
            output_end = audio_done if audio_done is not None else first_audio
            suffix = f" {response_id}" if response_id else ""
            _append_label(labels, first_audio, output_end, f"turn {index:02d} output{suffix}")

    for row in review.timeline:
        if _is_backend_audio_event(row):
            normalized = _normalized_type(row.type)
            _append_label(labels, row.ts, row.ts, _event_label(row, normalized))
        elif row.lane == "markers":
            marker = _marker_payload(row)
            note = marker.get("note") or "marker"
            number = marker.get("n")
            prefix = f"M{number}: " if number not in (None, "") else ""
            _append_label(labels, row.ts, row.ts, f"{prefix}{note}")

    return sorted(labels, key=lambda item: (item["start_ts"], item["end_ts"], item["label"]))


def _semantic_lanes_payload(
    review: RunReview,
    *,
    start_ts: float,
    end_ts: float,
    recovered_text: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    lane_defs = [
        ("vad", "VAD"),
        ("stt_partial", "STT partial"),
        ("stt_final", "STT final"),
        ("llm_input", "Transcript to LLM"),
        ("llm", "S2S response lifecycle"),
        ("llm_response", "Assistant text transcript"),
        ("stt_recovered", "STT recovered transcript"),
        ("transcript_availability", "Transcript availability"),
        ("tts", "TTS / audio generation"),
        ("robot_audio", "Robot audio playback"),
        ("policy", "Policy / cues"),
        ("markers", "Human markers"),
    ]
    lanes: dict[str, dict[str, Any]] = {
        lane_id: {"id": lane_id, "label": label, "events": []} for lane_id, label in lane_defs
    }

    def in_range(ts: float) -> bool:
        return start_ts <= ts <= end_ts

    def add(
        lane_id: str,
        ts: float,
        label: str,
        *,
        detail: str | None = None,
        end: float | None = None,
        kind: str = "marker",
        row: TimelineRow | None = None,
        response_id: str | None = None,
    ) -> None:
        if not in_range(ts) and (end is None or end < start_ts or ts > end_ts):
            return
        lane = lanes.get(lane_id)
        if lane is None:
            return
        event = {
            "kind": kind,
            "ts": round(ts, 3),
            "t": round(max(0.0, ts - start_ts), 3),
            "label": label,
        }
        if end is not None:
            event["end_ts"] = round(end, 3)
            event["end_t"] = round(max(event["t"] + 0.01, end - start_ts), 3)
        if detail:
            event["detail"] = detail
        if row is not None:
            event["type"] = row.type
            event["source"] = row.source
        if response_id:
            event["response_id"] = response_id
        lane["events"].append(event)

    rows = sorted(review.timeline, key=lambda item: (item.ts, item.lane, item.line))
    response_done_by_id = _response_done_ts_by_response(rows)
    recovered_text_by_id = _recovered_text_by_response(recovered_text or [])
    transcript_done_by_id: dict[str, TimelineRow] = {}
    assistant_handler_output_by_id: dict[str, TimelineRow] = {}
    unscoped_assistant_handler_outputs = 0
    for row in rows:
        normalized = _normalized_type(row.type)
        response_id = _response_id(row.data)
        if normalized == "response.output_audio_transcript.done" and response_id:
            transcript_done_by_id[response_id] = row
        elif row.type == "handler.output":
            role, _content = _handler_output_role_content(row)
            if role == "assistant":
                if response_id:
                    assistant_handler_output_by_id[response_id] = row
                else:
                    unscoped_assistant_handler_outputs += 1

    speech_starts = [row for row in rows if _normalized_type(row.type) == "input_audio_buffer.speech_started"]
    speech_stops = [row for row in rows if _normalized_type(row.type) == "input_audio_buffer.speech_stopped"]
    for index, start_row in enumerate(speech_starts, start=1):
        stop_row = next((row for row in speech_stops if row.ts >= start_row.ts), None)
        add(
            "vad",
            start_row.ts,
            f"speech {index:02d}",
            end=stop_row.ts if stop_row else start_row.ts,
            kind="span",
            row=start_row,
        )

    audio_starts = [row for row in rows if row.type == "assistant.audio.started"]
    audio_dones = [row for row in rows if row.type == "assistant.audio.done"]
    for index, start_row in enumerate(audio_starts, start=1):
        done_row = next((row for row in audio_dones if row.ts >= start_row.ts), None)
        response_id = _response_id(start_row.data)
        add(
            "llm",
            start_row.ts,
            "first audio",
            detail=response_id,
            row=start_row,
            response_id=response_id,
        )
        if done_row is not None:
            add(
                "llm",
                done_row.ts,
                "audio done",
                detail=response_id,
                row=done_row,
                response_id=response_id,
            )
        if response_id:
            transcript_row = transcript_done_by_id.get(response_id)
            handler_row = assistant_handler_output_by_id.get(response_id)
            has_backend_transcript = transcript_row is not None
            recovered_row = recovered_text_by_id.get(response_id) if not has_backend_transcript else None
            detail_parts = [
                f"response_id={response_id}",
                f"first_audio={start_row.ts:.3f}",
                f"audio_done={done_row.ts:.3f}" if done_row is not None else "audio_done=not recorded",
                f"response.done={response_done_by_id[response_id]:.3f}" if response_id in response_done_by_id else "response.done=not recorded",
                (
                    f"response.output_audio_transcript.done={transcript_row.ts:.3f}"
                    if transcript_row is not None
                    else "response.output_audio_transcript.done=not recorded"
                ),
                (
                    f"assistant handler.output={handler_row.ts:.3f}"
                    if handler_row is not None
                    else "assistant handler.output=response_id not recorded"
                ),
            ]
            if unscoped_assistant_handler_outputs and handler_row is None:
                detail_parts.append(f"unscoped assistant handler.output rows={unscoped_assistant_handler_outputs}")
            if recovered_row is not None:
                detail_parts.append("stt_recovered_text=available")
            add(
                "transcript_availability",
                start_row.ts,
                "backend transcript logged" if has_backend_transcript else "no backend transcript event",
                detail="\n".join(detail_parts),
                end=done_row.ts if done_row is not None else start_row.ts,
                kind="span",
                row=transcript_row or start_row,
                response_id=response_id,
            )
            if recovered_row is not None:
                recovered_label = str(recovered_row.get("text") or "").strip()
                recovered_detail = _recovered_text_detail(recovered_row)
                add(
                    "stt_recovered",
                    start_row.ts,
                    recovered_label or "STT recovered transcript",
                    detail=recovered_detail,
                    end=done_row.ts if done_row is not None else start_row.ts,
                    kind="span",
                    row=start_row,
                    response_id=response_id,
                )
        add(
            "robot_audio",
            start_row.ts,
            f"playback {index:02d}",
            detail=response_id,
            end=done_row.ts if done_row else start_row.ts,
            kind="span",
            row=start_row,
            response_id=response_id,
        )

    output_audio_start_by_response: dict[str, float] = {}
    output_audio_done_by_response: dict[str, float] = {}
    for row in rows:
        response_id = _response_id(row.data)
        if not response_id:
            continue
        if row.type == "hf.realtime.response.output_audio.delta":
            output_audio_start_by_response.setdefault(response_id, row.ts)
        elif _normalized_type(row.type) == "response.output_audio.done":
            output_audio_done_by_response[response_id] = row.ts
    for response_id, done_ts in output_audio_done_by_response.items():
        created_ts = output_audio_start_by_response.get(response_id, done_ts)
        add("tts", created_ts, "audio generation", detail=response_id, end=done_ts, kind="span", response_id=response_id)

    for row in rows:
        normalized = _normalized_type(row.type)
        response_id = _response_id(row.data)
        if normalized == "conversation.item.input_audio_transcription.delta":
            text = str(row.data.get("delta") or "")
            add("stt_partial", row.ts, text or "partial", row=row)
        elif normalized == "conversation.item.input_audio_transcription.completed":
            text = _event_text(row) or "final transcript"
            add("stt_final", row.ts, text, row=row)
        elif row.type == "handler.output":
            role, content = _handler_output_role_content(row)
            if role == "user":
                add("llm_input", row.ts, content or "user transcript", row=row)
            elif role == "assistant":
                add("llm_response", row.ts, content or "assistant text", row=row)
        elif normalized == "response.created":
            add("llm", row.ts, "response.created signal", detail=response_id, row=row, response_id=response_id)
        elif normalized == "response.done":
            usage = _response_usage_text(row)
            add("llm", row.ts, "response done", detail=usage or response_id, row=row, response_id=response_id)
        elif normalized == "response.output_audio_transcript.done":
            text = _event_text(row) or "assistant transcript"
            add("llm_response", row.ts, text, row=row, response_id=response_id)
        elif normalized == "response.output_audio.done":
            add("tts", row.ts, "audio done", detail=response_id, row=row, response_id=response_id)
        elif row.type.startswith("policy.") or row.type.startswith("runtime.antenna_cue") or row.type.startswith("runtime.ready_cue"):
            add("policy", row.ts, _policy_label(row), detail=_policy_detail(row), row=row)
        elif row.lane == "markers":
            marker = _marker_payload(row)
            note = marker.get("note") or "marker"
            number = marker.get("n")
            label = f"M{number}: {note}" if number not in (None, "") else str(note)
            add("markers", row.ts, label, row=row)

    return [
        {"id": lane["id"], "label": lane["label"], "events": sorted(lane["events"], key=lambda item: (item["t"], item.get("end_t", item["t"]), item["label"]))}
        for lane in lanes.values()
    ]


def _recovered_text_by_response(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_response: dict[str, dict[str, Any]] = {}
    for row in rows:
        response_id = row.get("response_id")
        text = row.get("text")
        if not isinstance(response_id, str) or not response_id or not isinstance(text, str) or not text.strip():
            continue
        by_response[response_id] = {**row, "text": text.strip()}
    return by_response


def _recovered_text_detail(row: Mapping[str, Any]) -> str:
    parts = [
        "provenance=STT recovered from robot response WAV",
        "trust=derived/fallible; not backend-emitted transcript",
    ]
    for key in ("response_id", "source", "model", "generated_on", "audio_path"):
        value = row.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key}={value}")
    text = row.get("text")
    if isinstance(text, str) and text:
        parts.append(f"text={text}")
    return "\n".join(parts)


def _load_recovered_text_sidecar(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"{path}:{line_no}: invalid recovered-text JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise click.ClickException(f"{path}:{line_no}: recovered-text row must be a JSON object")
        response_id = payload.get("response_id")
        text = payload.get("text")
        if not isinstance(response_id, str) or not response_id:
            raise click.ClickException(f"{path}:{line_no}: recovered-text row missing response_id")
        if not isinstance(text, str) or not text.strip():
            raise click.ClickException(f"{path}:{line_no}: recovered-text row missing text")
        rows.append({**payload, "text": text.strip()})
    return rows


def _default_recovered_text_sidecar(review: RunReview) -> Path:
    return review.run_root / "audio-review" / review.run_id / f"recovered-text-{review.run_id}.jsonl"


def _missing_response_text_candidates(review: RunReview) -> list[RecoveredTextCandidate]:
    rows = sorted(review.timeline, key=lambda item: (item.ts, item.lane, item.line))
    assistant_text_ids = _assistant_text_response_ids(rows)
    playback_spans = _playback_spans_by_response(rows)
    response_done_by_id = _response_done_ts_by_response(rows)
    candidates: list[RecoveredTextCandidate] = []
    seen: set[str] = set()
    for hint in sorted(
        review.audio_hints,
        key=lambda item: (
            item.start_ts if item.start_ts is not None else float("inf"),
            item.stream,
            str(item.wav_path),
        ),
    ):
        if not hint.stream.startswith("response-") or not hint.response_id:
            continue
        response_id = hint.response_id
        if response_id in seen or response_id in assistant_text_ids:
            continue
        seen.add(response_id)
        playback_span = playback_spans.get(response_id)
        candidates.append(
            RecoveredTextCandidate(
                response_id=response_id,
                wav_path=hint.wav_path,
                metadata_path=hint.metadata_path,
                sample_rate=hint.sample_rate,
                duration_s=_round_or_none(hint.duration_s),
                playback_start_ts=playback_span.get("start_ts") if playback_span else None,
                playback_end_ts=playback_span.get("end_ts") if playback_span else None,
                response_done_ts=response_done_by_id.get(response_id),
            )
        )
    return candidates


def _assistant_text_response_ids(rows: list[TimelineRow]) -> set[str]:
    response_ids: set[str] = set()
    for row in rows:
        response_id = _response_id(row.data)
        if not response_id:
            continue
        normalized = _normalized_type(row.type)
        if normalized == "response.output_audio_transcript.done" and _event_text(row):
            response_ids.add(response_id)
        elif row.type == "handler.output":
            role, content = _handler_output_role_content(row)
            if role == "assistant" and content:
                response_ids.add(response_id)
    return response_ids


def _write_recovered_text_sidecar(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(row, sort_keys=True) for row in rows)
    path.write_text(body + ("\n" if body else ""), encoding="utf-8")


def _recovered_text_row(
    review: RunReview,
    candidate: RecoveredTextCandidate,
    *,
    text: str,
    model: str,
    generated_on: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "run_id": review.run_id,
        "response_id": candidate.response_id,
        "text": _clean_label(text),
        "source": "stt-recovered",
        "model": model,
        "generated_on": generated_on,
        "audio_path": str(candidate.wav_path),
    }
    if candidate.sample_rate is not None:
        row["sample_rate"] = candidate.sample_rate
    if candidate.duration_s is not None:
        row["duration_s"] = candidate.duration_s
    if candidate.playback_start_ts is not None:
        row["playback_start_ts"] = round(candidate.playback_start_ts, 3)
    if candidate.playback_end_ts is not None:
        row["playback_end_ts"] = round(candidate.playback_end_ts, 3)
    if candidate.response_done_ts is not None:
        row["response_done_ts"] = round(candidate.response_done_ts, 3)
    return row


def _recovered_text_sort_key(row: Mapping[str, Any]) -> tuple[float, str]:
    playback_start = row.get("playback_start_ts")
    response_done = row.get("response_done_ts")
    if isinstance(playback_start, (int, float)):
        ts = float(playback_start)
    elif isinstance(response_done, (int, float)):
        ts = float(response_done)
    else:
        ts = float("inf")
    response_id = row.get("response_id")
    return ts, response_id if isinstance(response_id, str) else ""


def _m1max_parakeet_response_transcriber() -> ResponseTextTranscriber:
    try:
        from queue import Queue
        from threading import Event

        from speech_to_speech.STT.parakeet_tdt_handler import ParakeetTDTSTTHandler
        from speech_to_speech.pipeline.handler_types import STTIn
    except ImportError as exc:
        raise click.ClickException(
            "Missing speech_to_speech Parakeet STT package. On m1max, use "
            "scripts/m1max/recover_audio_review_text.sh <run_id> so recovery runs under the S2S "
            "backend Python environment."
        ) from exc

    handler = ParakeetTDTSTTHandler(
        Event(),
        Queue(),
        Queue(),
        setup_kwargs={"device": "auto", "enable_live_transcription": False},
    )

    def transcribe(candidate: RecoveredTextCandidate) -> str:
        audio, sample_rate = _read_mono_float32(candidate.wav_path)
        if sample_rate != 16000:
            audio = _resample_audio(audio, sample_rate, 16000)
        vad_audio = STTIn(
            audio=np.asarray(audio, dtype=np.float32),
            mode="final",
            turn_id=candidate.response_id,
            turn_revision=0,
        )
        for output in handler.process(vad_audio):
            text = getattr(output, "text", None)
            if isinstance(text, str) and text.strip():
                return _clean_label(text)
        return ""

    return transcribe


def _handler_output_role_content(row: TimelineRow) -> tuple[str | None, str | None]:
    item = row.data.get("item")
    if not isinstance(item, Mapping):
        return None, None
    role = item.get("role")
    content = item.get("content")
    return (role if isinstance(role, str) else None, content.strip() if isinstance(content, str) else None)


def _response_usage_text(row: TimelineRow) -> str | None:
    response = row.data.get("response")
    if not isinstance(response, Mapping):
        return None
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    parts = []
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    return " ".join(parts) if parts else None


def _policy_label(row: TimelineRow) -> str:
    label = row.type.removeprefix("policy.").removeprefix("runtime.")
    reason = row.data.get("reason")
    if isinstance(reason, str) and reason:
        return f"{label}: {reason}"
    return label


def _policy_detail(row: TimelineRow) -> str | None:
    parts = []
    for key in ("cue", "phase", "event_phase", "action", "event_kind"):
        value = row.data.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return " ".join(parts) if parts else None


def _append_label(labels: list[dict[str, Any]], start_ts: float, end_ts: float, label: str) -> None:
    start = float(start_ts)
    end = max(start, float(end_ts))
    labels.append({"start_ts": round(start, 3), "end_ts": round(end, 3), "label": _clean_label(label)})


def _aligned_timeline_bounds(
    placements: list[dict[str, Any]],
    labels: list[dict[str, Any]],
) -> tuple[float, float]:
    starts = [float(item["start_ts"]) for item in placements]
    ends = [float(item["end_ts"]) for item in placements]
    starts.extend(float(item["start_ts"]) for item in labels)
    ends.extend(float(item["end_ts"]) for item in labels)
    start_ts = min(starts)
    end_ts = max(ends)
    if end_ts <= start_ts:
        end_ts = start_ts + 1.0
    for item in placements:
        item["start_s"] = round(float(item["start_ts"]) - start_ts, 3)
        item["end_s"] = round(float(item["end_ts"]) - start_ts, 3)
    return round(start_ts, 3), round(end_ts, 3)


def _audio_hint_for_stream(review: RunReview, stream: str) -> Any | None:
    return next((hint for hint in review.audio_hints if hint.stream == stream), None)


def _input_anchor_from_first_speech_vad(
    review: RunReview,
    *,
    chunks: list[Any],
    sample_rate: int,
    fallback_start_ts: float | None,
) -> tuple[float, dict[str, Any]]:
    transcripts = [
        row
        for row in review.timeline
        if _normalized_type(row.type) == "conversation.item.input_audio_transcription.completed" and _event_text(row)
    ]
    first_transcript = transcripts[0] if transcripts else None
    speech_starts = [
        row
        for row in review.timeline
        if _normalized_type(row.type) == "input_audio_buffer.speech_started"
        and (first_transcript is None or row.ts <= first_transcript.ts)
    ]
    if first_transcript is not None and speech_starts and chunks:
        speech_start = speech_starts[-1]
        nearest_chunk = min(chunks, key=lambda chunk: abs(float(chunk.ts) - speech_start.ts))
        anchor_ts = speech_start.ts - nearest_chunk.sample_start / float(sample_rate)
        return anchor_ts, {
            "input_anchor_mode": "first_speech_vad_sync",
            "input_wav_start_ts": round(anchor_ts, 3),
            "anchor_event": {
                "ts": speech_start.ts,
                "type": speech_start.type,
                "transcript_ts": first_transcript.ts,
                "transcript": _event_text(first_transcript),
            },
            "anchor_chunk": {
                "ts": nearest_chunk.ts,
                "sample_start": nearest_chunk.sample_start,
                "sample_end": nearest_chunk.sample_end,
                "samples": nearest_chunk.samples,
                "sample_rate": nearest_chunk.sample_rate or sample_rate,
                "delta_to_anchor_event_s": round(nearest_chunk.ts - speech_start.ts, 6),
            },
        }

    anchor_ts = _continuous_stream_start_ts(
        chunks=chunks,
        sample_start=min((chunk.sample_start for chunk in chunks), default=0),
        sample_end=max((chunk.sample_end for chunk in chunks), default=0),
        sample_rate=sample_rate,
        fallback_start_ts=fallback_start_ts,
    )
    return anchor_ts, {
        "input_anchor_mode": "fallback_end_of_recording" if chunks else "fallback_recorder_start",
        "input_wav_start_ts": round(anchor_ts, 3),
        "reason": "no first transcript/speech-start/chunk sync point available",
    }


def _continuous_stream_start_ts(
    *,
    chunks: list[Any],
    sample_start: int,
    sample_end: int,
    sample_rate: int,
    fallback_start_ts: float | None,
) -> float:
    """Anchor a continuous stream by its last observed chunk.

    Input chunk timestamps can be bursty when buffered frames flush faster than real time at
    startup. Anchoring sample 0 to the first chunk timestamp then makes the whole mic track late.
    The end anchor preserves sample-clock playback while matching the last observed chunk wall time.
    """

    duration_s = max(0.0, (sample_end - sample_start) / float(sample_rate))
    if chunks:
        last_chunk = max(chunks, key=lambda chunk: (chunk.sample_end, chunk.ts))
        last_sample_rate = last_chunk.sample_rate or sample_rate
        stream_end_ts = last_chunk.ts + last_chunk.samples / float(last_sample_rate)
        return stream_end_ts - duration_s
    if fallback_start_ts is not None:
        return float(fallback_start_ts)
    return 0.0


def _dominant_sample_rate(review: RunReview) -> int | None:
    counts: dict[int, int] = {}
    for chunk in review.audio_chunks:
        if chunk.stream in {"input", "output"} and chunk.sample_rate:
            counts[int(chunk.sample_rate)] = counts.get(int(chunk.sample_rate), 0) + 1
    for hint in review.audio_hints:
        if hint.stream in {"input", "output"} and hint.sample_rate:
            counts[int(hint.sample_rate)] = counts.get(int(hint.sample_rate), 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _read_mono_float32(path: Path) -> tuple[np.ndarray, int]:
    try:
        import soundfile as sf

        audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
        arr = np.asarray(audio, dtype=np.float32)
        if arr.shape[1] > 1:
            arr = arr.mean(axis=1)
        else:
            arr = arr[:, 0]
    except Exception:
        with wave.open(str(path), "rb") as wav:
            sample_rate = int(wav.getframerate())
            channels = int(wav.getnchannels())
            sample_width = int(wav.getsampwidth())
            raw = wav.readframes(wav.getnframes())
        if sample_width != 2:
            raise ValueError(f"{path} is not readable as float or 16-bit PCM WAV")
        arr_i16 = np.frombuffer(raw, dtype="<i2")
        if channels > 1:
            arr_i16 = arr_i16.reshape(-1, channels).astype(np.float32).mean(axis=1).astype(np.int16)
        arr = arr_i16.astype(np.float32)

    arr = np.asarray(arr, dtype=np.float32).reshape(-1)
    max_abs = float(np.max(np.abs(arr))) if arr.size else 0.0
    if max_abs > 1.5:
        arr = arr / 32768.0
    return np.clip(arr, -1.0, 1.0), int(sample_rate)


def _resample_audio(audio: np.ndarray, source_sample_rate: int, target_sample_rate: int) -> np.ndarray:
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    if source_sample_rate == target_sample_rate or arr.size == 0:
        return arr
    from scipy.signal import resample_poly

    divisor = math.gcd(int(source_sample_rate), int(target_sample_rate))
    return np.asarray(
        resample_poly(arr, int(target_sample_rate) // divisor, int(source_sample_rate) // divisor),
        dtype=np.float32,
    )


def _mix_audio(
    canvas: np.ndarray,
    audio: np.ndarray,
    *,
    channel: int,
    anchor_ts: float,
    start_ts: float,
    sample_rate: int,
) -> None:
    target_start = int(round((anchor_ts - start_ts) * sample_rate))
    source_start = 0
    if target_start < 0:
        source_start = -target_start
        target_start = 0
    if source_start >= audio.shape[0] or target_start >= canvas.shape[0]:
        return
    frames = min(audio.shape[0] - source_start, canvas.shape[0] - target_start)
    if frames <= 0:
        return
    canvas[target_start : target_start + frames, channel] += audio[source_start : source_start + frames]


def _write_stereo_pcm16_wav(path: Path, audio: np.ndarray, *, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16.tobytes())


def _write_audacity_labels(path: Path, labels: list[dict[str, Any]], *, timeline_start_ts: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for item in labels:
        start_s = max(0.0, float(item["start_ts"]) - timeline_start_ts)
        end_s = max(start_s, float(item["end_ts"]) - timeline_start_ts)
        lines.append(f"{start_s:.3f}\t{end_s:.3f}\t{_clean_label(str(item['label']))}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _clean_label(value: str) -> str:
    return " ".join(value.replace("\t", " ").replace("\r", " ").replace("\n", " ").split())


def _float_value(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _backend_event_payload(row: TimelineRow) -> dict[str, Any]:
    normalized = _normalized_type(row.type)
    payload = {
        "ts": row.ts,
        "type": row.type,
        "normalized_type": normalized,
        "lane": row.lane,
        "source": row.source,
        "label": _event_label(row, normalized),
        "text": _event_text(row),
        "response_id": _response_id(row.data),
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _turn_payloads(review: RunReview) -> list[dict[str, Any]]:
    speech_started = [row for row in review.timeline if _normalized_type(row.type) == "input_audio_buffer.speech_started"]
    speech_stopped = [row for row in review.timeline if _normalized_type(row.type) == "input_audio_buffer.speech_stopped"]
    response_done_by_id = _response_done_ts_by_response(review.timeline)
    payloads: list[dict[str, Any]] = []
    previous_transcript_ts: float | None = None
    for turn in review.turns:
        payload = turn.to_dict()
        response_done_ts = response_done_by_id.get(turn.response_id or "")
        window_start = previous_transcript_ts if previous_transcript_ts is not None else float("-inf")
        starts = [row for row in speech_started if window_start < row.ts <= turn.transcript_ts]
        stops = [row for row in speech_stopped if window_start < row.ts <= turn.transcript_ts]
        speech_start_ts = starts[-1].ts if starts else None
        speech_stop_ts = stops[-1].ts if stops else None
        review_start_ts = speech_start_ts if speech_start_ts is not None else max(0.0, turn.transcript_ts - 1.0)
        review_end_ts = (
            turn.audio_done_ts
            or response_done_ts
            or turn.first_audio_ts
            or speech_stop_ts
            or turn.response_created_ts
            or turn.transcript_ts + 1.0
        )
        payload.update(
            {
                "speech_start_ts": speech_start_ts,
                "speech_stop_ts": speech_stop_ts,
                "response_done_ts": response_done_ts,
                "review_start_ts": review_start_ts,
                "review_end_ts": max(review_start_ts + 0.1, turn.transcript_ts, review_end_ts),
            }
        )
        payloads.append(payload)
        previous_transcript_ts = turn.transcript_ts
    return payloads


def _response_done_ts_by_response(rows: list[TimelineRow]) -> dict[str, float]:
    done_by_id: dict[str, float] = {}
    for row in sorted(rows, key=lambda item: (item.ts, item.lane, item.line)):
        if _normalized_type(row.type) != "response.done":
            continue
        response_id = _response_id(row.data)
        if response_id:
            done_by_id[response_id] = row.ts
    return done_by_id


def _output_segments(
    chunks: list[dict[str, Any]],
    *,
    sample_rate: int | None,
    playback_spans: Mapping[str, dict[str, float]],
) -> list[dict[str, Any]]:
    sr = sample_rate or 16000
    grouped: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        response_id = chunk.get("response_id")
        key = response_id if isinstance(response_id, str) and response_id else "unknown"
        grouped.setdefault(key, []).append(chunk)

    segments: list[dict[str, Any]] = []
    for response_id, response_chunks in grouped.items():
        ordered = sorted(response_chunks, key=lambda item: (item.get("sample_start") or 0, item.get("ts") or 0))
        starts = [int(item.get("sample_start") or 0) for item in ordered]
        ends = [int(item.get("sample_start") or 0) + int(item.get("samples") or 0) for item in ordered]
        timestamps = [float(item.get("ts")) for item in ordered if isinstance(item.get("ts"), (int, float))]
        if not starts or not ends or not timestamps:
            continue
        sample_start = min(starts)
        sample_end = max(ends)
        duration_s = max(0.0, (sample_end - sample_start) / float(sr))
        natural_start_ts = min(timestamps)
        playback_span = playback_spans.get(response_id)
        start_ts = playback_span.get("start_ts", natural_start_ts) if playback_span else natural_start_ts
        natural_end_ts = start_ts + duration_s
        wall_end_ts = playback_span.get("end_ts") if playback_span else None
        wall_duration_s = max(0.0, wall_end_ts - start_ts) if wall_end_ts is not None else None
        segments.append(
            {
                "response_id": response_id,
                "start_ts": round(start_ts, 3),
                "end_ts": round(wall_end_ts if wall_end_ts is not None else natural_end_ts, 3),
                "natural_end_ts": round(natural_end_ts, 3),
                "sample_start": sample_start,
                "sample_end": sample_end,
                "audio_start_s": round(sample_start / float(sr), 6),
                "duration_s": round(duration_s, 3),
                "wall_duration_s": round(wall_duration_s, 3) if wall_duration_s is not None else None,
            }
        )
    return sorted(segments, key=lambda item: (item["start_ts"], item["sample_start"]))


def _playback_spans_by_response(rows: list[TimelineRow]) -> dict[str, dict[str, float]]:
    spans: dict[str, dict[str, float]] = {}
    active_response_id: str | None = None
    active_start: float | None = None
    for row in sorted(rows, key=lambda item: (item.ts, item.lane, item.line)):
        if row.type == "assistant.audio.started":
            response_id = _response_id(row.data)
            if response_id:
                active_response_id = response_id
                active_start = row.ts
            continue
        if row.type == "assistant.audio.done" and active_response_id and active_start is not None:
            spans[active_response_id] = {"start_ts": active_start, "end_ts": row.ts}
            active_response_id = None
            active_start = None
    return spans


def _marker_payload(row: TimelineRow) -> dict[str, Any]:
    return {
        "ts": row.ts,
        "n": row.data.get("n"),
        "clock": row.data.get("clock"),
        "note": row.data.get("note"),
    }


def _normalized_type(value: str) -> str:
    for prefix in ("hf.realtime.", "realtime."):
        if value.startswith(prefix):
            return value.removeprefix(prefix)
    return value


def _event_label(row: TimelineRow, normalized: str) -> str:
    labels = {
        "input_audio_buffer.speech_started": "speech started",
        "input_audio_buffer.speech_stopped": "speech stopped",
        "conversation.item.input_audio_transcription.completed": "transcript",
        "response.created": "response.created signal",
        "response.done": "response done",
        "response.output_audio.done": "response audio done",
        "assistant.audio.started": "playback started",
        "assistant.audio.done": "playback done",
    }
    label = labels.get(normalized, normalized)
    text = _event_text(row)
    if text:
        return f"{label}: {text}"
    response_id = _response_id(row.data)
    if response_id:
        return f"{label}: {response_id}"
    return label


def _event_text(row: TimelineRow) -> str | None:
    for key in ("transcript", "text"):
        value = row.data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _response_id(data: Mapping[str, Any]) -> str | None:
    for key in ("response_id", "id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    metadata = data.get("metadata")
    if isinstance(metadata, Mapping):
        value = metadata.get("response_id")
        if isinstance(value, str) and value:
            return value
    response = data.get("response")
    if isinstance(response, Mapping):
        value = response.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def _timeline_bounds(values: list[Any]) -> tuple[float, float]:
    numeric = [float(value) for value in values if isinstance(value, (int, float)) and value > 0]
    if not numeric:
        return 0.0, 1.0
    start = min(numeric)
    end = max(numeric)
    if end <= start:
        end = start + 1.0
    return round(start, 3), round(end, 3)


def _safe_audio_id(stream: str, audio_files: Mapping[str, Path]) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", stream).strip("._-") or "audio"
    candidate = base
    index = 2
    while candidate in audio_files:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _round_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 3)


def _parse_range(header: str | None, *, size: int) -> tuple[int | None, int | None]:
    if not header or not header.startswith("bytes="):
        return None, None
    value = header.removeprefix("bytes=").split(",", 1)[0].strip()
    if "-" not in value:
        return None, None
    start_raw, end_raw = value.split("-", 1)
    try:
        if start_raw == "":
            suffix = int(end_raw)
            if suffix <= 0:
                return None, None
            return max(0, size - suffix), size - 1
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
    except ValueError:
        return None, None
    if start < 0 or start >= size or end < start:
        return None, None
    return start, min(end, size - 1)


_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reachy Audio Review</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #121417;
      --panel: #1a1e23;
      --panel2: #20262d;
      --text: #eef1f4;
      --muted: #9aa4af;
      --line: #39424d;
      --input: #5cc8ff;
      --output: #f5b84b;
      --event: #79d88f;
      --marker: #e884ff;
      --danger: #ff6b6b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
    }
    button, select, input {
      font: inherit;
    }
    button {
      border: 1px solid var(--line);
      background: var(--panel2);
      color: var(--text);
      border-radius: 6px;
      padding: 7px 10px;
      cursor: pointer;
    }
    button:hover { border-color: var(--muted); }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      background: #15191d;
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .title { min-width: 280px; }
    .title h1 {
      margin: 0 0 2px;
      font-size: 16px;
      line-height: 1.2;
      letter-spacing: 0;
    }
    .title div, .time, .path { color: var(--muted); font-size: 12px; }
    .controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .modeHint {
      color: var(--muted);
      font-size: 12px;
      max-width: 520px;
    }
    .controls label {
      color: var(--muted);
      display: inline-flex;
      align-items: center;
      gap: 5px;
      white-space: nowrap;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      min-height: calc(100vh - 62px);
    }
    .timelinePane {
      overflow: auto;
      border-right: 1px solid var(--line);
      background: var(--bg);
    }
    .timelineContent {
      position: relative;
      min-height: 560px;
      padding: 14px 0 40px;
    }
    .ruler {
      height: 30px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      position: relative;
      margin-left: 120px;
    }
    .tick {
      position: absolute;
      top: 0;
      height: 30px;
      border-left: 1px solid #303943;
      padding-left: 4px;
      font-size: 11px;
    }
    .row {
      display: grid;
      grid-template-columns: 120px minmax(0, 1fr);
      min-height: 84px;
      border-bottom: 1px solid #252c34;
    }
    .rowLabel {
      position: sticky;
      left: 0;
      z-index: 2;
      background: #15191d;
      border-right: 1px solid var(--line);
      padding: 12px;
      color: var(--muted);
      font-size: 12px;
    }
    .rowBody {
      position: relative;
      min-height: 84px;
      background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
    }
    canvas.wave {
      position: absolute;
      inset: 0;
      height: 84px;
    }
    .event, .turn, .marker {
      position: absolute;
      top: 10px;
      min-width: 10px;
      max-width: 280px;
      border-radius: 5px;
      padding: 4px 6px;
      font-size: 11px;
      line-height: 1.2;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      cursor: pointer;
      border: 1px solid transparent;
    }
    .event { background: rgba(121,216,143,0.14); border-color: rgba(121,216,143,0.45); color: #c8f5d1; }
    .turn { background: rgba(92,200,255,0.12); border-color: rgba(92,200,255,0.38); color: #d4f1ff; top: 42px; }
    .marker { background: rgba(232,132,255,0.12); border-color: rgba(232,132,255,0.45); color: #f1d0ff; }
    .selectionBand {
      position: absolute;
      top: 0;
      bottom: 0;
      background: rgba(255, 255, 255, 0.08);
      border-left: 1px solid rgba(255, 255, 255, 0.35);
      border-right: 1px solid rgba(255, 255, 255, 0.35);
      pointer-events: none;
      z-index: 3;
    }
    .chunk {
      position: absolute;
      top: 16px;
      height: 52px;
      border-radius: 4px;
      border: 1px solid transparent;
      cursor: pointer;
      opacity: 0.82;
    }
    .chunk.input { background: rgba(92,200,255,0.32); border-color: rgba(92,200,255,0.62); }
    .chunk.output { background: rgba(245,184,75,0.34); border-color: rgba(245,184,75,0.68); }
    .chunk:hover { opacity: 1; filter: brightness(1.2); }
    .chunk.selected {
      outline: 2px solid #fff;
      outline-offset: 1px;
    }
    .side {
      background: var(--panel);
      overflow: auto;
      padding: 14px;
    }
    .side h2 {
      font-size: 13px;
      margin: 4px 0 8px;
      color: var(--muted);
      font-weight: 600;
      letter-spacing: 0;
    }
    .item {
      border: 1px solid var(--line);
      background: var(--panel2);
      border-radius: 8px;
      padding: 9px;
      margin: 8px 0;
      cursor: pointer;
    }
    .item:hover { border-color: var(--muted); }
    .item strong { display: block; margin-bottom: 4px; }
    .item .meta { color: var(--muted); font-size: 12px; }
    .pill {
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 7px;
      margin-right: 4px;
      color: var(--muted);
      font-size: 11px;
    }
    .empty { color: var(--muted); padding: 12px 0; }
    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .layout { grid-template-columns: 1fr; }
      .side { border-top: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <div class="title">
      <h1 id="runTitle">Audio Review</h1>
      <div id="runMeta"></div>
    </div>
    <div class="controls">
      <button id="playInputBtn" title="Play selected input chunks">Play input</button>
      <button id="playOutputBtn" title="Play selected output chunks">Play output</button>
      <button id="stopBtn" title="Stop playback">Stop</button>
      <button id="clearBtn" title="Clear selection">Clear</button>
      <label>zoom <input id="zoom" type="range" min="5" max="80" step="1" value="12"></label>
      <span class="time" id="timeText"></span>
    </div>
    <div class="modeHint">Observed chunk timeline. Select a wall-time range, then play input or output chunks from that range.</div>
  </header>
  <main class="layout">
    <section class="timelinePane" id="timelinePane">
      <div class="timelineContent" id="timelineContent">
        <div class="selectionBand" id="selectionBand"></div>
        <div class="ruler" id="ruler"></div>
        <div id="rows"></div>
      </div>
    </section>
    <aside class="side">
      <h2>Turns</h2>
      <div id="turnList"></div>
      <h2>Markers</h2>
      <div id="markerList"></div>
      <h2>Files</h2>
      <div id="fileList"></div>
    </aside>
  </main>
  <script>
    const state = {
      data: null,
      pxPerSec: 12,
      selection: null,
      dragStartTs: null,
      playing: [],
      audio: new Map(),
    };

    const $ = (id) => document.getElementById(id);

    fetch('/api/review')
      .then((response) => response.json())
      .then((data) => {
        state.data = data;
        $('runTitle').textContent = data.run_id;
        $('runMeta').textContent = data.run_root;
        buildAudioElements();
        selectRange(data.timeline.start_ts, Math.min(data.timeline.start_ts + 5, data.timeline.end_ts));
        render();
      });

    function buildAudioElements() {
      for (const track of state.data.tracks) {
        const audio = document.createElement('audio');
        audio.src = track.url;
        audio.preload = 'metadata';
        audio.dataset.stream = track.stream;
        document.body.appendChild(audio);
        state.audio.set(track.stream, audio);
      }
    }

    function render() {
      const duration = Math.max(1, state.data.timeline.duration_s);
      const width = Math.max(900, Math.ceil(duration * state.pxPerSec));
      $('timelineContent').style.width = `${width + 120}px`;
      renderRuler(width);
      renderRows(width);
      renderSide();
      renderSelection();
    }

    function renderRuler(width) {
      const ruler = $('ruler');
      ruler.style.width = `${width}px`;
      ruler.innerHTML = '';
      const step = chooseStep();
      const start = state.data.timeline.start_ts;
      const end = state.data.timeline.end_ts;
      for (let t = Math.ceil(start / step) * step; t <= end; t += step) {
        const tick = document.createElement('div');
        tick.className = 'tick';
        tick.style.left = `${xForTs(t)}px`;
        tick.textContent = formatOffset(t);
        ruler.appendChild(tick);
      }
    }

    function chooseStep() {
      if (state.pxPerSec >= 50) return 5;
      if (state.pxPerSec >= 20) return 10;
      return 30;
    }

    function renderRows(width) {
      const rows = $('rows');
      rows.innerHTML = '';
      for (const track of state.data.tracks) {
        const row = makeRow(track.label);
        row.body.appendChild(makeChunkLayer(track, width));
        rows.appendChild(row.root);
      }

      const backend = makeRow('Backend');
      for (const event of state.data.backend_events) {
        backend.body.appendChild(makeTimelineChip(event.ts, event.label, 'event', event.ts, event.ts + 0.1));
      }
      for (const turn of state.data.turns) {
        const start = turn.review_start_ts || turn.speech_start_ts || turn.transcript_ts;
        const end = turn.review_end_ts || turn.audio_done_ts || turn.first_audio_ts || turn.response_created_ts || turn.transcript_ts + 1;
        backend.body.appendChild(makeTimelineChip(start, `#${turn.index} ${turn.transcript}`, 'turn', start, end));
      }
      rows.appendChild(backend.root);

      const markers = makeRow('Markers');
      for (const marker of state.data.markers) {
        markers.body.appendChild(makeTimelineChip(marker.ts, `M${marker.n || ''} ${marker.note || ''}`, 'marker', marker.ts, marker.ts + 0.1));
      }
      rows.appendChild(markers.root);
    }

    function makeRow(label) {
      const root = document.createElement('div');
      root.className = 'row';
      const rowLabel = document.createElement('div');
      rowLabel.className = 'rowLabel';
      rowLabel.textContent = label;
      const body = document.createElement('div');
      body.className = 'rowBody';
      body.style.width = `${Math.max(900, Math.ceil(state.data.timeline.duration_s * state.pxPerSec))}px`;
      body.addEventListener('mousedown', (event) => {
        const ts = tsForClientX(body, event.clientX);
        state.dragStartTs = ts;
        selectRange(ts, ts + 0.5);
      });
      body.addEventListener('mousemove', (event) => {
        if (state.dragStartTs == null) return;
        selectRange(state.dragStartTs, tsForClientX(body, event.clientX));
      });
      body.addEventListener('mouseup', () => { state.dragStartTs = null; });
      body.addEventListener('mouseleave', () => { state.dragStartTs = null; });
      root.appendChild(rowLabel);
      root.appendChild(body);
      return {root, body};
    }

    function makeChunkLayer(track, width) {
      const layer = document.createElement('div');
      layer.style.position = 'absolute';
      layer.style.inset = '0';
      const chunks = chunkIntervals(track);
      for (const interval of chunks) {
        const el = document.createElement('div');
        el.className = `chunk ${track.stream}`;
        el.style.left = `${xForTs(interval.start_ts)}px`;
        el.style.width = `${Math.max(1, (interval.end_ts - interval.start_ts) * state.pxPerSec)}px`;
        el.title = `${track.stream} ${formatOffset(interval.start_ts)}-${formatOffset(interval.end_ts)} samples ${interval.sample_start}:${interval.sample_end}`;
        el.addEventListener('click', (event) => {
          event.stopPropagation();
          selectRange(interval.start_ts, interval.end_ts);
        });
        layer.appendChild(el);
      }
      return layer;
    }

    function chunkIntervals(track) {
      return (track.chunks || []).map((chunk) => {
        const sampleRate = chunk.sample_rate || track.sample_rate || 16000;
        const duration = (chunk.samples || 0) / Math.max(1, sampleRate);
        return {
          stream: track.stream,
          start_ts: chunk.ts,
          end_ts: chunk.ts + duration,
          sample_start: chunk.sample_start || 0,
          sample_end: (chunk.sample_start || 0) + (chunk.samples || 0),
          sample_rate: sampleRate,
          response_id: chunk.response_id || null,
        };
      });
    }

    function makeTimelineChip(ts, label, className, rangeStart, rangeEnd) {
      const chip = document.createElement('div');
      chip.className = className;
      chip.style.left = `${xForTs(ts)}px`;
      chip.title = label;
      chip.textContent = label;
      chip.addEventListener('click', (event) => {
        event.stopPropagation();
        selectRange(rangeStart, rangeEnd || rangeStart + 0.5);
      });
      return chip;
    }

    function renderSide() {
      const turns = $('turnList');
      turns.innerHTML = '';
      for (const turn of state.data.turns) {
        const item = document.createElement('div');
        item.className = 'item';
        const start = turn.review_start_ts || turn.speech_start_ts || turn.transcript_ts;
        const end = turn.review_end_ts || turn.audio_done_ts || turn.first_audio_ts || turn.response_created_ts || turn.transcript_ts + 1;
        const inputCount = chunksInRange('input', start, end).length;
        const outputCount = chunksInRange('output', start, end).length;
        const speech = turn.speech_start_ts ? `speech ${formatOffset(turn.speech_start_ts)} -> transcript ${formatOffset(turn.transcript_ts)}` : `transcript ${formatOffset(turn.transcript_ts)}`;
        item.innerHTML = `<strong>#${turn.index} ${escapeHtml(turn.transcript)}</strong><div class="meta">${speech}</div><div><span class="pill">input ${inputCount}</span><span class="pill">output ${outputCount}</span></div>`;
        item.addEventListener('click', () => selectRange(start, end));
        turns.appendChild(item);
      }
      if (!state.data.turns.length) turns.innerHTML = '<div class="empty">No turns found.</div>';

      const markers = $('markerList');
      markers.innerHTML = '';
      for (const marker of state.data.markers) {
        const item = document.createElement('div');
        item.className = 'item';
        item.innerHTML = `<strong>M${marker.n || ''} ${escapeHtml(marker.note || '')}</strong><div class="meta">${formatOffset(marker.ts)} ${marker.clock || ''}</div>`;
        item.addEventListener('click', () => selectRange(marker.ts - 2, marker.ts + 4));
        markers.appendChild(item);
      }
      if (!state.data.markers.length) markers.innerHTML = '<div class="empty">No markers found.</div>';

      const files = $('fileList');
      files.innerHTML = '';
      for (const track of state.data.tracks) {
        const item = document.createElement('div');
        item.className = 'item';
        item.innerHTML = `<strong>${track.label}</strong><div class="path">${escapeHtml(track.wav_path)}</div>`;
        files.appendChild(item);
      }
    }

    function selectRange(a, b) {
      const start = Math.max(state.data.timeline.start_ts, Math.min(a, b));
      const end = Math.min(state.data.timeline.end_ts, Math.max(a, b));
      state.selection = {start, end: Math.max(start + 0.02, end)};
      renderSelection();
    }

    function renderSelection() {
      if (!state.data || !state.selection) return;
      const band = $('selectionBand');
      band.style.left = `${120 + xForTs(state.selection.start)}px`;
      band.style.width = `${Math.max(2, (state.selection.end - state.selection.start) * state.pxPerSec)}px`;
      const inputCount = chunksInRange('input', state.selection.start, state.selection.end).length;
      const outputCount = chunksInRange('output', state.selection.start, state.selection.end).length;
      $('timeText').textContent = `${formatOffset(state.selection.start)}-${formatOffset(state.selection.end)} input ${inputCount} output ${outputCount}`;
    }

    function playSelection(stream) {
      if (!state.selection) return;
      stopPlayback();
      const track = state.data.tracks.find((item) => item.stream === stream);
      const audio = state.audio.get(stream);
      if (!track || !audio) return;
      const chunks = chunksInRange(stream, state.selection.start, state.selection.end);
      if (!chunks.length) return;
      const ranges = mergeSampleRanges(chunks);
      playRanges(audio, ranges, 0);
    }

    function playRanges(audio, ranges, index) {
      if (index >= ranges.length) return;
      const range = ranges[index];
      audio.playbackRate = 1;
      audio.currentTime = range.start_s;
      const token = {audio, stopAt: range.end_s, next: () => playRanges(audio, ranges, index + 1)};
      state.playing.push(token);
      audio.play().catch(() => {});
      monitorRange(token);
    }

    function monitorRange(token) {
      if (!state.playing.includes(token)) return;
      if (token.audio.currentTime >= token.stopAt || token.audio.ended) {
        token.audio.pause();
        state.playing = state.playing.filter((item) => item !== token);
        token.next();
        return;
      }
      requestAnimationFrame(() => monitorRange(token));
    }

    function stopPlayback() {
      for (const token of state.playing) token.audio.pause();
      for (const audio of state.audio.values()) audio.pause();
      state.playing = [];
    }

    function chunksInRange(stream, start, end) {
      const track = state.data.tracks.find((item) => item.stream === stream);
      if (!track) return [];
      return chunkIntervals(track).filter((chunk) => chunk.end_ts >= start && chunk.start_ts <= end);
    }

    function mergeSampleRanges(chunks) {
      const ranges = chunks.map((chunk) => ({start: chunk.sample_start, end: chunk.sample_end, sampleRate: chunk.sample_rate})).sort((a, b) => a.start - b.start);
      const merged = [];
      for (const range of ranges) {
        const last = merged[merged.length - 1];
        if (last && range.start <= last.end + 320) {
          last.end = Math.max(last.end, range.end);
        } else {
          merged.push({...range});
        }
      }
      return merged.map((range) => ({start_s: range.start / range.sampleRate, end_s: range.end / range.sampleRate}));
    }

    function tsForClientX(body, clientX) {
      const rect = body.getBoundingClientRect();
      return state.data.timeline.start_ts + (clientX - rect.left) / state.pxPerSec;
    }

    function xForTs(ts) {
      return Math.max(0, (ts - state.data.timeline.start_ts) * state.pxPerSec);
    }

    function formatOffset(ts) {
      const s = Math.max(0, ts - state.data.timeline.start_ts);
      const min = Math.floor(s / 60);
      const sec = Math.floor(s % 60);
      const frac = Math.floor((s - Math.floor(s)) * 10);
      return `${min}:${String(sec).padStart(2, '0')}.${frac}`;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (char) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[char]));
    }

    $('playInputBtn').addEventListener('click', () => playSelection('input'));
    $('playOutputBtn').addEventListener('click', () => playSelection('output'));
    $('stopBtn').addEventListener('click', stopPlayback);
    $('clearBtn').addEventListener('click', () => {
      stopPlayback();
      selectRange(state.data.timeline.start_ts, Math.min(state.data.timeline.start_ts + 5, state.data.timeline.end_ts));
    });
    $('zoom').addEventListener('input', (event) => {
      state.pxPerSec = Number(event.target.value);
      render();
    });
  </script>
</body>
</html>
"""


_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reachy Audio Review</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101316;
      --panel: #171c21;
      --panel2: #20262d;
      --line: #34404a;
      --text: #eef2f5;
      --muted: #99a6b2;
      --vad: #f4b942;
      --stt: #67c7ff;
      --llm: #a78bfa;
      --recovered: #f472b6;
      --diag: #2dd4bf;
      --tts: #4ade80;
      --robot: #fb7185;
      --policy: #f97316;
      --marker: #e879f9;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 13px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 20;
      display: grid;
      grid-template-columns: minmax(260px, 1fr) minmax(360px, 720px);
      gap: 16px;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      background: #12171c;
    }
    h1 { margin: 0 0 4px; font-size: 17px; letter-spacing: 0; }
    .sub, .meta, .time { color: var(--muted); font-size: 12px; }
    .controls { display: grid; gap: 8px; }
    audio { width: 100%; height: 34px; }
    .buttons { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    button {
      border: 1px solid var(--line);
      background: var(--panel2);
      color: var(--text);
      border-radius: 6px;
      padding: 6px 10px;
      cursor: pointer;
    }
    button:hover { border-color: var(--muted); }
    label { color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
    input[type="range"] { width: 160px; }
    select {
      min-width: 230px;
      max-width: 340px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel2);
      color: var(--text);
      padding: 6px 8px;
    }
    main { display: grid; grid-template-columns: minmax(0, 1fr) 360px; min-height: calc(100vh - 116px); }
    .timelinePane { overflow: auto; border-right: 1px solid var(--line); }
    .timeline {
      position: relative;
      min-height: 720px;
      padding-bottom: 40px;
    }
    .ruler {
      position: sticky;
      top: 0;
      z-index: 10;
      height: 34px;
      margin-left: 150px;
      border-bottom: 1px solid var(--line);
      background: #12171c;
    }
    .tick {
      position: absolute;
      top: 0;
      height: 34px;
      border-left: 1px solid #2d3741;
      color: var(--muted);
      font-size: 11px;
      padding-left: 4px;
      line-height: 34px;
      white-space: nowrap;
    }
    .playhead {
      position: absolute;
      top: 0;
      bottom: 0;
      width: 2px;
      background: #ffffff;
      box-shadow: 0 0 0 1px rgba(0,0,0,0.3);
      z-index: 15;
      pointer-events: none;
    }
    .row {
      display: grid;
      grid-template-columns: 150px minmax(0, 1fr);
      min-height: 64px;
      border-bottom: 1px solid #202932;
    }
    .rowLabel {
      position: sticky;
      left: 0;
      z-index: 5;
      padding: 10px 12px;
      color: var(--muted);
      background: #12171c;
      border-right: 1px solid var(--line);
      font-weight: 600;
    }
    .rowBody {
      position: relative;
      min-height: 64px;
      cursor: pointer;
      background: linear-gradient(180deg, rgba(255,255,255,0.018), rgba(255,255,255,0));
    }
    .event {
      position: absolute;
      top: 22px;
      height: 18px;
      min-width: 8px;
      max-width: none;
      border-radius: 5px;
      border: 1px solid rgba(255,255,255,0.18);
      padding: 0;
      overflow: visible;
      line-height: 0;
      cursor: pointer;
      color: #f8fafc;
      background: rgba(148,163,184,0.20);
    }
    .event.point {
      top: 27px;
      width: 10px;
      min-width: 10px;
      height: 10px;
      padding: 0;
      overflow: visible;
      border-radius: 999px;
      background: currentColor;
      border-color: currentColor;
      transform: translateX(-5px);
    }
    .event span {
      display: none;
    }
    .event.vad { background: color-mix(in srgb, var(--vad) 35%, transparent); border-color: color-mix(in srgb, var(--vad) 70%, transparent); }
    .event.stt_partial, .event.stt_final { background: color-mix(in srgb, var(--stt) 30%, transparent); border-color: color-mix(in srgb, var(--stt) 70%, transparent); }
    .event.llm_input, .event.llm, .event.llm_response { background: color-mix(in srgb, var(--llm) 32%, transparent); border-color: color-mix(in srgb, var(--llm) 70%, transparent); }
    .event.stt_recovered { background: color-mix(in srgb, var(--recovered) 34%, transparent); border-color: color-mix(in srgb, var(--recovered) 78%, transparent); }
    .event.transcript_availability { background: color-mix(in srgb, var(--diag) 30%, transparent); border-color: color-mix(in srgb, var(--diag) 70%, transparent); }
    .event.tts { background: color-mix(in srgb, var(--tts) 28%, transparent); border-color: color-mix(in srgb, var(--tts) 65%, transparent); }
    .event.robot_audio { background: color-mix(in srgb, var(--robot) 32%, transparent); border-color: color-mix(in srgb, var(--robot) 70%, transparent); }
    .event.policy { background: color-mix(in srgb, var(--policy) 30%, transparent); border-color: color-mix(in srgb, var(--policy) 68%, transparent); }
    .event.markers { background: color-mix(in srgb, var(--marker) 32%, transparent); border-color: color-mix(in srgb, var(--marker) 70%, transparent); }
    .event.value-llm-response-created { color: #94a3b8; background: color-mix(in srgb, #94a3b8 34%, transparent); border-color: color-mix(in srgb, #94a3b8 76%, transparent); }
    .event.value-llm-first-audio { color: #38bdf8; background: color-mix(in srgb, #38bdf8 34%, transparent); border-color: color-mix(in srgb, #38bdf8 76%, transparent); }
    .event.value-llm-response-done { color: #a78bfa; background: color-mix(in srgb, #a78bfa 36%, transparent); border-color: color-mix(in srgb, #a78bfa 78%, transparent); }
    .event.value-llm-audio-done { color: #fb7185; background: color-mix(in srgb, #fb7185 34%, transparent); border-color: color-mix(in srgb, #fb7185 76%, transparent); }
    .event.value-transcript-logged { color: #22c55e; background: color-mix(in srgb, #22c55e 34%, transparent); border-color: color-mix(in srgb, #22c55e 76%, transparent); }
    .event.value-transcript-missing { color: #f59e0b; background: color-mix(in srgb, #f59e0b 42%, transparent); border-color: color-mix(in srgb, #f59e0b 86%, transparent); }
    .event.value-tts-generation { color: #4ade80; background: color-mix(in srgb, #4ade80 34%, transparent); border-color: color-mix(in srgb, #4ade80 76%, transparent); }
    .event.value-tts-audio-done { color: #facc15; background: color-mix(in srgb, #facc15 36%, transparent); border-color: color-mix(in srgb, #facc15 82%, transparent); }
    aside {
      background: var(--panel);
      padding: 14px;
      overflow: auto;
    }
    aside h2 { margin: 0 0 8px; font-size: 13px; color: var(--muted); }
    .card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel2);
      padding: 10px;
      margin-bottom: 12px;
    }
    .card strong { display: block; margin-bottom: 5px; }
    .kv { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 4px 8px; }
    .kv div:nth-child(odd) { color: var(--muted); }
    pre {
      margin: 8px 0 0;
      white-space: pre-wrap;
      word-break: break-word;
      color: #dbeafe;
      font-size: 12px;
    }
    @media (max-width: 900px) {
      header, main { grid-template-columns: 1fr; }
      aside { border-top: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1 id="title">Audio Review</h1>
      <div class="sub" id="subtitle"></div>
      <div class="meta" id="alignment"></div>
    </div>
    <div class="controls">
      <audio id="audio" controls preload="metadata"></audio>
      <div class="buttons">
        <button id="play">Play</button>
        <button id="pause">Pause</button>
        <button id="start">Start</button>
        <label>zoom <input id="zoom" type="range" min="4" max="60" step="1" value="10"></label>
        <label>response <select id="responseSelect"></select></label>
        <button id="playResponse" disabled>Play response</button>
        <span class="time" id="time">0:00.0 / 0:00.0</span>
      </div>
    </div>
  </header>
  <main>
    <section class="timelinePane" id="pane">
      <div class="timeline" id="timeline">
        <div class="playhead" id="playhead"></div>
        <div class="ruler" id="ruler"></div>
        <div id="rows"></div>
      </div>
    </section>
    <aside>
      <h2>Selected Event</h2>
      <div class="card" id="selected">Click an event or lane to inspect it.</div>
      <h2>Files</h2>
      <div class="card" id="files"></div>
      <h2>Lane Counts</h2>
      <div class="card" id="counts"></div>
    </aside>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const state = { data: null, pxPerSec: 10, responseAudio: new Map() };

    init().catch((err) => {
      $('selected').textContent = err.stack || String(err);
    });

    async function init() {
      const res = await fetch('/api/review');
      if (!res.ok) throw new Error(`Failed to load review: ${res.status}`);
      state.data = await res.json();
      $('title').textContent = `Audio Review: ${state.data.run_id}`;
      const aligned = state.data.aligned_audio;
      if (!aligned) throw new Error('No aligned audio in review payload. Start with --serve so the exporter runs first.');
      $('audio').src = aligned.url;
      $('subtitle').textContent = `${formatTime(aligned.duration_s)} stereo WAV | left=input, right=robot output`;
      const mode = aligned.alignment?.input_anchor_mode || 'unknown';
      const anchor = aligned.alignment?.input_wav_start_ts;
      $('alignment').textContent = `alignment: ${mode}${anchor ? ` | input_wav_start_ts=${anchor}` : ''}`;
      render();
      updatePlayhead();
    }

    function render() {
      const data = state.data;
      const duration = data.timeline.duration_s || 1;
      const width = Math.max(900, Math.ceil(duration * state.pxPerSec) + 80);
      $('timeline').style.width = `${width + 150}px`;
      renderRuler(width, duration);
      const rows = $('rows');
      rows.innerHTML = '';
      for (const lane of data.lanes || []) {
        const row = document.createElement('div');
        row.className = 'row';
        const label = document.createElement('div');
        label.className = 'rowLabel';
        label.textContent = lane.label;
        const body = document.createElement('div');
        body.className = 'rowBody';
        body.style.width = `${width}px`;
        body.addEventListener('click', (event) => {
          if (event.target !== body) return;
          seekFromClientX(body, event.clientX);
        });
        for (const item of lane.events || []) {
          body.appendChild(renderEvent(lane.id, item));
        }
        row.appendChild(label);
        row.appendChild(body);
        rows.appendChild(row);
      }
      renderCounts();
      renderFiles();
      renderResponseControl();
      updatePlayhead();
    }

    function renderRuler(width, duration) {
      const ruler = $('ruler');
      ruler.innerHTML = '';
      ruler.style.width = `${width}px`;
      const targetPx = 120;
      const rawStep = Math.max(1, targetPx / state.pxPerSec);
      const step = niceStep(rawStep);
      for (let t = 0; t <= duration + 0.001; t += step) {
        const tick = document.createElement('div');
        tick.className = 'tick';
        tick.style.left = `${t * state.pxPerSec}px`;
        tick.textContent = formatTime(t);
        ruler.appendChild(tick);
      }
    }

    function renderEvent(laneId, item) {
      const el = document.createElement('div');
      el.className = ['event', laneId, item.kind === 'span' ? 'span' : 'point', valueClass(laneId, item.label)].filter(Boolean).join(' ');
      const left = item.t * state.pxPerSec;
      const end = item.end_t ?? item.t;
      const width = item.kind === 'span' ? Math.max(8, (end - item.t) * state.pxPerSec) : 10;
      el.style.left = `${left}px`;
      el.style.width = `${width}px`;
      el.title = eventTitle(item);
      el.setAttribute('aria-label', item.label);
      const span = document.createElement('span');
      span.textContent = item.label;
      el.appendChild(span);
      el.addEventListener('click', (event) => {
        event.stopPropagation();
        $('audio').currentTime = Math.max(0, item.t);
        showSelected(laneId, item);
      });
      return el;
    }

    function valueClass(laneId, label) {
      const key = `${laneId}:${label}`;
      return {
        'llm:response.created signal': 'value-llm-response-created',
        'llm:first audio': 'value-llm-first-audio',
        'llm:response done': 'value-llm-response-done',
        'llm:audio done': 'value-llm-audio-done',
        'transcript_availability:backend transcript logged': 'value-transcript-logged',
        'transcript_availability:no backend transcript event': 'value-transcript-missing',
        'tts:audio generation': 'value-tts-generation',
        'tts:audio done': 'value-tts-audio-done',
      }[key] || '';
    }

    function showSelected(laneId, item) {
      const html = [
        `<strong>${escapeHtml(item.label)}</strong>`,
        '<div class="kv">',
        `<div>lane</div><div>${escapeHtml(laneId)}</div>`,
        `<div>time</div><div>${formatTime(item.t)} (${item.t.toFixed(3)}s)</div>`,
        item.end_t !== undefined ? `<div>end</div><div>${formatTime(item.end_t)} (${item.end_t.toFixed(3)}s)</div>` : '',
        item.type ? `<div>event</div><div>${escapeHtml(item.type)}</div>` : '',
        item.response_id ? `<div>response</div><div>${escapeHtml(item.response_id)}</div>` : '',
        '</div>',
        item.detail ? `<pre>${escapeHtml(item.detail)}</pre>` : ''
      ].join('');
      $('selected').innerHTML = html;
    }

    function renderResponseControl() {
      const select = $('responseSelect');
      const items = state.data.response_audio || [];
      const previous = select.value;
      select.innerHTML = '';
      $('playResponse').disabled = !items.length;
      if (!items.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'none';
        select.appendChild(option);
        return;
      }
      for (const item of items) {
        const start = item.playback_start_ts ?? item.start_ts;
        const option = document.createElement('option');
        option.value = item.id || item.stream || item.response_id;
        const when = Number.isFinite(Number(start)) ? `${formatTime(offsetForTs(start))} ` : '';
        const name = item.response_id || item.stream || item.id;
        const duration = item.duration_s != null ? ` (${Number(item.duration_s).toFixed(2)}s)` : '';
        option.textContent = `${when}${name}${duration}`;
        select.appendChild(option);
      }
      select.value = items.some((item) => (item.id || item.stream || item.response_id) === previous) ? previous : (items[0].id || items[0].stream || items[0].response_id);
    }

    function selectedResponseAudio() {
      const selected = $('responseSelect').value;
      return (state.data.response_audio || []).find((item) => (item.id || item.stream || item.response_id) === selected) || null;
    }

    function seekResponseAudio(item) {
      const ts = item.playback_start_ts ?? item.start_ts;
      if (!Number.isFinite(Number(ts))) return;
      $('audio').currentTime = Math.max(0, offsetForTs(ts));
      updatePlayhead();
      showResponseSelected(item);
    }

    function playResponseAudio(item) {
      pauseResponseAudio();
      $('audio').pause();
      const ts = item.playback_start_ts ?? item.start_ts;
      if (Number.isFinite(Number(ts))) $('audio').currentTime = Math.max(0, offsetForTs(ts));
      const audio = responseAudioFor(item);
      audio.currentTime = 0;
      audio.play().catch(() => {});
      showResponseSelected(item);
    }

    function pauseResponseAudio() {
      for (const audio of state.responseAudio.values()) audio.pause();
    }

    function responseAudioFor(item) {
      const id = item.id || item.stream || item.response_id;
      let audio = state.responseAudio.get(id);
      if (!audio) {
        audio = new Audio(item.url);
        audio.preload = 'metadata';
        state.responseAudio.set(id, audio);
      }
      return audio;
    }

    function showResponseSelected(item) {
      const start = item.playback_start_ts ?? item.start_ts;
      const end = item.playback_end_ts ?? item.end_ts;
      const html = [
        `<strong>${escapeHtml(item.response_id || item.stream || item.id)}</strong>`,
        '<div class="kv">',
        item.playback_start_ts != null ? `<div>playback</div><div>${formatTime(offsetForTs(start))}${end != null ? `-${formatTime(offsetForTs(end))}` : ''}</div>` : '',
        item.duration_s != null ? `<div>wav</div><div>${Number(item.duration_s).toFixed(3)}s</div>` : '',
        item.url ? `<div>url</div><div>${escapeHtml(item.url)}</div>` : '',
        item.wav_path ? `<div>path</div><div>${escapeHtml(item.wav_path)}</div>` : '',
        '</div>'
      ].join('');
      $('selected').innerHTML = html;
    }

    function renderFiles() {
      const aligned = state.data.aligned_audio;
      $('files').innerHTML = [
        '<div class="kv">',
        `<div>wav</div><div>${escapeHtml(aligned.wav_path)}</div>`,
        `<div>metadata</div><div>${escapeHtml(aligned.metadata_path)}</div>`,
        `<div>labels</div><div>${escapeHtml(aligned.labels_path)}</div>`,
        '</div>'
      ].join('');
    }

    function renderCounts() {
      const lines = (state.data.lanes || []).map((lane) => {
        return `<div>${escapeHtml(lane.label)}</div><div>${(lane.events || []).length}</div>`;
      }).join('');
      $('counts').innerHTML = `<div class="kv">${lines}</div>`;
    }

    function seekFromClientX(body, clientX) {
      const rect = body.getBoundingClientRect();
      const t = Math.max(0, Math.min(state.data.timeline.duration_s, (clientX - rect.left) / state.pxPerSec));
      $('audio').currentTime = t;
      updatePlayhead();
    }

    function updatePlayhead() {
      const audio = $('audio');
      const t = audio.currentTime || 0;
      const x = 150 + t * state.pxPerSec;
      $('playhead').style.left = `${x}px`;
      const duration = state.data?.timeline?.duration_s || audio.duration || 0;
      $('time').textContent = `${formatTime(t)} / ${formatTime(duration)}`;
      if (!audio.paused && !audio.ended) requestAnimationFrame(updatePlayhead);
    }

    function eventTitle(item) {
      const parts = [item.label, `${formatTime(item.t)} (${item.t.toFixed(3)}s)`];
      if (item.detail) parts.push(item.detail);
      if (item.type) parts.push(item.type);
      return parts.join('\n');
    }

    function niceStep(raw) {
      const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300];
      return steps.find((step) => step >= raw) || 600;
    }

    function formatTime(value) {
      const seconds = Math.max(0, Number(value) || 0);
      const min = Math.floor(seconds / 60);
      const sec = Math.floor(seconds % 60);
      const frac = Math.floor((seconds - Math.floor(seconds)) * 10);
      return `${min}:${String(sec).padStart(2, '0')}.${frac}`;
    }

    function offsetForTs(ts) {
      return Math.max(0, Number(ts) - Number(state.data.timeline.start_ts || 0));
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (char) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[char]));
    }

    $('play').addEventListener('click', () => {
      pauseResponseAudio();
      $('audio').play();
    });
    $('pause').addEventListener('click', () => {
      $('audio').pause();
      pauseResponseAudio();
    });
    $('start').addEventListener('click', () => {
      pauseResponseAudio();
      $('audio').currentTime = 0;
      updatePlayhead();
    });
    $('audio').addEventListener('timeupdate', updatePlayhead);
    $('audio').addEventListener('play', updatePlayhead);
    $('zoom').addEventListener('input', (event) => {
      state.pxPerSec = Number(event.target.value);
      render();
    });
    $('responseSelect').addEventListener('change', () => {
      const item = selectedResponseAudio();
      if (item) seekResponseAudio(item);
    });
    $('playResponse').addEventListener('click', () => {
      const item = selectedResponseAudio();
      if (item) playResponseAudio(item);
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
