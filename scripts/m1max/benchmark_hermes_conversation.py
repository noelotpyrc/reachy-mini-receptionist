#!/usr/bin/env python3
"""Benchmark a sequential text conversation through the Hermes Responses API."""

from __future__ import annotations

import argparse
import json
import math
import os
import runpy
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_HERMES_URL = "http://127.0.0.1:8643/v1/responses"
DEFAULT_MODEL = "openai/gpt-5.6-luna"


@dataclass(frozen=True)
class Turn:
    index: int
    prompt: str
    semantic_check: str


@dataclass(frozen=True)
class Sample:
    run: int
    turn: int
    conversation: str
    prompt: str
    semantic_check: str
    response_id: str
    response_text: str
    started_at: float
    completed_at: float
    ttft_ms: float
    total_ms: float
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    tool_calls: int


def _load_turns(path: Path) -> list[Turn]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_turns = payload.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ValueError("manifest must contain a nonempty turns list")

    turns: list[Turn] = []
    for raw in raw_turns:
        if not isinstance(raw, dict):
            raise ValueError("manifest turns must be objects")
        index = raw.get("index")
        prompt = raw.get("expected_transcript")
        semantic_check = raw.get("semantic_check")
        if not isinstance(index, int) or index < 1:
            raise ValueError("each turn must have a positive integer index")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"turn {index} has no expected transcript")
        if not isinstance(semantic_check, str) or not semantic_check.strip():
            raise ValueError(f"turn {index} has no semantic check")
        turns.append(
            Turn(
                index=index,
                prompt=prompt.strip(),
                semantic_check=semantic_check.strip(),
            )
        )

    expected_indexes = list(range(1, len(turns) + 1))
    actual_indexes = [turn.index for turn in turns]
    if actual_indexes != expected_indexes:
        raise ValueError(
            f"turn indexes must be contiguous and ordered: expected "
            f"{expected_indexes}, got {actual_indexes}"
        )
    return turns


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metric_summary(values: list[float]) -> dict[str, float]:
    if not values:
        raise ValueError("cannot summarize an empty sample")
    return {
        "min": round(min(values), 1),
        "p50": round(_percentile(values, 0.50), 1),
        "p90": round(_percentile(values, 0.90), 1),
        "p95": round(_percentile(values, 0.95), 1),
        "max": round(max(values), 1),
    }


def _usage_total(samples: list[Sample], field: str) -> int | None:
    values = [getattr(sample, field) for sample in samples]
    known = [value for value in values if isinstance(value, int)]
    return sum(known) if len(known) == len(values) else None


def _summaries(samples: list[Sample]) -> dict[str, Any]:
    if not samples:
        return {}
    by_run: list[dict[str, Any]] = []
    for run in sorted({sample.run for sample in samples}):
        grouped = [sample for sample in samples if sample.run == run]
        by_run.append(
            {
                "run": run,
                "turns": len(grouped),
                "ttft_ms": _metric_summary([sample.ttft_ms for sample in grouped]),
                "total_ms": _metric_summary([sample.total_ms for sample in grouped]),
            }
        )
    return {
        "turns": len(samples),
        "ttft_ms": _metric_summary([sample.ttft_ms for sample in samples]),
        "total_ms": _metric_summary([sample.total_ms for sample in samples]),
        "input_tokens": _usage_total(samples, "input_tokens"),
        "output_tokens": _usage_total(samples, "output_tokens"),
        "total_tokens": _usage_total(samples, "total_tokens"),
        "tool_calls": sum(sample.tool_calls for sample in samples),
        "by_run": by_run,
    }


def _run_conversation(
    *,
    run: int,
    run_id: str,
    turns: list[Turn],
    url: str,
    api_key: str,
    model: str,
    timeout_s: float,
    post_stream: Callable[..., dict[str, Any]],
    wall_clock: Callable[[], float] = time.time,
) -> list[Sample]:
    conversation = f"{run_id}-run-{run:02d}"
    samples: list[Sample] = []
    for turn in turns:
        print(
            f"[benchmark] run {run} turn {turn.index:02d}/{len(turns):02d}",
            file=sys.stderr,
            flush=True,
        )
        started_at = wall_clock()
        result = post_stream(
            url=url,
            api_key=api_key,
            payload={
                "model": model,
                "input": turn.prompt,
                "stream": True,
                "conversation": conversation,
            },
            timeout_s=timeout_s,
        )
        completed_at = wall_clock()
        samples.append(
            Sample(
                run=run,
                turn=turn.index,
                conversation=conversation,
                prompt=turn.prompt,
                semantic_check=turn.semantic_check,
                response_id=str(result["response_id"]),
                response_text=str(result["text"]),
                started_at=started_at,
                completed_at=completed_at,
                ttft_ms=round(float(result["ttft_ms"]), 1),
                total_ms=round(float(result["total_ms"]), 1),
                input_tokens=result.get("input_tokens"),
                output_tokens=result.get("output_tokens"),
                total_tokens=result.get("total_tokens"),
                tool_calls=int(result.get("tool_calls", 0)),
            )
        )
    return samples


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--hermes-url", default=DEFAULT_HERMES_URL)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.runs < 1 or args.warmups < 0:
        parser.error("--runs must be positive and --warmups cannot be negative")
    if args.output.exists():
        parser.error(f"--output already exists: {args.output}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    api_key = os.getenv("HERMES_API_KEY") or os.getenv("API_SERVER_KEY")
    if not api_key:
        raise SystemExit("missing HERMES_API_KEY or API_SERVER_KEY")

    turns = _load_turns(args.manifest)
    helpers = runpy.run_path(
        str(Path(__file__).with_name("benchmark_hermes_text.py"))
    )
    post_stream = helpers["_post_stream"]
    benchmark_started_at = time.time()
    run_id = (
        "hermes-conversation-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    samples: list[Sample] = []
    status = "completed"
    error: str | None = None

    try:
        for warmup in range(1, args.warmups + 1):
            print(
                f"[benchmark] warmup {warmup}/{args.warmups}",
                file=sys.stderr,
                flush=True,
            )
            post_stream(
                url=args.hermes_url,
                api_key=str(api_key),
                payload={
                    "model": args.model,
                    "input": turns[0].prompt,
                    "stream": True,
                    "conversation": f"{run_id}-warmup-{warmup:02d}",
                },
                timeout_s=args.timeout,
            )
        for run in range(1, args.runs + 1):
            samples.extend(
                _run_conversation(
                    run=run,
                    run_id=run_id,
                    turns=turns,
                    url=args.hermes_url,
                    api_key=str(api_key),
                    model=args.model,
                    timeout_s=args.timeout,
                    post_stream=post_stream,
                )
            )
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    report = {
        "schema_version": 1,
        "run_id": run_id,
        "status": status,
        "error": error,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_started_at": benchmark_started_at,
        "benchmark_completed_at": time.time(),
        "model": args.model,
        "hermes_url": args.hermes_url,
        "manifest": str(args.manifest),
        "runs_requested": args.runs,
        "warmups": args.warmups,
        "turns_per_run": len(turns),
        "samples": [asdict(sample) for sample in samples],
        "summary": _summaries(samples),
    }
    _write_report(args.output, report)
    print(f"[benchmark] report: {args.output}", file=sys.stderr)
    if status != "completed":
        print(f"[benchmark] failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
