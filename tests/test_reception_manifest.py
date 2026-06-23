import json
import threading

import numpy as np

from reachy_mini_brain import reception


def _manifest(tmp_path, run_id):
    return json.loads((tmp_path / "runs" / f"run-{run_id}.json").read_text())


def test_pydantic_manifest_records_resolved_openrouter_model(tmp_path, monkeypatch):
    monkeypatch.setattr(reception, "ARTIFACTS", tmp_path)

    reception.ReceptionDaemon(
        reception.MockSession(),
        brain=True,
        brain_backend="pydantic",
        brain_model="sonnet",
        run_id="pydantic-test",
    )

    config = _manifest(tmp_path, "pydantic-test")["config"]
    assert config["brain_backend"] == "pydantic"
    assert config["brain_model"] == "openai/gpt-oss-20b"
    assert config["brain_model_requested"] == "sonnet"


def test_claude_manifest_records_requested_model(tmp_path, monkeypatch):
    monkeypatch.setattr(reception, "ARTIFACTS", tmp_path)

    reception.ReceptionDaemon(
        reception.MockSession(),
        brain=True,
        brain_backend="claude",
        brain_model="haiku",
        run_id="claude-test",
    )

    config = _manifest(tmp_path, "claude-test")["config"]
    assert config["brain_backend"] == "claude"
    assert config["brain_model"] == "haiku"
    assert config["brain_model_requested"] == "haiku"


class _QueuedTranscriptSession:
    def __init__(self, items):
        self.items = list(items)

    def listen_read(self, timeout: float = 0.0):
        if self.items:
            return self.items.pop(0)
        return {"text": "", "buffer_duration": 0.0}


class _BlockingOnlyTranscriptSession:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def listen_read(self, timeout: float = 0.0):
        self.calls.append(timeout)
        if timeout > 0 and self.items:
            return self.items.pop(0)
        return {"text": "", "buffer_duration": 0.0}


def test_transcript_batch_is_drained_and_ordered(tmp_path, monkeypatch):
    monkeypatch.setattr(reception, "ARTIFACTS", tmp_path)
    daemon = reception.ReceptionDaemon(
        _QueuedTranscriptSession([
            {"utterance_id": 1, "speech_start_ts": 10.0, "text": "first"},
        ]),
        run_id="batch-test",
    )

    batch = daemon._drain_transcript_batch(
        {"utterance_id": 2, "speech_start_ts": 20.0, "text": "second"}
    )

    assert [item["utterance_id"] for item in batch] == [1, 2]
    brain_input = daemon._format_brain_input(batch)
    assert brain_input.index("first") < brain_input.index("second")


def test_batch_wait_is_skipped_without_known_pending_utterance(tmp_path, monkeypatch):
    monkeypatch.setattr(reception, "ARTIFACTS", tmp_path)
    session = _BlockingOnlyTranscriptSession([
        {"utterance_id": 2, "speech_start_ts": 20.0, "text": "second"},
    ])
    daemon = reception.ReceptionDaemon(session, run_id="no-pending-batch-test")

    batch, meta = daemon._collect_transcript_batch(
        {"utterance_id": 1, "speech_start_ts": 10.0, "text": "first"},
        {},
        stop=threading.Event(),
        drain_activity=lambda: None,
    )

    assert [item["utterance_id"] for item in batch] == [1]
    assert meta["batch_wait_s"] == 0.0
    assert all(timeout == 0.0 for timeout in session.calls)


def test_batch_wait_collects_known_pending_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(reception, "ARTIFACTS", tmp_path)
    session = _BlockingOnlyTranscriptSession([
        {"utterance_id": 2, "speech_start_ts": 20.0, "text": "second"},
    ])
    daemon = reception.ReceptionDaemon(
        session, run_id="pending-batch-test", batch_max_wait=1.0,
    )
    pending = {2: {"type": "utterance_queued", "utterance_id": 2}}

    batch, meta = daemon._collect_transcript_batch(
        {"utterance_id": 1, "speech_start_ts": 10.0, "text": "first"},
        pending,
        stop=threading.Event(),
        drain_activity=lambda: None,
    )

    assert [item["utterance_id"] for item in batch] == [1, 2]
    assert pending == {}
    assert not meta["batch_wait_timeout"]
    assert any(timeout > 0 for timeout in session.calls)


def test_batch_wait_timeout_releases_missing_pending_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(reception, "ARTIFACTS", tmp_path)
    daemon = reception.ReceptionDaemon(
        _BlockingOnlyTranscriptSession([]),
        run_id="pending-timeout-test",
        batch_max_wait=0.0,
    )
    pending = {2: {"type": "utterance_queued", "utterance_id": 2}}

    batch, meta = daemon._collect_transcript_batch(
        {"utterance_id": 1, "speech_start_ts": 10.0, "text": "first"},
        pending,
        stop=threading.Event(),
        drain_activity=lambda: None,
    )

    assert [item["utterance_id"] for item in batch] == [1]
    assert pending == {}
    assert meta["batch_wait_timeout"]
    assert meta["batch_timed_out_utterance_ids"] == [2]


def test_brain_received_metadata_is_added_per_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(reception, "ARTIFACTS", tmp_path)
    daemon = reception.ReceptionDaemon(reception.MockSession(), run_id="brain-ts-test")
    transcripts = [{
        "utterance_id": 4,
        "speech_end_ts": 100.0,
        "stt_done_ts": 103.5,
        "text": "hello",
    }]

    daemon._add_brain_received_metadata(transcripts, 105.25)

    assert transcripts[0]["brain_received_ts"] == 105.25
    assert transcripts[0]["brain_received_after_speech_end_s"] == 5.25
    assert transcripts[0]["brain_received_after_stt_done_s"] == 1.75


def test_transcript_and_utterance_artifacts_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(reception, "ARTIFACTS", tmp_path)
    daemon = reception.ReceptionDaemon(
        reception.MockSession(), save_turns=True, run_id="artifact-test",
    )

    daemon._save_transcript_artifacts({
        "utterance_id": 7,
        "speech_start_ts": 100.0,
        "speech_end_ts": 101.0,
        "queued_ts": 101.1,
        "stt_start_ts": 101.2,
        "stt_done_ts": 101.5,
        "stt_latency": 0.3,
        "model": "medium",
        "language": "en",
        "text": "hello",
        "buffer_duration": 1.0,
        "audio": np.zeros(16000, dtype=np.float32),
    })

    transcript = json.loads(
        (tmp_path / "transcripts" / "transcripts-artifact-test.jsonl").read_text()
    )
    assert transcript["utterance_id"] == 7
    assert transcript["text"] == "hello"
    assert "audio" not in transcript
    assert transcript["utterance_wav"].endswith("utterance-artifact-test-001.wav")
    assert (tmp_path / "utterances" / "utterance-artifact-test-001.wav").exists()
