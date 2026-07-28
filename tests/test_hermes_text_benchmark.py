from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType


def _load_benchmark() -> ModuleType:
    path = Path("scripts/m1max/benchmark_hermes_text.py")
    spec = importlib.util.spec_from_file_location("benchmark_hermes_text", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sse(*events: dict) -> io.BytesIO:
    content = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return io.BytesIO((content + "data: [DONE]\n\n").encode("utf-8"))


def test_consume_stream_measures_first_text_and_completion() -> None:
    benchmark = _load_benchmark()
    stream = _sse(
        {"type": "response.created", "response": {"id": "resp-test"}},
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.output_text.delta", "delta": " there"},
        {
            "type": "response.completed",
            "response": {
                "id": "resp-test",
                "output": [],
                "usage": {"input_tokens": 100, "output_tokens": 2, "total_tokens": 102},
            },
        },
    )
    times = iter([10.25, 10.75])

    result = benchmark._consume_stream(stream, started=10.0, clock=lambda: next(times))

    assert result == {
        "response_id": "resp-test",
        "text": "Hello there",
        "ttft_ms": 250.0,
        "total_ms": 750.0,
        "input_tokens": 100,
        "output_tokens": 2,
        "total_tokens": 102,
        "tool_calls": 0,
    }


def test_consume_stream_rejects_missing_text_delta() -> None:
    benchmark = _load_benchmark()
    stream = _sse(
        {"type": "response.created", "response": {"id": "resp-test"}},
        {"type": "response.completed", "response": {"id": "resp-test", "output": []}},
    )

    try:
        benchmark._consume_stream(stream, started=1.0, clock=lambda: 2.0)
    except benchmark.BenchmarkError as exc:
        assert "without an output text delta" in str(exc)
    else:
        raise AssertionError("missing text delta should fail the sample")


def test_request_payload_separates_hermes_conversation_and_direct_history() -> None:
    benchmark = _load_benchmark()
    scenario = benchmark.SCENARIOS[2]

    hermes = benchmark._request_payload(
        target="hermes",
        model="test-model",
        scenario=scenario,
        conversation="conversation-test",
        instructions=None,
    )
    direct = benchmark._request_payload(
        target="direct",
        model="test-model",
        scenario=scenario,
        conversation="unused",
        instructions="Clinic context",
    )

    assert hermes["input"] == "What is my name?"
    assert hermes["conversation"] == "conversation-test"
    assert "instructions" not in hermes
    assert direct["input"][-1] == {"role": "user", "content": "What is my name?"}
    assert direct["input"][0]["content"] == "My name is Casey Jordan."
    assert direct["instructions"] == "Clinic context"
    assert "conversation" not in direct


def test_summaries_report_interpolated_latency_percentiles() -> None:
    benchmark = _load_benchmark()
    samples = [
        benchmark.Sample(
            target="hermes",
            scenario="clinic_facts",
            iteration=index,
            response_id=f"resp-{index}",
            ttft_ms=value,
            total_ms=value + 100,
            input_tokens=2000,
            output_tokens=20,
            total_tokens=2020,
            tool_calls=0,
            semantic_ok=True,
        )
        for index, value in enumerate([100.0, 200.0, 300.0, 400.0])
    ]

    summary = benchmark._summaries(samples)[0]

    assert summary["ttft_ms"] == {"min": 100.0, "p50": 250.0, "p95": 385.0, "max": 400.0}
    assert summary["total_ms"]["p50"] == 350.0
    assert summary["input_tokens_p50"] == 2000.0
    assert summary["tool_calls"] == 0
    assert summary["semantic_failures"] == 0


def test_semantic_validation_requires_every_expected_fact_group() -> None:
    benchmark = _load_benchmark()
    groups = benchmark.SCENARIOS[0].expected_groups

    assert benchmark._semantic_ok(
        "Open Monday through Friday from 9:00 am to 5:00 pm.", groups
    )
    assert not benchmark._semantic_ok("Open Monday through Friday.", groups)


def test_semantic_validation_normalizes_typographic_apostrophes() -> None:
    benchmark = _load_benchmark()
    groups = benchmark.SCENARIOS[1].expected_groups

    assert benchmark._semantic_ok(
        "I can\u2019t reschedule appointments, but front-desk staff can help.", groups
    )


def test_progress_numbers_restart_after_warmups() -> None:
    benchmark = _load_benchmark()

    assert [benchmark._progress_number(index, 2) for index in (-2, -1, 0, 1)] == [
        1,
        2,
        1,
        2,
    ]
