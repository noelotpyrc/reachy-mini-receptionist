from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from click.testing import CliRunner

from reachy_mini_brain.official_runtime.rerun_review import (
    cli,
    format_text_review,
    load_run_review,
    render_review_to_rerun,
)


def test_load_run_review_derives_multilane_timeline_latency_and_audio_hints(tmp_path: Path) -> None:
    run_root = _make_synthetic_run(tmp_path)

    review = load_run_review(run_root)

    assert review.run_id == "official-live-test"
    assert {row.lane for row in review.timeline} == {"capture", "events", "markers", "policies", "realtime"}
    assert any(row.type == "hf.realtime.conversation.item.input_audio_transcription.completed" for row in review.timeline)
    assert any(row.type == "runtime.milestone" for row in review.timeline)
    assert {span.entity for span in review.model.spans} >= {
        "policy/wave_conversation",
        "backend/processing",
        "robot/speaker",
        "robot/antennas/thinking",
        "robot/antennas/pulse",
        "robot/antennas/ready_cue",
    }
    assert {marker.entity for marker in review.model.markers} >= {
        "policy/wave_conversation",
        "policy/conversation_cue",
        "perception/wave",
        "perception/approach",
        "human/feedback",
        "session/milestones",
    }

    assert len(review.turns) == 1
    turn = review.turns[0]
    assert turn.transcript == "Where should I check in?"
    assert turn.response_id == "resp-1"
    assert turn.latency_s == {
        "transcript_to_thinking": 0.1,
        "transcript_to_response_created": 1.0,
        "response_created_to_first_audio": 0.4,
        "first_audio_to_audio_done": 1.0,
        "transcript_to_audio_done": 2.4,
    }

    assert len(review.suppressions) == 1
    assert review.suppressions[0].type == "conversation_cue.start_suppressed"
    assert review.suppressions[0].data["reason"] == "robot_speaking"

    assert len(review.audio_hints) == 1
    hint = review.audio_hints[0]
    assert hint.stream == "response-resp_1"
    assert hint.response_id == "resp-1"
    assert hint.wav_path == run_root / "audio" / "audio-response-resp_1-official-live-test-01.wav"
    assert hint.sample_start == 0
    assert hint.sample_end == 640
    assert hint.duration_s == 0.04

    text = format_text_review(review)
    assert "lag felt here" in text
    assert "response-resp_1" in text
    assert str(hint.wav_path) in text


def test_rerun_review_cli_json_output(tmp_path: Path) -> None:
    run_root = _make_synthetic_run(tmp_path)

    result = CliRunner().invoke(cli, [str(run_root), "--json-output"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["run_id"] == "official-live-test"
    assert payload["timeline_rows"] >= 10
    assert payload["model"]["spans"]
    assert payload["model"]["markers"]
    assert payload["turns"][0]["response_id"] == "resp-1"
    assert payload["audio_hints"][0]["sample_end"] == 640


def test_turn_pairing_does_not_attach_late_response_to_prior_transcripts(tmp_path: Path) -> None:
    run_root = _make_synthetic_run_with_two_transcripts_before_response(tmp_path)

    review = load_run_review(run_root)

    assert [turn.transcript for turn in review.turns] == ["first partial thought", "actual question"]
    assert review.turns[0].response_id is None
    assert review.turns[0].first_audio_ts is None
    assert review.turns[1].response_id == "resp-late"
    assert review.turns[1].first_audio_ts == 13.0
    assert review.turns[1].latency_s["transcript_to_audio_done"] == 1.5


def test_render_review_to_rerun_uses_optional_sdk(monkeypatch, tmp_path: Path) -> None:
    run_root = _make_synthetic_run(tmp_path)
    review = load_run_review(run_root)
    calls: list[tuple[str, object, object | None]] = []

    def fake_init(*args: object, **kwargs: object) -> None:
        calls.append(("init", args, kwargs))

    def fake_save(path: str) -> None:
        calls.append(("save", path, None))

    def fake_set_time(timeline: str, *, timestamp: float) -> None:
        calls.append(("time", timeline, timestamp))

    def fake_log(entity: str, value: object) -> None:
        calls.append(("log", entity, value))

    fake_rerun = types.SimpleNamespace(
        init=fake_init,
        save=fake_save,
        set_time=fake_set_time,
        log=fake_log,
        TextLog=lambda text: ("text", text),
        Scalars=lambda value: ("scalars", value),
    )
    monkeypatch.setitem(sys.modules, "rerun", fake_rerun)

    render_review_to_rerun(review, save_path=tmp_path / "review.rrd")

    logged_entities = [call[1] for call in calls if call[0] == "log"]
    assert "policy/wave_conversation" in logged_entities
    assert "policy/conversation_cue" in logged_entities
    assert "backend/processing" in logged_entities
    assert "robot/speaker" in logged_entities
    assert "robot/antennas/thinking" in logged_entities
    assert "robot/antennas/pulse" in logged_entities
    assert "robot/antennas/ready_cue" in logged_entities
    assert "perception/approach" in logged_entities
    assert "human/feedback" in logged_entities
    assert "backend/latency/transcript_to_response_created" not in logged_entities
    assert "backend/turn" not in logged_entities
    assert "audio/response-resp_1/rms" in logged_entities
    assert all(not str(entity).startswith("conversation/") for entity in logged_entities)
    text_values = [call[2][1] for call in calls if call[0] == "log" and isinstance(call[2], tuple) and call[2][0] == "text"]
    assert any("START backend-processing" in value and "state=1.0" in value for value in text_values)
    assert any("END backend-processing" in value and "state=0.0" in value for value in text_values)
    assert any(call[0] == "save" and call[1] == str(tmp_path / "review.rrd") for call in calls)


def test_render_review_to_rerun_places_video_frames_on_sidecar_timestamps(monkeypatch, tmp_path: Path) -> None:
    run_root = _make_synthetic_video_run(tmp_path)
    review = load_run_review(run_root)
    current_time = {"ts": None}
    calls: list[tuple[str, object, object | None]] = []

    class FakeCapture:
        def __init__(self, path: str) -> None:
            self.path = path
            self.frames = ["bgr-0", "bgr-1"]

        def read(self) -> tuple[bool, object | None]:
            if not self.frames:
                return False, None
            return True, self.frames.pop(0)

        def release(self) -> None:
            return None

    def fake_set_time(timeline: str, *, timestamp: float) -> None:
        current_time["ts"] = timestamp
        calls.append(("time", timeline, timestamp))

    def fake_log(entity: str, value: object) -> None:
        calls.append(("log", entity, (current_time["ts"], value)))

    fake_rerun = types.SimpleNamespace(
        init=lambda *args, **kwargs: None,
        set_time=fake_set_time,
        log=fake_log,
        TextLog=lambda text: ("text", text),
        Scalars=lambda value: ("scalars", value),
        Image=lambda frame: ("image", frame),
    )
    fake_cv2 = types.SimpleNamespace(
        VideoCapture=FakeCapture,
        COLOR_BGR2RGB=1,
        cvtColor=lambda frame, code: f"rgb-{frame}",
    )
    monkeypatch.setitem(sys.modules, "rerun", fake_rerun)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    render_review_to_rerun(review)

    image_logs = [call for call in calls if call[0] == "log" and call[1] == "camera/image"]
    assert image_logs == [
        ("log", "camera/image", (200.25, ("image", "rgb-bgr-0"))),
        ("log", "camera/image", (200.75, ("image", "rgb-bgr-1"))),
    ]
    assert "camera/warnings" not in [call[1] for call in calls if call[0] == "log"]


def test_render_review_to_rerun_jpeg_encodes_video_frames(monkeypatch, tmp_path: Path) -> None:
    run_root = _make_synthetic_video_run(tmp_path)
    review = load_run_review(run_root)
    calls: list[tuple[str, object]] = []

    class FakeCapture:
        def __init__(self, path: str) -> None:
            self.frames = ["bgr-0", "bgr-1"]

        def read(self) -> tuple[bool, object | None]:
            if not self.frames:
                return False, None
            return True, self.frames.pop(0)

        def release(self) -> None:
            return None

    class Encoded:
        def __init__(self, value: bytes) -> None:
            self.value = value

        def tobytes(self) -> bytes:
            return self.value

    fake_rerun = types.SimpleNamespace(
        init=lambda *args, **kwargs: None,
        set_time=lambda *args, **kwargs: None,
        log=lambda entity, value: calls.append((entity, value)),
        TextLog=lambda text: ("text", text),
        Scalars=lambda value: ("scalars", value),
        Image=lambda frame: ("image", frame),
        EncodedImage=lambda **kwargs: ("encoded", kwargs),
    )
    fake_cv2 = types.SimpleNamespace(
        VideoCapture=FakeCapture,
        IMWRITE_JPEG_QUALITY=7,
        imencode=lambda extension, frame, options: (True, Encoded(f"jpeg-{frame}".encode())),
    )
    monkeypatch.setitem(sys.modules, "rerun", fake_rerun)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    render_review_to_rerun(review)

    image_values = [value for entity, value in calls if entity == "camera/image"]
    assert image_values == [
        ("encoded", {"contents": b"jpeg-bgr-0", "media_type": "image/jpeg"}),
        ("encoded", {"contents": b"jpeg-bgr-1", "media_type": "image/jpeg"}),
    ]


def test_render_review_to_rerun_falls_back_to_capture_timestamps_and_warns(monkeypatch, tmp_path: Path) -> None:
    run_root = _make_synthetic_video_run_with_capture_fallback(tmp_path)
    review = load_run_review(run_root)
    current_time = {"ts": None}
    calls: list[tuple[str, object, object | None]] = []

    class FakeCapture:
        def __init__(self, path: str) -> None:
            self.path = path
            self.frames = ["frame-0", "frame-1"]

        def read(self) -> tuple[bool, object | None]:
            if not self.frames:
                return False, None
            return True, self.frames.pop(0)

        def release(self) -> None:
            return None

    def fake_set_time(timeline: str, *, timestamp: float) -> None:
        current_time["ts"] = timestamp
        calls.append(("time", timeline, timestamp))

    def fake_log(entity: str, value: object) -> None:
        calls.append(("log", entity, (current_time["ts"], value)))

    fake_rerun = types.SimpleNamespace(
        init=lambda *args, **kwargs: None,
        set_time=fake_set_time,
        log=fake_log,
        TextLog=lambda text: ("text", text),
        Image=lambda frame: ("image", frame),
    )
    fake_cv2 = types.SimpleNamespace(VideoCapture=FakeCapture)
    monkeypatch.setitem(sys.modules, "rerun", fake_rerun)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    render_review_to_rerun(review)

    image_logs = [call for call in calls if call[0] == "log" and call[1] == "camera/image"]
    warning_logs = [call for call in calls if call[0] == "log" and call[1] == "camera/warnings"]
    assert image_logs == [
        ("log", "camera/image", (210.1, ("image", "frame-0"))),
        ("log", "camera/image", (210.3, ("image", "frame-1"))),
    ]
    assert len(warning_logs) == 1
    assert "decoded_frames=2 timestamps=3 source=capture" in warning_logs[0][2][1][1]


def _make_synthetic_run(tmp_path: Path) -> Path:
    artifact_root = tmp_path / "artifacts"
    run_root = artifact_root / "official-runtime-live"
    run_id = "official-live-test"
    events_path = run_root / "events" / f"events-{run_id}-01.jsonl"
    realtime_path = run_root / "realtime" / f"realtime-{run_id}-01.jsonl"
    policies_path = run_root / "policies" / f"policies-{run_id}-01.jsonl"
    capture_path = run_root / "capture" / f"capture-{run_id}-01.jsonl"
    audio_path = run_root / "audio" / f"audio-response-resp_1-{run_id}-01.wav"
    audio_meta_path = audio_path.with_suffix(".jsonl")
    manifest_path = run_root / "runs" / f"run-{run_id}.json"
    markers_path = artifact_root / f"markers-{run_id}.jsonl"

    _write_jsonl(
        events_path,
        [
            {"run_id": run_id, "ts": 99.5, "type": "run.started", "source": "official_runtime.artifacts"},
            {
                "run_id": run_id,
                "ts": 99.7,
                "type": "runtime.ready_cue",
                "source": "official_runtime.live_app",
                "cue": "ready",
                "phase": "high",
            },
            {
                "run_id": run_id,
                "ts": 99.72,
                "type": "runtime.ready_cue",
                "source": "official_runtime.live_app",
                "cue": "ready",
                "phase": "rest",
            },
            {
                "run_id": run_id,
                "ts": 99.8,
                "type": "runtime.antenna_cue",
                "source": "official_runtime.live_app",
                "cue": "policy_pulse",
                "phase": "high",
            },
            {
                "run_id": run_id,
                "ts": 99.9,
                "type": "hf.realtime.input_audio_buffer.speech_started",
                "source": "official_runtime.hf_official",
            },
            {
                "run_id": run_id,
                "ts": 100.0,
                "type": "hf.realtime.conversation.item.input_audio_transcription.completed",
                "source": "official_runtime.hf_official",
                "role": "user",
                "text": "Where should I check in?",
                "final": True,
            },
            {
                "run_id": run_id,
                "ts": 100.05,
                "type": "runtime.antenna_cue",
                "source": "official_runtime.moves",
                "cue": "thinking",
                "event_phase": "started",
            },
            {
                "run_id": run_id,
                "ts": 100.3,
                "type": "runtime.antenna_cue",
                "source": "official_runtime.live_app",
                "cue": "policy_pulse",
                "phase": "rest",
            },
            {
                "run_id": run_id,
                "ts": 101.0,
                "type": "hf.realtime.response.created",
                "source": "official_runtime.hf_official",
                "response_id": "resp-1",
            },
            {
                "run_id": run_id,
                "ts": 101.4,
                "type": "assistant.audio.started",
                "source": "official_runtime.stream",
                "metadata": {"response_id": "resp-1"},
                "samples": 320,
            },
            {
                "run_id": run_id,
                "ts": 101.4,
                "type": "runtime.antenna_cue",
                "source": "official_runtime.moves",
                "cue": "thinking",
                "event_phase": "stopped",
                "reason": "assistant.audio.started",
            },
            {
                "run_id": run_id,
                "ts": 102.0,
                "type": "hf.realtime.response.output_audio.done",
                "source": "official_runtime.hf_official",
                "response_id": "resp-1",
            },
            {
                "run_id": run_id,
                "ts": 102.4,
                "type": "assistant.audio.done",
                "source": "official_runtime.stream",
                "reason": "output_idle",
            },
        ],
    )
    _write_jsonl(
        realtime_path,
        [
            {
                "run_id": run_id,
                "ts": 99.6,
                "type": "runtime.milestone",
                "milestone": "robot_control_ready",
            },
            {"run_id": run_id, "ts": 99.7, "type": "movement_gate", "active": False},
        ],
    )
    _write_jsonl(
        capture_path,
        [
            {
                "run_id": run_id,
                "ts": 99.55,
                "type": "vision_frame",
                "people": 1,
                "tracks": [{"id": 1, "area": 0.12, "cx": 0.45, "cy": 0.4}],
                "events": [{"kind": "approach", "id": 1, "area": 0.12, "cx": 0.45, "cy": 0.4}],
            },
            {
                "run_id": run_id,
                "ts": 99.75,
                "type": "vision_frame",
                "people": 1,
                "tracks": [{"id": 1, "area": 0.2, "cx": 0.5, "cy": 0.4}],
                "events": [{"kind": "wave", "gesture": "Open_Palm", "score": 0.9}],
            },
        ],
    )
    _write_jsonl(
        policies_path,
        [
            {
                "run_id": run_id,
                "ts": 99.75,
                "type": "wave_received",
                "source": "reception",
                "event": {"kind": "wave"},
            },
            {
                "run_id": run_id,
                "ts": 99.76,
                "type": "conversation_opened",
                "source": "reception",
                "audio_gate_open": True,
            },
            {
                "run_id": run_id,
                "ts": 99.81,
                "type": "antenna_pulse",
                "source": "reception",
            },
            {
                "run_id": run_id,
                "ts": 100.1,
                "type": "conversation_cue.thinking_started",
                "source": "conversation_cue",
                "event_kind": "hf.realtime.conversation.item.input_audio_transcription.completed",
            },
            {
                "run_id": run_id,
                "ts": 100.2,
                "type": "conversation_cue.start_suppressed",
                "source": "conversation_cue",
                "reason": "robot_speaking",
            },
            {
                "run_id": run_id,
                "ts": 103.0,
                "type": "conversation_closed",
                "source": "reception",
                "reason": "explicit_goodbye",
                "audio_gate_open": False,
            },
        ],
    )
    _write_jsonl(
        markers_path,
        [{"run_id": run_id, "n": 1, "ts": 101.25, "clock": "14:00:01", "note": "lag felt here"}],
    )
    _write_jsonl(
        audio_meta_path,
        [
            {
                "run_id": run_id,
                "ts": 101.4,
                "type": "chunk",
                "sample_start": 0,
                "samples": 320,
                "rms": 0.2,
                "response_id": "resp-1",
            },
            {
                "run_id": run_id,
                "ts": 101.42,
                "type": "chunk",
                "sample_start": 320,
                "samples": 320,
                "rms": 0.1,
                "response_id": "resp-1",
            },
            {"run_id": run_id, "ts": 102.4, "type": "stop", "sample_end": 640},
        ],
    )
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"RIFFsynthetic")
    manifest = {
        "run_id": run_id,
        "started_ts": 99.0,
        "artifacts": {
            "events": [{"path": _remote_path(events_path, run_root), "run_id_field": True}],
            "realtime": [{"path": _remote_path(realtime_path, run_root), "run_id_field": True}],
            "policies": [{"path": _remote_path(policies_path, run_root), "run_id_field": True}],
            "capture": [{"path": _remote_path(capture_path, run_root), "run_id_field": True}],
            "audio": [
                {
                    "stream": "response-resp_1",
                    "path": _remote_path(audio_path, run_root),
                    "metadata": _remote_path(audio_meta_path, run_root),
                    "sample_rate": 16000,
                    "status": "closed",
                }
            ],
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_root


def _make_synthetic_video_run(tmp_path: Path) -> Path:
    artifact_root = tmp_path / "artifacts"
    run_root = artifact_root / "official-runtime-live"
    run_id = "official-live-video-test"
    video_path = run_root / "video" / f"video-{run_id}-01.mkv"
    video_meta_path = video_path.with_suffix(".jsonl")
    manifest_path = run_root / "runs" / f"run-{run_id}.json"

    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"fake-video")
    _write_jsonl(
        video_meta_path,
        [
            {"run_id": run_id, "ts": 200.25, "type": "frame", "frame_index": 0, "fps": 5.0},
            {"run_id": run_id, "ts": 200.75, "type": "frame", "frame_index": 1, "fps": 5.0},
        ],
    )
    manifest = {
        "run_id": run_id,
        "started_ts": 200.0,
        "artifacts": {
            "video": [
                {
                    "path": _remote_path(video_path, run_root),
                    "metadata": _remote_path(video_meta_path, run_root),
                    "fps": 5.0,
                    "status": "closed",
                    "started_ts": 200.0,
                    "frames": 2,
                }
            ],
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_root


def _make_synthetic_video_run_with_capture_fallback(tmp_path: Path) -> Path:
    artifact_root = tmp_path / "artifacts"
    run_root = artifact_root / "official-runtime-live"
    run_id = "official-live-video-fallback-test"
    video_path = run_root / "video" / f"video-{run_id}-01.mkv"
    capture_path = run_root / "capture" / f"capture-{run_id}-01.jsonl"
    manifest_path = run_root / "runs" / f"run-{run_id}.json"

    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"fake-video")
    _write_jsonl(
        capture_path,
        [
            {"run_id": run_id, "ts": 210.1, "type": "vision_frame", "people": 0, "tracks": [], "events": []},
            {"run_id": run_id, "ts": 210.3, "type": "vision_frame", "people": 1, "tracks": [], "events": []},
            {"run_id": run_id, "ts": 210.5, "type": "vision_frame", "people": 1, "tracks": [], "events": []},
        ],
    )
    manifest = {
        "run_id": run_id,
        "started_ts": 210.0,
        "artifacts": {
            "capture": [{"path": _remote_path(capture_path, run_root), "status": "closed"}],
            "video": [
                {
                    "path": _remote_path(video_path, run_root),
                    "fps": 5.0,
                    "status": "closed",
                    "started_ts": 210.0,
                    "frames": 2,
                }
            ],
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_root


def _make_synthetic_run_with_two_transcripts_before_response(tmp_path: Path) -> Path:
    artifact_root = tmp_path / "artifacts"
    run_root = artifact_root / "official-runtime-live"
    run_id = "official-live-window-test"
    events_path = run_root / "events" / f"events-{run_id}-01.jsonl"
    realtime_path = run_root / "realtime" / f"realtime-{run_id}-01.jsonl"
    policies_path = run_root / "policies" / f"policies-{run_id}-01.jsonl"
    manifest_path = run_root / "runs" / f"run-{run_id}.json"

    _write_jsonl(
        events_path,
        [
            {
                "run_id": run_id,
                "ts": 10.0,
                "type": "hf.realtime.conversation.item.input_audio_transcription.completed",
                "role": "user",
                "transcript": "first partial thought",
            },
            {
                "run_id": run_id,
                "ts": 12.0,
                "type": "hf.realtime.conversation.item.input_audio_transcription.completed",
                "role": "user",
                "transcript": "actual question",
            },
            {
                "run_id": run_id,
                "ts": 13.0,
                "type": "assistant.audio.started",
                "metadata": {"response_id": "resp-late"},
            },
            {"run_id": run_id, "ts": 13.5, "type": "assistant.audio.done"},
        ],
    )
    _write_jsonl(realtime_path, [{"run_id": run_id, "ts": 9.5, "type": "runtime.started"}])
    _write_jsonl(policies_path, [{"run_id": run_id, "ts": 12.1, "type": "conversation_cue.thinking_started"}])
    manifest = {
        "run_id": run_id,
        "artifacts": {
            "events": [{"path": str(events_path)}],
            "realtime": [{"path": str(realtime_path)}],
            "policies": [{"path": str(policies_path)}],
            "audio": [],
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_root


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _remote_path(path: Path, run_root: Path) -> str:
    return str(
        Path("/Users/leon/projects/reachy_mini_receptionist_clean/artifacts/official-runtime-live")
        / path.relative_to(run_root)
    )
