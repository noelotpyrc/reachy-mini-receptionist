from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType


def _load_benchmark() -> ModuleType:
    path = Path("scripts/m1max/benchmark_direct_policy.py")
    spec = importlib.util.spec_from_file_location("benchmark_direct_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sse(*events: dict) -> io.BytesIO:
    content = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return io.BytesIO((content + "data: [DONE]\n\n").encode())


def test_policy_payload_matches_direct_lane_shape() -> None:
    benchmark = _load_benchmark()

    payload = benchmark._payload(
        model="test-model",
        system_prompt="system",
        policy_prompt="policy",
    )

    assert payload == {
        "model": "test-model",
        "input": [
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "system"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "policy"}],
            },
        ],
        "stream": True,
        "tools": [],
        "tool_choice": "auto",
    }


def test_policy_prompt_requires_exact_line() -> None:
    benchmark = _load_benchmark()

    assert benchmark._policy_prompt("Goodbye!", "depart") == (
        "Reception policy event: depart. "
        "Say exactly this line aloud, without adding extra words: Goodbye!"
    )


def test_stream_consumer_measures_text_and_usage(monkeypatch) -> None:
    benchmark = _load_benchmark()
    times = iter([10.2, 10.5])
    monkeypatch.setattr(benchmark.time, "perf_counter", lambda: next(times))
    stream = _sse(
        {"type": "response.output_text.delta", "delta": "Goodbye!"},
        {
            "type": "response.completed",
            "response": {
                "id": "resp-1",
                "model": "test-model",
                "provider": "provider-a",
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 3,
                    "total_tokens": 23,
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        },
    )

    result = benchmark._consume_stream(stream, started=10.0)

    assert result == {
        "response_id": "resp-1",
        "resolved_model": "test-model",
        "provider": "provider-a",
        "text": "Goodbye!",
        "ttft_ms": 200.0,
        "total_ms": 500.0,
        "input_tokens": 20,
        "output_tokens": 3,
        "total_tokens": 23,
        "reasoning_tokens": 0,
    }


def test_summary_reports_exact_matches_and_percentiles() -> None:
    benchmark = _load_benchmark()
    samples = [
        {
            "error": None,
            "exact_match": exact,
            "ttft_ms": value,
            "total_ms": value + 100,
        }
        for exact, value in [(True, 100.0), (True, 200.0), (False, 300.0)]
    ]

    summary = benchmark._summary(samples)

    assert summary["successful"] == 3
    assert summary["exact_matches"] == 2
    assert summary["ttft_ms"]["p50"] == 200.0
    assert summary["total_ms"]["max"] == 400.0
