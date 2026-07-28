#!/usr/bin/env python3
"""Measure Hermes text latency for the final transcript from audio replay turn 4."""

from __future__ import annotations

import argparse
import json
import math
import os
import runpy
import sys
import uuid
from pathlib import Path
from typing import Any


TURN_PROMPTS = (
    "Hey, nice to meet you.",
    "So uh my name is Mike. I'm here for appointment.",
    "Too starty.",
    "Uh I think I'm late for the tournament.",
)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 1),
        "p50": round(percentile(values, 0.50), 1),
        "p90": round(percentile(values, 0.90), 1),
        "p95": round(percentile(values, 0.95), 1),
        "max": round(max(values), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")

    benchmark = runpy.run_path(
        str(Path(__file__).with_name("benchmark_hermes_text.py"))
    )
    post_stream = benchmark["_post_stream"]
    samples: list[dict[str, Any]] = []

    for iteration in range(1, args.iterations + 1):
        conversation = f"isolated-turn4-{uuid.uuid4().hex}"
        final_result: dict[str, Any] | None = None
        for turn, prompt in enumerate(TURN_PROMPTS, start=1):
            result = post_stream(
                url="http://127.0.0.1:8643/v1/responses",
                api_key=os.environ["API_SERVER_KEY"],
                payload={
                    "model": "wrapper-routed",
                    "input": prompt,
                    "stream": True,
                    "conversation": conversation,
                },
                timeout_s=120.0,
            )
            if turn == len(TURN_PROMPTS):
                final_result = result

        assert final_result is not None
        sample = {
            "iteration": iteration,
            "conversation": conversation,
            **final_result,
        }
        samples.append(sample)
        print(
            f"sample {iteration}/{args.iterations}: "
            f"ttft={final_result['ttft_ms']:.1f} ms, "
            f"total={final_result['total_ms']:.1f} ms",
            file=sys.stderr,
            flush=True,
        )

    print(
        json.dumps(
            {
                "iterations": args.iterations,
                "ttft_ms": summarize([sample["ttft_ms"] for sample in samples]),
                "total_ms": summarize([sample["total_ms"] for sample in samples]),
                "samples": samples,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
