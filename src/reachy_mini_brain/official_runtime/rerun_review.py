"""Offline Rerun review for official-runtime artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import click


LANE_SUBDIRS = {
    "events": "events",
    "realtime": "realtime",
    "policies": "policies",
    "capture": "capture",
    "audio": "audio",
    "video": "video",
}

TRANSCRIPT_KINDS = {
    "conversation.item.input_audio_transcription.completed",
    "gemini.user_transcription_completed",
    "livekit.room.transcription",
    "backend.transcript.final",
}

SESSION_MILESTONES = {
    "robot_control_ready",
    "robot_sdk_connected",
    "robot_audio_warmup_start",
    "robot_audio_warmup_ok",
    "robot_video_warmup_start",
    "robot_video_warmup_ok",
    "software_pipeline_initialized",
    "first_mic_frame_captured",
    "first_mic_frame_forwarded",
    "audio_gate_opened",
    "audio_gate_closed",
}


class RerunReviewError(RuntimeError):
    """Raised when a run cannot be reviewed."""


class RerunUnavailableError(RerunReviewError):
    """Raised when Rerun rendering is requested but rerun-sdk is unavailable."""


@dataclass(frozen=True)
class TimelineRow:
    lane: str
    path: Path
    line: int
    ts: float
    type: str
    source: str | None
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "path": str(self.path),
            "line": self.line,
            "ts": self.ts,
            "type": self.type,
            "source": self.source,
            "data": self.data,
        }


@dataclass(frozen=True)
class AudioChunk:
    stream: str
    wav_path: Path
    metadata_path: Path
    ts: float
    sample_start: int
    samples: int
    sample_rate: int | None
    rms: float | None
    data: dict[str, Any]

    @property
    def sample_end(self) -> int:
        return self.sample_start + self.samples


@dataclass(frozen=True)
class AudioHint:
    stream: str
    wav_path: Path
    metadata_path: Path
    start_ts: float | None
    end_ts: float | None
    sample_start: int
    sample_end: int
    sample_rate: int | None
    response_id: str | None = None

    @property
    def duration_s(self) -> float | None:
        if not self.sample_rate:
            return None
        return max(0.0, (self.sample_end - self.sample_start) / float(self.sample_rate))

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "wav_path": str(self.wav_path),
            "metadata_path": str(self.metadata_path),
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "sample_start": self.sample_start,
            "sample_end": self.sample_end,
            "sample_rate": self.sample_rate,
            "duration_s": _round_or_none(self.duration_s),
            "response_id": self.response_id,
        }


@dataclass(frozen=True)
class ConversationTurn:
    index: int
    transcript_ts: float
    transcript: str
    response_id: str | None
    thinking_ts: float | None
    response_created_ts: float | None
    first_audio_ts: float | None
    audio_done_ts: float | None
    latency_s: dict[str, float | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "transcript_ts": self.transcript_ts,
            "transcript": self.transcript,
            "response_id": self.response_id,
            "thinking_ts": self.thinking_ts,
            "response_created_ts": self.response_created_ts,
            "first_audio_ts": self.first_audio_ts,
            "audio_done_ts": self.audio_done_ts,
            "latency_s": self.latency_s,
        }


@dataclass(frozen=True)
class TimelineSpan:
    entity: str
    label: str
    start_ts: float
    end_ts: float
    start_row: TimelineRow
    end_row: TimelineRow

    @property
    def duration_s(self) -> float:
        return round(max(0.0, self.end_ts - self.start_ts), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "label": self.label,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_s": self.duration_s,
            "start_type": self.start_row.type,
            "end_type": self.end_row.type,
        }


@dataclass(frozen=True)
class TimelineMarker:
    entity: str
    label: str
    ts: float
    row: TimelineRow

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "label": self.label,
            "ts": self.ts,
            "type": self.row.type,
            "data": self.row.data,
        }


@dataclass(frozen=True)
class TimelineModel:
    spans: list[TimelineSpan]
    markers: list[TimelineMarker]
    allowlisted_rows: list[TimelineRow]

    def to_dict(self) -> dict[str, Any]:
        return {
            "spans": [span.to_dict() for span in self.spans],
            "markers": [marker.to_dict() for marker in self.markers],
            "allowlisted_rows": len(self.allowlisted_rows),
        }


@dataclass(frozen=True)
class RunReview:
    run_id: str
    run_root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    timeline: list[TimelineRow]
    turns: list[ConversationTurn]
    suppressions: list[TimelineRow]
    audio_chunks: list[AudioChunk]
    audio_hints: list[AudioHint]
    model: TimelineModel

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_root": str(self.run_root),
            "manifest_path": str(self.manifest_path),
            "timeline_rows": len(self.timeline),
            "turns": [turn.to_dict() for turn in self.turns],
            "suppressions": [row.to_dict() for row in self.suppressions],
            "audio_hints": [hint.to_dict() for hint in self.audio_hints],
            "model": self.model.to_dict(),
        }


def load_run_review(path: str | Path, *, run_id: str | None = None) -> RunReview:
    """Load a recorded official-runtime run without mutating artifacts."""

    manifest_path = _find_manifest(Path(path).expanduser(), run_id=run_id)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resolved_run_id = str(manifest.get("run_id") or run_id or _run_id_from_manifest(manifest_path))
    run_root = manifest_path.parent.parent

    timeline: list[TimelineRow] = []
    for lane in ("events", "realtime", "policies"):
        timeline.extend(_load_lane_rows(manifest, lane=lane, run_root=run_root))
    timeline.extend(_load_capture_detection_rows(manifest, run_root=run_root))
    timeline.extend(_load_markers(run_root, resolved_run_id))
    timeline.sort(key=lambda row: (row.ts, row.lane, row.line))
    model_rows = _dedupe_timeline_rows(timeline)

    audio_chunks, audio_hints = _load_audio(manifest, run_root=run_root)
    turns = _derive_turns(model_rows)
    suppressions = [row for row in model_rows if _is_suppression(row)]
    model = _derive_timeline_model(model_rows)
    return RunReview(
        run_id=resolved_run_id,
        run_root=run_root,
        manifest_path=manifest_path,
        manifest=manifest,
        timeline=timeline,
        turns=turns,
        suppressions=suppressions,
        audio_chunks=audio_chunks,
        audio_hints=audio_hints,
        model=model,
    )


def format_text_review(review: RunReview) -> str:
    """Return a compact text handoff for CLI use and audio listening."""

    lines = [
        f"run_id: {review.run_id}",
        f"run_root: {review.run_root}",
        f"timeline_rows: {len(review.timeline)}",
        f"model_spans: {len(review.model.spans)}",
        f"model_markers: {len(review.model.markers)}",
        f"turns: {len(review.turns)}",
        f"suppressions: {len(review.suppressions)}",
        f"audio_hints: {len(review.audio_hints)}",
    ]
    if review.model.spans:
        lines.append("")
        lines.append("timeline spans:")
        for span in review.model.spans:
            lines.append(
                f"- {span.entity} {span.start_ts:.3f}->{span.end_ts:.3f} "
                f"duration={span.duration_s:.3f}s label={span.label}"
            )
    if review.model.markers:
        lines.append("")
        lines.append("timeline markers:")
        for marker in review.model.markers:
            lines.append(f"- {marker.entity} {marker.ts:.3f}: {marker.label}")
    if review.turns:
        lines.append("")
        lines.append("turns:")
        for turn in review.turns:
            response = f" response={turn.response_id}" if turn.response_id else ""
            lines.append(f"- #{turn.index} {turn.transcript_ts:.3f}{response}: {turn.transcript}")
            for key, value in turn.latency_s.items():
                if value is not None:
                    lines.append(f"  {key}: {value:.3f}s")
    markers = [row for row in review.timeline if row.lane == "markers"]
    if markers:
        lines.append("")
        lines.append("markers:")
        for row in markers:
            note = row.data.get("note")
            n = row.data.get("n")
            prefix = f"M{n}" if n is not None else "marker"
            suffix = f": {note}" if note else ""
            lines.append(f"- {prefix} {row.ts:.3f}{suffix}")
    if review.suppressions:
        lines.append("")
        lines.append("suppression / missed-cue rows:")
        for row in review.suppressions:
            reason = row.data.get("reason")
            suffix = f" reason={reason}" if reason else ""
            lines.append(f"- {row.ts:.3f} [{row.lane}] {row.type}{suffix}")
    if review.audio_hints:
        lines.append("")
        lines.append("audio listen hints:")
        for hint in review.audio_hints:
            response = f" response={hint.response_id}" if hint.response_id else ""
            duration = hint.duration_s
            duration_text = f" duration={duration:.3f}s" if duration is not None else ""
            lines.append(
                f"- {hint.stream}{response}: {hint.wav_path} "
                f"samples={hint.sample_start}:{hint.sample_end}{duration_text}"
            )
    return "\n".join(lines)


def render_review_to_rerun(review: RunReview, *, save_path: str | Path | None = None, spawn: bool = False) -> None:
    """Render a run review to Rerun.

    Importing rerun happens lazily so parser tests and normal ops tooling do not
    depend on ``rerun-sdk``.
    """

    try:
        import rerun as rr  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RerunUnavailableError("rerun-sdk is not installed; install the optional diagnosis extra") from exc

    recording_id = f"reachy-mini-{review.run_id}"
    _rr_init(rr, recording_id=recording_id, spawn=spawn)
    if save_path is not None:
        _rr_save(rr, Path(save_path))

    for span in review.model.spans:
        _rr_set_time(rr, span.start_ts)
        _rr_log_scalar(rr, span.entity, 1.0)
        _rr_log_text(rr, span.entity, _summarize_span_boundary(span, boundary="START", state=1.0))
        _rr_set_time(rr, span.end_ts)
        _rr_log_scalar(rr, span.entity, 0.0)
        _rr_log_text(rr, span.entity, _summarize_span_boundary(span, boundary="END", state=0.0))

    for marker in review.model.markers:
        _rr_set_time(rr, marker.ts)
        _rr_log_text(rr, marker.entity, marker.label)

    for chunk in review.audio_chunks:
        if chunk.rms is None:
            continue
        _rr_set_time(rr, chunk.ts)
        _rr_log_scalar(rr, f"audio/{chunk.stream}/rms", chunk.rms)

    for hint in review.audio_hints:
        if hint.start_ts is None:
            continue
        _rr_set_time(rr, hint.start_ts)
        _rr_log_text(rr, f"audio/{hint.stream}/listen_hint", _summarize_audio_hint(hint))

    _render_video_to_rerun(rr, review)


@click.command()
@click.argument("run_path", type=click.Path(path_type=Path))
@click.option("--run-id", help="Run id when RUN_PATH contains multiple manifests.")
@click.option("--save-rrd", type=click.Path(path_type=Path), help="Write a portable .rrd file.")
@click.option("--spawn", is_flag=True, help="Open a Rerun viewer instead of only printing the text handoff.")
@click.option("--json-output", is_flag=True, help="Print machine-readable review JSON.")
def cli(run_path: Path, run_id: str | None, save_rrd: Path | None, spawn: bool, json_output: bool) -> None:
    """Review one official-runtime run with Rerun-compatible artifacts."""

    review = load_run_review(run_path, run_id=run_id)
    if save_rrd is not None or spawn:
        render_review_to_rerun(review, save_path=save_rrd, spawn=spawn)
    if json_output:
        click.echo(json.dumps(review.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_text_review(review))


def main() -> None:
    cli()


def _find_manifest(path: Path, *, run_id: str | None) -> Path:
    if path.is_file():
        return path
    if run_id:
        candidate = path / "runs" / f"run-{run_id}.json"
        if candidate.exists():
            return candidate
        raise RerunReviewError(f"manifest not found for run_id={run_id}: {candidate}")
    candidates = sorted((path / "runs").glob("run-*.json"))
    if not candidates:
        raise RerunReviewError(f"no run manifest found under {path / 'runs'}")
    if len(candidates) > 1:
        raise RerunReviewError("multiple run manifests found; pass --run-id")
    return candidates[0]


def _run_id_from_manifest(path: Path) -> str:
    name = path.stem
    return name.removeprefix("run-")


def _load_lane_rows(manifest: Mapping[str, Any], *, lane: str, run_root: Path) -> list[TimelineRow]:
    entries = _artifact_entries(manifest, lane)
    rows: list[TimelineRow] = []
    for entry in entries:
        raw_path = entry.get("path")
        if not raw_path:
            continue
        path = _resolve_path(raw_path, run_root=run_root, subdir=LANE_SUBDIRS.get(lane))
        rows.extend(_read_jsonl(path, lane=lane))
    return rows


def _load_markers(run_root: Path, run_id: str) -> list[TimelineRow]:
    path = run_root.parent / f"markers-{run_id}.jsonl"
    if not path.exists():
        return []
    rows: list[TimelineRow] = []
    for line_no, payload in _iter_jsonl(path):
        ts = _float_or_none(payload.get("ts"))
        if ts is None:
            continue
        data = dict(payload)
        rows.append(
            TimelineRow(
                lane="markers",
                path=path,
                line=line_no,
                ts=ts,
                type="marker",
                source="human",
                data=data,
            )
        )
    return rows


def _load_capture_detection_rows(manifest: Mapping[str, Any], *, run_root: Path) -> list[TimelineRow]:
    rows: list[TimelineRow] = []
    for entry in _artifact_entries(manifest, "capture"):
        raw_path = entry.get("path")
        if not raw_path:
            continue
        path = _resolve_path(raw_path, run_root=run_root, subdir=LANE_SUBDIRS.get("capture"))
        for line_no, payload in _iter_jsonl(path):
            ts = _float_or_none(payload.get("ts"))
            events = payload.get("events")
            if ts is None or not isinstance(events, list):
                continue
            for event_index, event in enumerate(events):
                if not isinstance(event, Mapping):
                    continue
                kind = event.get("kind")
                if not isinstance(kind, str) or not kind:
                    continue
                data = {
                    "run_id": payload.get("run_id"),
                    "type": f"vision.{kind}",
                    "source": "official_runtime.capture",
                    "frame_ts": ts,
                    "frame_line": line_no,
                    "frame_event_index": event_index,
                    "people": payload.get("people"),
                    "tracks": payload.get("tracks"),
                    "event": dict(event),
                }
                data.update(dict(event))
                rows.append(
                    TimelineRow(
                        lane="capture",
                        path=path,
                        line=line_no,
                        ts=ts,
                        type=f"vision.{kind}",
                        source="official_runtime.capture",
                        data=data,
                    )
                )
    return rows


def _load_capture_frame_timestamps(manifest: Mapping[str, Any], *, run_root: Path) -> list[float]:
    timestamps: list[float] = []
    for entry in _artifact_entries(manifest, "capture"):
        raw_path = entry.get("path")
        if not raw_path:
            continue
        path = _resolve_path(raw_path, run_root=run_root, subdir=LANE_SUBDIRS.get("capture"))
        for _, payload in _iter_jsonl(path):
            if payload.get("type") != "vision_frame":
                continue
            ts = _float_or_none(payload.get("ts"))
            if ts is not None:
                timestamps.append(ts)
    return timestamps


def _read_jsonl(path: Path, *, lane: str) -> list[TimelineRow]:
    if not path.exists():
        return []
    rows: list[TimelineRow] = []
    for line_no, payload in _iter_jsonl(path):
        ts = _float_or_none(payload.get("ts"))
        if ts is None:
            continue
        row_type = str(payload.get("type") or payload.get("kind") or "unknown")
        source = payload.get("source")
        rows.append(
            TimelineRow(
                lane=lane,
                path=path,
                line=line_no,
                ts=ts,
                type=row_type,
                source=str(source) if source is not None else None,
                data=dict(payload),
            )
        )
    return rows


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            yield line_no, payload


def _load_audio(manifest: Mapping[str, Any], *, run_root: Path) -> tuple[list[AudioChunk], list[AudioHint]]:
    chunks: list[AudioChunk] = []
    hints: list[AudioHint] = []
    for entry in _artifact_entries(manifest, "audio"):
        stream = str(entry.get("stream") or "")
        if not stream:
            continue
        wav_raw = entry.get("path")
        meta_raw = entry.get("metadata")
        if not wav_raw or not meta_raw:
            continue
        wav_path = _resolve_path(wav_raw, run_root=run_root, subdir="audio")
        meta_path = _resolve_path(meta_raw, run_root=run_root, subdir="audio")
        sample_rate = _int_or_none(entry.get("sample_rate"))
        stream_chunks = _load_audio_chunks(
            stream=stream,
            wav_path=wav_path,
            metadata_path=meta_path,
            sample_rate=sample_rate,
        )
        chunks.extend(stream_chunks)
        hint = _audio_hint_from_chunks(stream=stream, wav_path=wav_path, metadata_path=meta_path, chunks=stream_chunks)
        if hint is not None:
            hints.append(hint)
    return chunks, hints


def _load_audio_chunks(
    *,
    stream: str,
    wav_path: Path,
    metadata_path: Path,
    sample_rate: int | None,
) -> list[AudioChunk]:
    if not metadata_path.exists():
        return []
    chunks: list[AudioChunk] = []
    for _, payload in _iter_jsonl(metadata_path):
        if payload.get("type") != "chunk":
            continue
        ts = _float_or_none(payload.get("ts"))
        sample_start = _int_or_none(payload.get("sample_start"))
        samples = _int_or_none(payload.get("samples"))
        if ts is None or sample_start is None or samples is None:
            continue
        chunk_sample_rate = _int_or_none(payload.get("sample_rate")) or sample_rate
        chunks.append(
            AudioChunk(
                stream=stream,
                wav_path=wav_path,
                metadata_path=metadata_path,
                ts=ts,
                sample_start=sample_start,
                samples=samples,
                sample_rate=chunk_sample_rate,
                rms=_float_or_none(payload.get("rms")),
                data=dict(payload),
            )
        )
    return chunks


def _audio_hint_from_chunks(
    *,
    stream: str,
    wav_path: Path,
    metadata_path: Path,
    chunks: list[AudioChunk],
) -> AudioHint | None:
    if not chunks:
        return None
    sample_rate = next((chunk.sample_rate for chunk in chunks if chunk.sample_rate), None)
    start_ts = min(chunk.ts for chunk in chunks)
    last_chunk = max(chunks, key=lambda chunk: (chunk.ts, chunk.sample_end))
    end_ts = last_chunk.ts
    if last_chunk.sample_rate:
        end_ts += last_chunk.samples / float(last_chunk.sample_rate)
    sample_start = min(chunk.sample_start for chunk in chunks)
    sample_end = max(chunk.sample_end for chunk in chunks)
    response_id = next((_response_id_from_mapping(chunk.data) for chunk in chunks if _response_id_from_mapping(chunk.data)), None)
    return AudioHint(
        stream=stream,
        wav_path=wav_path,
        metadata_path=metadata_path,
        start_ts=round(start_ts, 3),
        end_ts=round(end_ts, 3),
        sample_start=sample_start,
        sample_end=sample_end,
        sample_rate=sample_rate,
        response_id=response_id,
    )


def _derive_timeline_model(rows: list[TimelineRow]) -> TimelineModel:
    sorted_rows = sorted(rows, key=lambda row: (row.ts, row.lane, row.line))
    spans: list[TimelineSpan] = []
    markers: list[TimelineMarker] = []

    spans.extend(_pair_single_active_span(
        sorted_rows,
        entity="policy/wave_conversation",
        label="conversation envelope",
        start_predicate=lambda row: _canonical_type(row) == "policy.conversation_opened",
        end_predicate=lambda row: _canonical_type(row) == "policy.conversation_closed",
    ))
    spans.extend(_pair_backend_spans(sorted_rows))
    spans.extend(_pair_single_active_span(
        sorted_rows,
        entity="robot/speaker",
        label="robot-speaking",
        start_predicate=_is_audio_started,
        end_predicate=_is_audio_done,
    ))
    spans.extend(_pair_single_active_span(
        sorted_rows,
        entity="robot/antennas/thinking",
        label="thinking-cue",
        start_predicate=_is_thinking_antenna_started,
        end_predicate=_is_thinking_antenna_stopped,
    ))
    spans.extend(_pair_single_active_span(
        sorted_rows,
        entity="robot/antennas/pulse",
        label="reaction-pulse",
        start_predicate=_is_policy_pulse_started,
        end_predicate=_is_policy_pulse_stopped,
    ))
    spans.extend(_pair_single_active_span(
        sorted_rows,
        entity="robot/antennas/ready_cue",
        label="startup-ready-cue",
        start_predicate=_is_ready_cue_started,
        end_predicate=_is_ready_cue_stopped,
    ))

    last_policy_behavior: str | None = None
    for row in sorted_rows:
        marker_entity = _marker_entity(row, fallback_policy_behavior=last_policy_behavior)
        if marker_entity is None:
            continue
        markers.append(TimelineMarker(entity=marker_entity, label=_summarize_row(row), ts=row.ts, row=row))
        policy_behavior = _policy_behavior(row, fallback=last_policy_behavior)
        if policy_behavior is not None and policy_behavior != "policy/conversation_cue":
            last_policy_behavior = policy_behavior

    allowlisted_rows = _dedupe_timeline_rows(
        [span.start_row for span in spans]
        + [span.end_row for span in spans]
        + [marker.row for marker in markers]
    )
    return TimelineModel(spans=spans, markers=markers, allowlisted_rows=allowlisted_rows)


def _dedupe_timeline_rows(rows: Iterable[TimelineRow]) -> list[TimelineRow]:
    deduped: list[TimelineRow] = []
    seen: set[tuple[Any, ...]] = set()
    for row in sorted(rows, key=lambda item: (item.ts, item.lane, item.line)):
        key = _row_identity(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _row_identity(row: TimelineRow) -> tuple[Any, ...]:
    return (
        round(row.ts, 3),
        _canonical_type(row),
        row.source,
        row.data.get("event_ts"),
        row.data.get("response_id"),
        row.data.get("reason"),
        row.data.get("action"),
        row.data.get("milestone"),
        row.data.get("cue"),
        row.data.get("event_phase"),
        row.data.get("phase"),
        row.data.get("n"),
    )


def _pair_single_active_span(
    rows: Iterable[TimelineRow],
    *,
    entity: str,
    label: str,
    start_predicate: Any,
    end_predicate: Any,
) -> list[TimelineSpan]:
    spans: list[TimelineSpan] = []
    active: TimelineRow | None = None
    for row in rows:
        if active is None and start_predicate(row):
            active = row
            continue
        if active is not None and end_predicate(row):
            spans.append(_span(entity=entity, label=label, start=active, end=row))
            active = None
    return spans


def _pair_backend_spans(rows: Iterable[TimelineRow]) -> list[TimelineSpan]:
    spans: list[TimelineSpan] = []
    active: TimelineRow | None = None
    for row in rows:
        if _is_backend_user_start(row):
            if active is None:
                active = row
            continue
        if _is_response_created(row):
            if active is None:
                active = row
            continue
        if active is not None and _is_response_output_done(row):
            spans.append(_span(entity="backend/processing", label="backend-processing", start=active, end=row))
            active = None
    return spans


def _span(*, entity: str, label: str, start: TimelineRow, end: TimelineRow) -> TimelineSpan:
    return TimelineSpan(
        entity=entity,
        label=label,
        start_ts=start.ts,
        end_ts=end.ts,
        start_row=start,
        end_row=end,
    )


def _marker_entity(row: TimelineRow, *, fallback_policy_behavior: str | None) -> str | None:
    if row.lane == "markers":
        return "human/feedback"

    kind = _canonical_type(row)
    policy_behavior = _policy_behavior(row, fallback=fallback_policy_behavior)
    if policy_behavior is not None:
        return policy_behavior

    if kind in {"vision.wave", "vision.approach", "vision.depart"}:
        return f"perception/{kind.removeprefix('vision.')}"

    if kind == "runtime.milestone" and _is_session_milestone(row):
        return "session/milestones"

    return None


def _policy_behavior(row: TimelineRow, *, fallback: str | None = None) -> str | None:
    kind = _canonical_type(row)
    action = row.data.get("action")
    reason = row.data.get("reason")

    if kind in {"policy.greet", "policy.greet_suppressed"}:
        return "policy/greet"
    if kind in {"policy.farewell", "policy.farewell_suppressed"}:
        return "policy/farewell"
    if kind in {
        "policy.wave_received",
        "policy.conversation_opened",
        "policy.conversation_closed",
        "policy.conversation_already_active",
    }:
        return "policy/wave_conversation"
    if kind in {
        "policy.conversation_cue.thinking_started",
        "policy.conversation_cue.thinking_stopped",
        "policy.conversation_cue.start_suppressed",
    }:
        return "policy/conversation_cue"
    if kind == "policy.cooldown_skip":
        if action == "greet":
            return "policy/greet"
        if action == "farewell":
            return "policy/farewell"
        if action == "conversation_open":
            return "policy/wave_conversation"
    if kind == "policy.speech_requested":
        if reason == "approach":
            return "policy/greet"
        if reason == "depart":
            return "policy/farewell"
        if reason == "wave":
            return "policy/wave_conversation"
    if kind == "policy.antenna_pulse":
        if reason == "approach":
            return "policy/greet"
        if reason == "depart":
            return "policy/farewell"
        if reason == "wave":
            return "policy/wave_conversation"
        return fallback
    return None


def _canonical_type(row: TimelineRow) -> str:
    value = row.type
    if value.startswith("policy."):
        return value
    if row.lane == "policies" or row.source in {"reception", "conversation_cue"}:
        if value.startswith("conversation_cue."):
            return f"policy.{value}"
        if value in {
            "greet",
            "farewell",
            "wave_received",
            "conversation_opened",
            "conversation_closed",
            "conversation_already_active",
            "cooldown_skip",
            "speech_requested",
            "antenna_pulse",
            "greet_suppressed",
            "farewell_suppressed",
        }:
            return f"policy.{value}"
    return value


def _is_session_milestone(row: TimelineRow) -> bool:
    milestone = row.data.get("milestone")
    if not isinstance(milestone, str):
        return False
    return milestone in SESSION_MILESTONES


def _is_backend_user_start(row: TimelineRow) -> bool:
    return _normalized_type(row.type) == "input_audio_buffer.speech_started"


def _is_response_output_done(row: TimelineRow) -> bool:
    return _normalized_type(row.type) == "response.output_audio.done"


def _is_thinking_antenna_started(row: TimelineRow) -> bool:
    return row.type == "runtime.antenna_cue" and row.data.get("cue") == "thinking" and row.data.get("event_phase") == "started"


def _is_thinking_antenna_stopped(row: TimelineRow) -> bool:
    return row.type == "runtime.antenna_cue" and row.data.get("cue") == "thinking" and row.data.get("event_phase") == "stopped"


def _is_policy_pulse_started(row: TimelineRow) -> bool:
    return (
        row.type == "runtime.antenna_cue"
        and row.data.get("cue") == "policy_pulse"
        and row.data.get("event_phase", row.data.get("phase")) in {"started", "high"}
    )


def _is_policy_pulse_stopped(row: TimelineRow) -> bool:
    return (
        row.type == "runtime.antenna_cue"
        and row.data.get("cue") == "policy_pulse"
        and row.data.get("event_phase", row.data.get("phase")) in {"stopped", "rest"}
    )


def _is_ready_cue_started(row: TimelineRow) -> bool:
    return row.type == "runtime.ready_cue" and row.data.get("cue") == "ready" and row.data.get("phase") == "high"


def _is_ready_cue_stopped(row: TimelineRow) -> bool:
    return row.type == "runtime.ready_cue" and row.data.get("cue") == "ready" and row.data.get("phase") == "rest"


def _derive_turns(rows: list[TimelineRow]) -> list[ConversationTurn]:
    transcripts = [row for row in rows if _transcript_text(row)]
    turns: list[ConversationTurn] = []
    for index, transcript_row in enumerate(transcripts, start=1):
        next_transcript = transcripts[index] if index < len(transcripts) else None
        turn_window = [
            row
            for row in rows
            if row.ts >= transcript_row.ts and (next_transcript is None or row.ts < next_transcript.ts)
        ]
        thinking = _first(turn_window, _is_thinking_started)
        first_audio = _first(turn_window, _is_audio_started)
        response_id = _response_id_from_row(first_audio)
        response_created = _first(
            turn_window,
            lambda row: _is_response_created(row)
            and (response_id is None or _response_id_from_row(row) in (None, response_id)),
        )
        if response_id is None:
            response_id = _response_id_from_row(response_created)
        audio_done = _matching_audio_done(rows, first_audio)
        transcript = _transcript_text(transcript_row) or ""
        turns.append(
            ConversationTurn(
                index=index,
                transcript_ts=transcript_row.ts,
                transcript=transcript,
                response_id=response_id,
                thinking_ts=thinking.ts if thinking else None,
                response_created_ts=response_created.ts if response_created else None,
                first_audio_ts=first_audio.ts if first_audio else None,
                audio_done_ts=audio_done.ts if audio_done else None,
                latency_s={
                    "transcript_to_thinking": _delta(transcript_row, thinking),
                    "transcript_to_response_created": _delta(transcript_row, response_created),
                    "response_created_to_first_audio": _delta(response_created, first_audio),
                    "first_audio_to_audio_done": _delta(first_audio, audio_done),
                    "transcript_to_audio_done": _delta(transcript_row, audio_done),
                },
            )
        )
    return turns


def _matching_audio_done(rows: list[TimelineRow], first_audio: TimelineRow | None) -> TimelineRow | None:
    if first_audio is None:
        return None
    after_audio = [row for row in rows if row.ts >= first_audio.ts]
    next_audio = _first(
        (row for row in after_audio if row is not first_audio),
        _is_audio_started,
    )
    for row in after_audio:
        if next_audio is not None and row.ts >= next_audio.ts:
            return None
        if _is_audio_done(row):
            return row
    return None


def _artifact_entries(manifest: Mapping[str, Any], lane: str) -> list[dict[str, Any]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return []
    entries = artifacts.get(lane)
    if not isinstance(entries, list):
        return []
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)]


def _resolve_path(raw_path: Any, *, run_root: Path, subdir: str | None = None) -> Path:
    path = Path(str(raw_path)).expanduser()
    candidates = []
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.extend([run_root / path, path])
    if subdir:
        candidates.append(run_root / subdir / path.name)
    if path.parent.name:
        candidates.append(run_root / path.parent.name / path.name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else path


def _transcript_text(row: TimelineRow) -> str | None:
    kind = _normalized_type(row.type)
    if kind not in TRANSCRIPT_KINDS:
        return None
    if row.data.get("final") is False:
        return None
    role = row.data.get("role")
    if role not in (None, "", "user", "transcript", "user_transcript"):
        return None
    text = row.data.get("transcript")
    if text is None:
        text = row.data.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text or None


def _normalized_type(value: str) -> str:
    for prefix in ("hf.realtime.", "realtime."):
        if value.startswith(prefix):
            return value.removeprefix(prefix)
    return value


def _is_thinking_started(row: TimelineRow) -> bool:
    return row.type in {
        "assistant.thinking.started",
        "policy.conversation_cue.thinking_started",
        "conversation_cue.thinking_started",
    }


def _is_response_created(row: TimelineRow) -> bool:
    return _normalized_type(row.type) == "response.created"


def _is_audio_started(row: TimelineRow) -> bool:
    return row.type == "assistant.audio.started"


def _is_audio_done(row: TimelineRow) -> bool:
    return row.type == "assistant.audio.done"


def _is_suppression(row: TimelineRow) -> bool:
    return "suppressed" in row.type


def _response_matches(row: TimelineRow, response_created: TimelineRow | None) -> bool:
    expected = _response_id_from_row(response_created)
    actual = _response_id_from_row(row)
    return expected is None or actual is None or expected == actual


def _response_id_from_row(row: TimelineRow | None) -> str | None:
    if row is None:
        return None
    return _response_id_from_mapping(row.data)


def _response_id_from_mapping(data: Mapping[str, Any]) -> str | None:
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


def _first(rows: Iterable[TimelineRow], predicate: Any) -> TimelineRow | None:
    for row in rows:
        if predicate(row):
            return row
    return None


def _delta(start: TimelineRow | None, end: TimelineRow | None) -> float | None:
    if start is None or end is None:
        return None
    return round(max(0.0, end.ts - start.ts), 3)


def _entity_for_row(row: TimelineRow) -> str:
    if row.lane == "markers":
        return "markers"
    if row.type.startswith("hf."):
        return f"conversation/{row.type}"
    if row.type.startswith("policy.") or row.lane == "policies":
        return f"policy/{row.type}"
    if row.lane == "realtime":
        return f"realtime/{row.type}"
    return f"events/{row.type}"


def _summarize_row(row: TimelineRow) -> str:
    parts = [f"{row.ts:.3f}", row.lane, row.type]
    for key in ("n", "note", "text", "transcript", "reason", "milestone", "event_kind", "response_id"):
        value = row.data.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    response_id = _response_id_from_row(row)
    if response_id and "response_id" not in row.data:
        parts.append(f"response_id={response_id}")
    return " | ".join(parts)


def _summarize_audio_hint(hint: AudioHint) -> str:
    response = f" response={hint.response_id}" if hint.response_id else ""
    duration = hint.duration_s
    duration_text = f" duration={duration:.3f}s" if duration is not None else ""
    return (
        f"{hint.stream}{response} wav={hint.wav_path} "
        f"samples={hint.sample_start}:{hint.sample_end}{duration_text}"
    )


def _render_video_to_rerun(rr: Any, review: RunReview) -> None:
    for entry in _artifact_entries(review.manifest, "video"):
        raw_path = entry.get("path")
        if not raw_path:
            continue
        path = _resolve_path(raw_path, run_root=review.run_root, subdir=LANE_SUBDIRS.get("video"))
        if not path.exists():
            _rr_log_video_warning(rr, review, entry, f"video file missing: {path}")
            continue

        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError:
            _rr_log_video_warning(rr, review, entry, "opencv-python is not installed; camera/image not rendered")
            continue

        timestamps, timestamp_source = _video_frame_timestamps(entry, review)
        cap = cv2.VideoCapture(str(path))
        decoded_frames = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                frame_ts = (
                    timestamps[decoded_frames]
                    if decoded_frames < len(timestamps)
                    else _nominal_video_ts(entry, review, decoded_frames)
                )
                _rr_set_time(rr, frame_ts)
                _rr_log_video_frame(rr, "camera/image", cv2, frame)
                decoded_frames += 1
        finally:
            release = getattr(cap, "release", None)
            if callable(release):
                release()

        if timestamps and decoded_frames != len(timestamps):
            _rr_log_video_warning(
                rr,
                review,
                entry,
                (
                    "video frame/timestamp count mismatch: "
                    f"decoded_frames={decoded_frames} timestamps={len(timestamps)} source={timestamp_source}; "
                    "overlap used recorded timestamps, unmatched frames are approximate"
                ),
            )
        elif not timestamps and decoded_frames:
            _rr_log_video_warning(
                rr,
                review,
                entry,
                "video rendered with nominal fps timestamps only; no sidecar or capture timestamps found",
            )


def _video_frame_timestamps(entry: Mapping[str, Any], review: RunReview) -> tuple[list[float], str]:
    metadata = entry.get("metadata")
    if metadata:
        meta_path = _resolve_path(metadata, run_root=review.run_root, subdir=LANE_SUBDIRS.get("video"))
        if meta_path.exists():
            timestamps = _load_video_sidecar_timestamps(meta_path)
            if timestamps:
                return timestamps, f"sidecar:{meta_path.name}"

    capture_timestamps = _load_capture_frame_timestamps(review.manifest, run_root=review.run_root)
    if capture_timestamps:
        return capture_timestamps, "capture"

    return [], "nominal_fps"


def _load_video_sidecar_timestamps(path: Path) -> list[float]:
    indexed: list[tuple[int, float]] = []
    unindexed: list[float] = []
    for _, payload in _iter_jsonl(path):
        if payload.get("type") not in (None, "frame"):
            continue
        ts = _float_or_none(payload.get("ts"))
        if ts is None:
            continue
        frame_index = _int_or_none(payload.get("frame_index"))
        if frame_index is None:
            unindexed.append(ts)
        else:
            indexed.append((frame_index, ts))
    if indexed:
        return [ts for _, ts in sorted(indexed, key=lambda item: item[0])]
    return unindexed


def _nominal_video_ts(entry: Mapping[str, Any], review: RunReview, frame_index: int) -> float:
    started_ts = _float_or_none(entry.get("started_ts")) or _float_or_none(review.manifest.get("started_ts")) or 0.0
    fps = max(1.0, _float_or_none(entry.get("fps")) or 1.0)
    return round(started_ts + frame_index / fps, 3)


def _video_frame_to_rgb(cv2: Any, frame: Any) -> Any:
    cvt_color = getattr(cv2, "cvtColor", None)
    bgr_to_rgb = getattr(cv2, "COLOR_BGR2RGB", None)
    if callable(cvt_color) and bgr_to_rgb is not None:
        try:
            return cvt_color(frame, bgr_to_rgb)
        except Exception:  # noqa: BLE001
            return frame
    return frame


def _rr_log_video_warning(rr: Any, review: RunReview, entry: Mapping[str, Any], text: str) -> None:
    ts = _float_or_none(entry.get("started_ts")) or _float_or_none(review.manifest.get("started_ts"))
    if ts is None and review.timeline:
        ts = review.timeline[0].ts
    _rr_set_time(rr, ts or 0.0)
    _rr_log_text(rr, "camera/warnings", text)


def _summarize_span_boundary(span: TimelineSpan, *, boundary: str, state: float) -> str:
    row = span.start_row if boundary == "START" else span.end_row
    parts = [
        f"{boundary} {span.label}",
        f"state={state:.1f}",
        f"entity={span.entity}",
        f"source_type={row.type}",
        f"source_ts={row.ts:.3f}",
        f"lane={row.lane}",
    ]
    if row.source:
        parts.append(f"source={row.source}")
    for key in ("response_id", "item_id", "cue", "phase", "event_phase", "reason", "action", "event_kind"):
        value = row.data.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    response_id = _response_id_from_row(row)
    if response_id and "response_id" not in row.data:
        parts.append(f"response_id={response_id}")
    return " | ".join(parts)


def _rr_init(rr: Any, *, recording_id: str, spawn: bool) -> None:
    init = getattr(rr, "init", None)
    if callable(init):
        try:
            init("reachy-mini-rerun-review", recording_id=recording_id, spawn=spawn)
        except TypeError:
            init("reachy-mini-rerun-review", spawn=spawn)


def _rr_save(rr: Any, path: Path) -> None:
    save = getattr(rr, "save", None)
    if not callable(save):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    save(str(path))


def _rr_set_time(rr: Any, ts: float) -> None:
    set_time = getattr(rr, "set_time", None)
    if callable(set_time):
        try:
            set_time("wall", timestamp=ts)
            return
        except TypeError:
            pass
    set_time_seconds = getattr(rr, "set_time_seconds", None)
    if callable(set_time_seconds):
        try:
            set_time_seconds("wall", seconds=ts)
        except TypeError:
            set_time_seconds("wall", ts)


def _rr_log_text(rr: Any, entity: str, text: str) -> None:
    text_log = getattr(rr, "TextLog", None)
    value = text_log(text) if callable(text_log) else text
    rr.log(entity, value)


def _rr_log_scalar(rr: Any, entity: str, value: float) -> None:
    scalars = getattr(rr, "Scalars", None)
    scalar = getattr(rr, "Scalar", None)
    if callable(scalars):
        logged_value = scalars(value)
    elif callable(scalar):
        logged_value = scalar(value)
    else:
        logged_value = value
    rr.log(entity, logged_value)


def _rr_log_image(rr: Any, entity: str, frame: Any) -> None:
    image = getattr(rr, "Image", None)
    value = image(frame) if callable(image) else frame
    rr.log(entity, value)


def _rr_log_video_frame(rr: Any, entity: str, cv2: Any, frame: Any) -> None:
    encoded_image = getattr(rr, "EncodedImage", None)
    imencode = getattr(cv2, "imencode", None)
    if callable(encoded_image) and callable(imencode):
        quality_key = getattr(cv2, "IMWRITE_JPEG_QUALITY", 1)
        ok, encoded = imencode(".jpg", frame, [quality_key, 80])
        if ok:
            rr.log(
                entity,
                encoded_image(contents=encoded.tobytes(), media_type="image/jpeg"),
            )
            return
    _rr_log_image(rr, entity, _video_frame_to_rgb(cv2, frame))


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 3)


if __name__ == "__main__":
    main()
