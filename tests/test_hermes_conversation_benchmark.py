from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


def _load_benchmark() -> ModuleType:
    path = Path("scripts/m1max/benchmark_hermes_conversation.py")
    spec = importlib.util.spec_from_file_location(
        "benchmark_hermes_conversation", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "turns": [
                    {
                        "index": 1,
                        "expected_transcript": "Hello.",
                        "semantic_check": "Respond naturally.",
                    },
                    {
                        "index": 2,
                        "expected_transcript": "My name is Mike.",
                        "semantic_check": "Retain the visitor name.",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_turns_uses_curated_transcripts_and_checks(tmp_path: Path) -> None:
    benchmark = _load_benchmark()

    turns = benchmark._load_turns(_manifest(tmp_path / "manifest.json"))

    assert [turn.prompt for turn in turns] == ["Hello.", "My name is Mike."]
    assert turns[1].semantic_check == "Retain the visitor name."


def test_load_turns_rejects_noncontiguous_indexes(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "turns": [
                    {
                        "index": 2,
                        "expected_transcript": "Hello.",
                        "semantic_check": "Respond naturally.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    try:
        benchmark._load_turns(path)
    except ValueError as exc:
        assert "contiguous and ordered" in str(exc)
    else:
        raise AssertionError("noncontiguous turn indexes should fail")


def test_run_conversation_preserves_one_conversation_and_response_text(
    tmp_path: Path,
) -> None:
    benchmark = _load_benchmark()
    turns = benchmark._load_turns(_manifest(tmp_path / "manifest.json"))
    requests: list[dict] = []

    def post_stream(**kwargs):
        requests.append(kwargs)
        number = len(requests)
        return {
            "response_id": f"resp-{number}",
            "text": f"answer {number}",
            "ttft_ms": 100.0 * number,
            "total_ms": 200.0 * number,
            "input_tokens": 1000,
            "output_tokens": 10,
            "total_tokens": 1010,
            "tool_calls": 0,
        }

    times = iter([10.0, 10.5, 11.0, 11.8])
    samples = benchmark._run_conversation(
        run=2,
        run_id="test-run",
        turns=turns,
        url="http://hermes.test/v1/responses",
        api_key="test-key",
        model="test-model",
        timeout_s=30.0,
        post_stream=post_stream,
        wall_clock=lambda: next(times),
    )

    assert {request["payload"]["conversation"] for request in requests} == {
        "test-run-run-02"
    }
    assert [request["payload"]["input"] for request in requests] == [
        "Hello.",
        "My name is Mike.",
    ]
    assert samples[1].response_text == "answer 2"
    assert samples[1].started_at == 11.0
    assert samples[1].completed_at == 11.8


def test_summaries_include_p90_and_usage_totals(tmp_path: Path) -> None:
    benchmark = _load_benchmark()
    turns = benchmark._load_turns(_manifest(tmp_path / "manifest.json"))

    def post_stream(**kwargs):
        number = 1 if kwargs["payload"]["input"] == "Hello." else 2
        return {
            "response_id": f"resp-{number}",
            "text": f"answer {number}",
            "ttft_ms": 100.0 * number,
            "total_ms": 200.0 * number,
            "input_tokens": 1000,
            "output_tokens": 10,
            "total_tokens": 1010,
            "tool_calls": 0,
        }

    samples = benchmark._run_conversation(
        run=1,
        run_id="test-run",
        turns=turns,
        url="http://hermes.test/v1/responses",
        api_key="test-key",
        model="test-model",
        timeout_s=30.0,
        post_stream=post_stream,
    )
    summary = benchmark._summaries(samples)

    assert summary["ttft_ms"]["p50"] == 150.0
    assert summary["ttft_ms"]["p90"] == 190.0
    assert summary["input_tokens"] == 2000
    assert summary["output_tokens"] == 20
    assert summary["by_run"][0]["turns"] == 2
