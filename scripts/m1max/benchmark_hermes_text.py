#!/usr/bin/env python3
"""Benchmark streaming text latency through Hermes and direct OpenRouter."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable


DEFAULT_HERMES_URL = "http://127.0.0.1:8643/v1/responses"
DEFAULT_DIRECT_URL = "https://openrouter.ai/api/v1/responses"
DEFAULT_MODEL = "openai/gpt-5.4-mini"


class BenchmarkError(RuntimeError):
    """Raised when a benchmark sample cannot be trusted."""


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str
    expected_groups: tuple[tuple[str, ...], ...]
    seed_prompt: str | None = None
    direct_history: tuple[dict[str, str], ...] = ()


SCENARIOS = (
    Scenario(
        name="clinic_facts",
        prompt="What days and hours is the clinic open?",
        expected_groups=(("monday",), ("friday",), ("9:00",), ("5:00",)),
    ),
    Scenario(
        name="unsupported_action",
        prompt="Please reschedule my appointment to Friday at 3:00 pm.",
        expected_groups=(("can't", "cannot", "unable"), ("appointment",)),
    ),
    Scenario(
        name="continued_recall",
        prompt="What is my name?",
        seed_prompt="My name is Casey Jordan.",
        direct_history=(
            {"role": "user", "content": "My name is Casey Jordan."},
            {"role": "assistant", "content": "Thank you, Casey."},
        ),
        expected_groups=(("casey jordan",),),
    ),
)


@dataclass(frozen=True)
class Sample:
    target: str
    scenario: str
    iteration: int
    response_id: str
    ttft_ms: float
    total_ms: float
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    tool_calls: int
    semantic_ok: bool


def _iter_sse_events(stream: BinaryIO) -> Iterable[dict[str, Any]]:
    data_lines: list[str] = []
    for raw_line in stream:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                data_lines.clear()
                if data != "[DONE]":
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise BenchmarkError("received invalid SSE JSON") from exc
                    if isinstance(event, dict):
                        yield event
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise BenchmarkError("received invalid trailing SSE JSON") from exc
            if isinstance(event, dict):
                yield event


def _usage_from_response(response: dict[str, Any]) -> tuple[int | None, ...]:
    usage = response.get("usage") or {}
    return (
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("total_tokens"),
    )


def _consume_stream(
    stream: BinaryIO,
    *,
    started: float,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    first_text_at: float | None = None
    completed_at: float | None = None
    response_id = ""
    text_parts: list[str] = []
    tool_call_ids: set[str] = set()
    usage: tuple[int | None, ...] = (None, None, None)

    for event in _iter_sse_events(stream):
        event_type = str(event.get("type", ""))
        if event_type == "error" or event_type.endswith(".failed"):
            raise BenchmarkError(f"stream failed with event {event_type!r}")

        response = event.get("response")
        if isinstance(response, dict):
            response_id = str(response.get("id") or response_id)

        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                if first_text_at is None:
                    first_text_at = clock()
                text_parts.append(delta)

        if "function_call" in event_type:
            item_id = str(event.get("item_id") or event.get("call_id") or event_type)
            tool_call_ids.add(item_id)

        if event_type == "response.completed" and isinstance(response, dict):
            completed_at = clock()
            usage = _usage_from_response(response)
            for item in response.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "function_call":
                    tool_call_ids.add(str(item.get("call_id") or item.get("id") or item))

    if first_text_at is None:
        raise BenchmarkError("stream completed without an output text delta")
    if completed_at is None:
        raise BenchmarkError("stream ended without response.completed")
    if not response_id:
        raise BenchmarkError("stream completed without a response ID")

    return {
        "response_id": response_id,
        "text": "".join(text_parts),
        "ttft_ms": (first_text_at - started) * 1000,
        "total_ms": (completed_at - started) * 1000,
        "input_tokens": usage[0],
        "output_tokens": usage[1],
        "total_tokens": usage[2],
        "tool_calls": len(tool_call_ids),
    }


def _request_payload(
    *,
    target: str,
    model: str,
    scenario: Scenario,
    conversation: str,
    instructions: str | None,
) -> dict[str, Any]:
    if target == "hermes":
        input_value: str | list[dict[str, str]] = scenario.prompt
    else:
        input_value = [*scenario.direct_history, {"role": "user", "content": scenario.prompt}]

    payload: dict[str, Any] = {
        "model": model,
        "input": input_value,
        "stream": True,
    }
    if target == "hermes":
        payload["conversation"] = conversation
    elif instructions:
        payload["instructions"] = instructions
    return payload


def _post_stream(
    *,
    url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return _consume_stream(response, started=started)
    except urllib.error.HTTPError as exc:
        body = exc.read(512).decode("utf-8", errors="replace")
        raise BenchmarkError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise BenchmarkError(f"request failed: {exc.reason}") from exc


def _semantic_ok(text: str, expected_groups: tuple[tuple[str, ...], ...]) -> bool:
    normalized = text.casefold().translate(
        {
            ord("\u2018"): "'",
            ord("\u2019"): "'",
            ord("\u02bc"): "'",
        }
    )
    return all(any(option.casefold() in normalized for option in group) for group in expected_groups)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("cannot summarize an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metric_summary(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 1),
        "p50": round(_percentile(values, 0.50), 1),
        "p95": round(_percentile(values, 0.95), 1),
        "max": round(max(values), 1),
    }


def _summaries(samples: list[Sample]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Sample]] = {}
    for sample in samples:
        groups.setdefault((sample.target, sample.scenario), []).append(sample)

    summaries = []
    for (target, scenario), grouped in sorted(groups.items()):
        summaries.append(
            {
                "target": target,
                "scenario": scenario,
                "runs": len(grouped),
                "ttft_ms": _metric_summary([sample.ttft_ms for sample in grouped]),
                "total_ms": _metric_summary([sample.total_ms for sample in grouped]),
                "input_tokens_p50": _percentile(
                    [float(sample.input_tokens) for sample in grouped if sample.input_tokens is not None],
                    0.50,
                ),
                "output_tokens_p50": _percentile(
                    [float(sample.output_tokens) for sample in grouped if sample.output_tokens is not None],
                    0.50,
                ),
                "tool_calls": sum(sample.tool_calls for sample in grouped),
                "semantic_failures": sum(not sample.semantic_ok for sample in grouped),
            }
        )
    return summaries


def _conversation_id(run_id: str, target: str, scenario: str, iteration: int) -> str:
    return f"{run_id}-{target}-{scenario}-{iteration}"


def _progress_number(iteration: int, warmups: int) -> int:
    return iteration + warmups + 1 if iteration < 0 else iteration + 1


def _run_sample(
    *,
    target: str,
    scenario: Scenario,
    iteration: int,
    run_id: str,
    url: str,
    api_key: str,
    model: str,
    instructions: str | None,
    timeout_s: float,
) -> Sample:
    conversation = _conversation_id(run_id, target, scenario.name, iteration)
    if target == "hermes" and scenario.seed_prompt:
        seed_payload = {
            "model": model,
            "input": scenario.seed_prompt,
            "stream": True,
            "conversation": conversation,
        }
        seed = _post_stream(
            url=url,
            api_key=api_key,
            payload=seed_payload,
            timeout_s=timeout_s,
        )
        if seed["tool_calls"]:
            raise BenchmarkError(f"{scenario.name} seed unexpectedly called a tool")

    payload = _request_payload(
        target=target,
        model=model,
        scenario=scenario,
        conversation=conversation,
        instructions=instructions,
    )
    result = _post_stream(
        url=url,
        api_key=api_key,
        payload=payload,
        timeout_s=timeout_s,
    )
    semantic_ok = _semantic_ok(result["text"], scenario.expected_groups)
    sample = Sample(
        target=target,
        scenario=scenario.name,
        iteration=iteration,
        response_id=result["response_id"],
        ttft_ms=round(result["ttft_ms"], 1),
        total_ms=round(result["total_ms"], 1),
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
        total_tokens=result["total_tokens"],
        tool_calls=result["tool_calls"],
        semantic_ok=semantic_ok,
    )
    if sample.tool_calls:
        raise BenchmarkError(f"{target}/{scenario.name} unexpectedly called a tool")
    if not sample.semantic_ok:
        raise BenchmarkError(f"{target}/{scenario.name} failed semantic validation")
    return sample


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("hermes", "direct", "both"), default="hermes")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--hermes-url", default=DEFAULT_HERMES_URL)
    parser.add_argument("--direct-url", default=DEFAULT_DIRECT_URL)
    parser.add_argument("--direct-instructions-file", type=Path)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.runs < 1 or args.warmups < 0:
        parser.error("--runs must be positive and --warmups cannot be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    targets = ("hermes", "direct") if args.target == "both" else (args.target,)
    credentials = {
        "hermes": os.getenv("HERMES_API_KEY") or os.getenv("API_SERVER_KEY"),
        "direct": os.getenv("OPENROUTER_API_KEY"),
    }
    for target in targets:
        if not credentials[target]:
            raise SystemExit(f"missing API key for {target}")

    instructions = None
    instructions_sha256 = None
    if "direct" in targets:
        if args.direct_instructions_file is None:
            raise SystemExit("--direct-instructions-file is required for direct benchmarking")
        instructions_bytes = args.direct_instructions_file.read_bytes()
        instructions = instructions_bytes.decode("utf-8")
        instructions_sha256 = hashlib.sha256(instructions_bytes).hexdigest()

    run_id = f"hermes-text-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    samples: list[Sample] = []
    try:
        for target in targets:
            url = args.hermes_url if target == "hermes" else args.direct_url
            for scenario in SCENARIOS:
                for iteration in range(-args.warmups, args.runs):
                    phase = "warmup" if iteration < 0 else "sample"
                    print(
                        f"[benchmark] {target}/{scenario.name} {phase} "
                        f"{_progress_number(iteration, args.warmups)}",
                        file=sys.stderr,
                    )
                    sample = _run_sample(
                        target=target,
                        scenario=scenario,
                        iteration=iteration,
                        run_id=run_id,
                        url=url,
                        api_key=str(credentials[target]),
                        model=args.model,
                        instructions=instructions if target == "direct" else None,
                        timeout_s=args.timeout,
                    )
                    if iteration >= 0:
                        samples.append(sample)
    except BenchmarkError as exc:
        print(f"[benchmark] failed: {exc}", file=sys.stderr)
        return 1

    report = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "targets": list(targets),
        "runs_per_scenario": args.runs,
        "warmups_per_scenario": args.warmups,
        "direct_instructions_sha256": instructions_sha256,
        "samples": [asdict(sample) for sample in samples],
        "summaries": _summaries(samples),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"[benchmark] report: {args.output}", file=sys.stderr)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
