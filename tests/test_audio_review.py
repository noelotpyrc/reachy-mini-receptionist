from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
from click.testing import CliRunner

from reachy_mini_brain.official_runtime.audio_review import (
    AudioReviewApp,
    _browser_wav_bytes,
    _parse_range,
    build_audio_review_app,
    cli,
    export_aligned_audio_review,
    recover_missing_response_text,
)
from reachy_mini_brain.official_runtime.rerun_review import load_run_review


def test_audio_review_payload_contains_tracks_backend_events_and_markers(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)
    review = load_run_review(run_root)

    app = build_audio_review_app(review)
    payload = app.payload

    assert payload["run_id"] == "audio-review-test"
    assert [track["stream"] for track in payload["tracks"]] == ["input", "output"]
    assert payload["tracks"][0]["url"] == "/audio/input"
    assert payload["tracks"][1]["url"] == "/audio/output"
    assert [event["normalized_type"] for event in payload["backend_events"]] == [
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "conversation.item.input_audio_transcription.completed",
        "response.created",
        "assistant.audio.started",
        "response.output_audio.done",
        "response.done",
        "assistant.audio.done",
    ]
    assert payload["turns"][0]["transcript"] == "I need directions."
    assert payload["turns"][0]["speech_start_ts"] == 100.0
    assert payload["turns"][0]["speech_stop_ts"] == 100.8
    assert payload["turns"][0]["response_done_ts"] == 103.1
    assert payload["turns"][0]["review_start_ts"] == 100.0
    assert payload["turns"][0]["review_end_ts"] == 103.2
    assert payload["markers"][0]["note"] == "speaker sounded quiet"
    assert set(app.audio_files) == {"input", "output", "response-resp-1"}
    assert payload["response_audio"] == [
        {
            "duration_s": 0.04,
            "end_ts": 102.04,
            "id": "response-resp-1",
            "metadata_path": str(run_root / "audio" / "audio-response-resp-1-audio-review-test-01.jsonl"),
            "playback_end_ts": 103.2,
            "playback_start_ts": 102.0,
            "response_id": "resp-1",
            "sample_end": 640,
            "sample_rate": 16000,
            "sample_start": 0,
            "start_ts": 102.0,
            "stream": "response-resp-1",
            "url": "/audio/response-resp-1",
            "wav_path": str(run_root / "audio" / "audio-response-resp-1-audio-review-test-01.wav"),
        }
    ]
    output_track = next(track for track in payload["tracks"] if track["stream"] == "output")
    assert output_track["segments"] == [
        {
            "audio_start_s": 0.0,
            "duration_s": 0.04,
            "end_ts": 103.2,
            "natural_end_ts": 102.04,
            "response_id": "resp-1",
            "sample_end": 640,
            "sample_start": 0,
            "start_ts": 102.0,
            "wall_duration_s": 1.2,
        }
    ]


def test_audio_review_cli_json_output(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)

    result = CliRunner().invoke(cli, [str(run_root), "--json-output"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["run_id"] == "audio-review-test"
    assert len(payload["tracks"]) == 2


def test_audio_review_cli_exports_aligned_review_by_default(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)
    output_dir = tmp_path / "aligned"

    result = CliRunner().invoke(cli, [str(run_root), "--output-dir", str(output_dir)])

    assert result.exit_code == 0
    assert "aligned audio review exported" in result.output
    assert (output_dir / "audio-review-audio-review-test.wav").exists()
    assert (output_dir / "audio-review-audio-review-test.labels.txt").exists()
    assert (output_dir / "audio-review-audio-review-test.json").exists()


def test_audio_review_exports_aligned_stereo_wav_and_labels(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)

    export = export_aligned_audio_review(load_run_review(run_root), output_dir=tmp_path / "review")

    assert export.wav_path.exists()
    assert export.labels_path.exists()
    assert export.metadata_path.exists()
    assert export.start_ts == 100.0
    assert export.sample_rate == 16000
    assert [placement["stream"] for placement in export.placements] == ["input", "output"]
    assert export.placements[0]["channel"] == 0
    assert export.placements[1]["channel"] == 1
    assert export.placements[1]["start_s"] == 2.0

    with wave.open(str(export.wav_path), "rb") as wav:
        assert wav.getnchannels() == 2
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() >= 51200

    labels = export.labels_path.read_text(encoding="utf-8")
    assert "turn 01 input: I need directions." in labels
    assert "turn 01 output resp-1" in labels

    metadata = json.loads(export.metadata_path.read_text(encoding="utf-8"))
    assert metadata["channels"] == {"left": "input", "right": "output"}
    assert metadata["wav_path"] == str(export.wav_path)
    assert metadata["alignment"]["input_anchor_mode"] == "first_speech_vad_sync"


def test_audio_review_payload_can_use_aligned_wav_and_semantic_lanes(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)
    review = load_run_review(run_root)
    export = export_aligned_audio_review(review, output_dir=tmp_path / "review")

    app = build_audio_review_app(review, aligned_export=export)
    payload = app.payload

    assert payload["aligned_audio"]["url"] == "/audio/aligned"
    assert payload["aligned_audio"]["alignment"]["input_anchor_mode"] == "first_speech_vad_sync"
    assert payload["timeline"]["start_ts"] == export.start_ts
    assert "aligned" in app.audio_files
    lane_ids = [lane["id"] for lane in payload["lanes"]]
    lanes = {lane["id"]: lane for lane in payload["lanes"]}
    assert {"vad", "stt_final", "llm_input", "llm", "llm_response", "stt_recovered", "transcript_availability", "tts", "robot_audio", "markers"} <= set(lanes)
    assert lane_ids[lane_ids.index("llm_response") + 1] == "stt_recovered"
    assert lanes["llm"]["label"] == "S2S response lifecycle"
    assert [event["label"] for event in lanes["llm"]["events"]] == [
        "response.created signal",
        "first audio",
        "response done",
        "audio done",
    ]
    assert lanes["llm_response"]["events"] == []
    assert lanes["transcript_availability"]["label"] == "Transcript availability"
    assert lanes["transcript_availability"]["events"][0]["label"] == "no backend transcript event"
    assert "response.output_audio_transcript.done=not recorded" in lanes["transcript_availability"]["events"][0]["detail"]
    assert lanes["vad"]["events"][0]["kind"] == "span"
    assert lanes["stt_final"]["events"][0]["label"] == "I need directions."
    assert lanes["llm_input"]["events"][0]["label"] == "I need directions."


def test_audio_review_payload_uses_recovered_text_in_separate_lane(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)
    review = load_run_review(run_root)
    export = export_aligned_audio_review(review, output_dir=tmp_path / "review")

    app = build_audio_review_app(
        review,
        aligned_export=export,
        recovered_text=[
            {
                "response_id": "resp-1",
                "text": "Please follow the signs to reception.",
                "source": "stt-recovered",
                "model": "m1max parakeet-tdt",
                "audio_path": "/tmp/response.wav",
            }
        ],
    )
    lane_ids = [lane["id"] for lane in app.payload["lanes"]]
    lanes = {lane["id"]: lane for lane in app.payload["lanes"]}

    assert lanes["llm_response"]["events"] == []
    assert lane_ids[lane_ids.index("llm_response") + 1] == "stt_recovered"
    recovered = lanes["stt_recovered"]["events"][0]
    assert recovered["label"] == "Please follow the signs to reception."
    assert recovered["response_id"] == "resp-1"
    assert "provenance=STT recovered from robot response WAV" in recovered["detail"]
    assert "model=m1max parakeet-tdt" in recovered["detail"]
    assert "stt_recovered_text=available" in lanes["transcript_availability"]["events"][0]["detail"]


def test_audio_review_recovers_missing_response_text_sidecar(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)
    review = load_run_review(run_root)
    output_path = tmp_path / "recovered.jsonl"

    export = recover_missing_response_text(
        review,
        output_path=output_path,
        transcribe=lambda candidate: f"Recovered text for {candidate.response_id}",
    )

    assert export.path == output_path
    assert export.candidate_count == 1
    assert export.recovered_count == 1
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert rows == export.rows
    assert rows[0]["run_id"] == "audio-review-test"
    assert rows[0]["response_id"] == "resp-1"
    assert rows[0]["text"] == "Recovered text for resp-1"
    assert rows[0]["source"] == "stt-recovered"
    assert rows[0]["model"] == "m1max speech_to_speech parakeet-tdt"
    assert rows[0]["audio_path"] == str(run_root / "audio" / "audio-response-resp-1-audio-review-test-01.wav")
    assert rows[0]["duration_s"] == 0.04
    assert rows[0]["playback_start_ts"] == 102.0
    assert rows[0]["playback_end_ts"] == 103.2
    assert rows[0]["response_done_ts"] == 103.1


def test_audio_review_recovery_preserves_existing_sidecar_rows(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)
    review = load_run_review(run_root)
    output_path = tmp_path / "recovered.jsonl"
    existing = {
        "response_id": "resp-1",
        "text": "Existing corrected text.",
        "source": "stt-recovered",
        "model": "manual",
    }
    output_path.write_text(json.dumps(existing, sort_keys=True) + "\n", encoding="utf-8")

    def fail_transcribe(candidate: object) -> str:
        raise AssertionError(f"unexpected transcribe call: {candidate}")

    export = recover_missing_response_text(
        review,
        output_path=output_path,
        transcribe=fail_transcribe,
    )

    assert export.candidate_count == 1
    assert export.recovered_count == 0
    assert export.skipped_existing_count == 1
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert rows == [existing]


def test_audio_review_recovery_skips_backend_logged_transcripts(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)
    events_path = run_root / "events" / "events-audio-review-test-01.jsonl"
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "run_id": "audio-review-test",
                    "ts": 103.05,
                    "type": "hf.realtime.response.output_audio_transcript.done",
                    "response_id": "resp-1",
                    "text": "Backend logged text.",
                },
                sort_keys=True,
            )
            + "\n"
        )
    review = load_run_review(run_root)
    output_path = tmp_path / "recovered.jsonl"

    def fail_transcribe(candidate: object) -> str:
        raise AssertionError(f"unexpected transcribe call: {candidate}")

    export = recover_missing_response_text(
        review,
        output_path=output_path,
        transcribe=fail_transcribe,
    )

    assert export.candidate_count == 0
    assert export.recovered_count == 0
    assert output_path.read_text(encoding="utf-8") == ""


def test_audio_review_cli_recovers_missing_text_with_default_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = _make_audio_review_run(tmp_path)

    monkeypatch.setattr(
        "reachy_mini_brain.official_runtime.audio_review._m1max_parakeet_response_transcriber",
        lambda: (lambda candidate: f"CLI recovered {candidate.response_id}"),
    )

    result = CliRunner().invoke(cli, [str(run_root), "--recover-missing-text"])

    assert result.exit_code == 0
    assert "recovered text sidecar written" in result.output
    output_path = run_root / "audio-review" / "audio-review-test" / "recovered-text-audio-review-test.jsonl"
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["response_id"] == "resp-1"
    assert rows[0]["text"] == "CLI recovered resp-1"


def test_audio_review_byte_range_parser(tmp_path: Path) -> None:
    run_root = _make_audio_review_run(tmp_path)
    app = build_audio_review_app(load_run_review(run_root))

    assert app.audio_files["input"].read_bytes().startswith(b"RIFF")
    assert _parse_range("bytes=0-3", size=12) == (0, 3)
    assert _parse_range("bytes=4-", size=12) == (4, 11)
    assert _parse_range("bytes=-4", size=12) == (8, 11)
    assert _parse_range("items=0-3", size=12) == (None, None)


def test_audio_review_converts_float_wav_to_browser_pcm16(tmp_path: Path) -> None:
    sf = pytest.importorskip("soundfile")
    import wave

    path = tmp_path / "float.wav"
    sf.write(str(path), [0.0, 8192.0, -8192.0], 16000, subtype="FLOAT")
    app = AudioReviewApp(payload={}, audio_files={"input": path})

    data = _browser_wav_bytes(app, "input", path)

    assert data.startswith(b"RIFF")
    out = tmp_path / "converted.wav"
    out.write_bytes(data)
    with wave.open(str(out), "rb") as wav:
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 3
        samples = wav.readframes(3)
    assert samples[2:4] == (8191).to_bytes(2, "little", signed=True)
    assert samples[4:6] == (-8191).to_bytes(2, "little", signed=True)


def _make_audio_review_run(tmp_path: Path) -> Path:
    artifact_root = tmp_path / "artifacts"
    run_root = artifact_root / "official-runtime-live"
    run_id = "audio-review-test"
    events_path = run_root / "events" / f"events-{run_id}-01.jsonl"
    realtime_path = run_root / "realtime" / f"realtime-{run_id}-01.jsonl"
    policies_path = run_root / "policies" / f"policies-{run_id}-01.jsonl"
    input_path = run_root / "audio" / f"audio-input-{run_id}-01.wav"
    output_path = run_root / "audio" / f"audio-output-{run_id}-01.wav"
    response_path = run_root / "audio" / f"audio-response-resp-1-{run_id}-01.wav"
    input_meta_path = input_path.with_suffix(".jsonl")
    output_meta_path = output_path.with_suffix(".jsonl")
    response_meta_path = response_path.with_suffix(".jsonl")
    manifest_path = run_root / "runs" / f"run-{run_id}.json"
    markers_path = artifact_root / f"markers-{run_id}.jsonl"

    _write_jsonl(
        events_path,
        [
            {"run_id": run_id, "ts": 100.0, "type": "hf.realtime.input_audio_buffer.speech_started"},
            {"run_id": run_id, "ts": 100.8, "type": "hf.realtime.input_audio_buffer.speech_stopped"},
            {
                "run_id": run_id,
                "ts": 101.0,
                "type": "hf.realtime.conversation.item.input_audio_transcription.completed",
                "role": "user",
                "text": "I need directions.",
                "final": True,
            },
            {"run_id": run_id, "ts": 101.01, "type": "handler.output", "item": {"role": "user", "content": "I need directions."}},
            {"run_id": run_id, "ts": 101.8, "type": "hf.realtime.response.created", "response_id": "resp-1"},
            {"run_id": run_id, "ts": 101.9, "type": "hf.realtime.response.output_audio.delta", "response_id": "resp-1"},
            {"run_id": run_id, "ts": 102.0, "type": "assistant.audio.started", "metadata": {"response_id": "resp-1"}},
            {"run_id": run_id, "ts": 103.0, "type": "hf.realtime.response.output_audio.done", "response_id": "resp-1"},
            {
                "run_id": run_id,
                "ts": 103.1,
                "type": "hf.realtime.response.done",
                "response_id": "resp-1",
                "response": {"id": "resp-1", "status": "completed", "usage": {"output_tokens": 3}},
            },
            {"run_id": run_id, "ts": 103.2, "type": "assistant.audio.done"},
        ],
    )
    _write_jsonl(realtime_path, [{"run_id": run_id, "ts": 99.5, "type": "runtime.milestone"}])
    _write_jsonl(policies_path, [{"run_id": run_id, "ts": 99.8, "type": "conversation_opened"}])
    _write_jsonl(markers_path, [{"run_id": run_id, "n": 1, "ts": 102.4, "note": "speaker sounded quiet"}])
    _write_jsonl(
        input_meta_path,
        [
            {
                "run_id": run_id,
                "ts": 100.0,
                "type": "chunk",
                "sample_start": 0,
                "samples": 320,
                "sample_rate": 16000,
                "rms": 0.1,
            },
            {"run_id": run_id, "ts": 100.02, "type": "stop", "sample_end": 320},
        ],
    )
    _write_jsonl(
        output_meta_path,
        [
            {
                "run_id": run_id,
                "ts": 102.0,
                "type": "chunk",
                "sample_start": 0,
                "samples": 320,
                "sample_rate": 16000,
                "rms": 0.2,
                "response_id": "resp-1",
            },
            {
                "run_id": run_id,
                "ts": 102.001,
                "type": "chunk",
                "sample_start": 320,
                "samples": 320,
                "sample_rate": 16000,
                "rms": 0.2,
                "response_id": "resp-1",
            },
            {"run_id": run_id, "ts": 102.02, "type": "stop", "sample_end": 640},
        ],
    )
    _write_jsonl(
        response_meta_path,
        [
            {
                "run_id": run_id,
                "ts": 102.0,
                "type": "chunk",
                "sample_start": 0,
                "samples": 640,
                "sample_rate": 16000,
                "rms": 0.2,
                "response_id": "resp-1",
            },
            {"run_id": run_id, "ts": 102.04, "type": "stop", "sample_end": 640},
        ],
    )
    input_path.parent.mkdir(parents=True, exist_ok=True)
    _write_pcm16_wav(input_path, [2000] * 320)
    _write_pcm16_wav(output_path, [4000] * 640)
    _write_pcm16_wav(response_path, [4000] * 640)

    manifest = {
        "run_id": run_id,
        "started_ts": 99.0,
        "artifacts": {
            "events": [{"path": str(events_path)}],
            "realtime": [{"path": str(realtime_path)}],
            "policies": [{"path": str(policies_path)}],
            "audio": [
                {
                    "stream": "input",
                    "path": str(input_path),
                    "metadata": str(input_meta_path),
                    "sample_rate": 16000,
                    "status": "closed",
                },
                {
                    "stream": "output",
                    "path": str(output_path),
                    "metadata": str(output_meta_path),
                    "sample_rate": 16000,
                    "status": "closed",
                },
                {
                    "stream": "response-resp-1",
                    "path": str(response_path),
                    "metadata": str(response_meta_path),
                    "sample_rate": 16000,
                    "status": "closed",
                },
            ],
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_root


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _write_pcm16_wav(path: Path, samples: list[int], sample_rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples))
