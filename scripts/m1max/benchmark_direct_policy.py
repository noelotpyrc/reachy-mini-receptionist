#!/usr/bin/env python3
"""Benchmark the direct OpenRouter lane used for deterministic policy speech."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable


DEFAULT_MODEL = "openai/gpt-5.6-luna"
DEFAULT_URL = "https://openrouter.ai/api/v1/responses"
DEFAULT_INSTRUCTIONS = Path("profiles/clinic_receptionist/instructions.txt")

VOICE_PROMPT_LEAD = """\
You are in a spoken conversation. The user speaks and hears you.
The session prompt defines persona, facts, goals, and tool descriptions. These channel rules only control spoken output and tool-use behavior."""

VOICE_PROMPT_TAIL = """\
## Voice Rules
- Keep replies brief by default: usually one spoken sentence, two if needed. Go longer only when asked.
- Speak naturally. No markdown, bullets, headings, visual formatting, or action/emote text like *laughs*.
- Treat transcripts as noisy. Correct likely mishearings only if asked or meaning depends on it.
- Speech is the default. Use at most one tool when it helps fulfill the request or clearly fits the moment.
- Before a tool call, use a brief natural utterance unless the user asked for silence or tool-only output. For slow information tools, briefly say that you will check.
- For expression/background tools, speak first. If asked to show an expression, use a short pattern like "Sure, here's my best <emotion>." Otherwise use a fitting empathetic sentence. Never mention tools.
- After completed expression/background/physical-action tools, do not add a second spoken comment unless the result has user-facing information.
- Use motion, dance, emotion, and similar tools sparingly when they add empathy, celebration, playfulness, or a requested physical action.
- If unsure whether a tool is needed, just speak."""

SCENARIOS = (
    ("greet", "approach", "Welcome to the clinic, how can I help?"),
    ("goodbye", "depart", "Goodbye! Have a nice day!"),
)


class BenchmarkError(RuntimeError):
    """Raised when one API response is incomplete or unusable."""


def _voice_system_prompt(session_prompt: str) -> str:
    return (
        f"{VOICE_PROMPT_LEAD}\n\n"
        f"Session Prompt:\n{session_prompt.strip()}\n\n"
        f"{VOICE_PROMPT_TAIL}\n"
    )


def _policy_prompt(text: str, reason: str) -> str:
    return (
        f"Reception policy event: {reason}. "
        f"Say exactly this line aloud, without adding extra words: {text}"
    )


def _payload(*, model: str, system_prompt: str, policy_prompt: str) -> dict[str, Any]:
    return {
        "model": model,
        "input": [
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": policy_prompt}],
            },
        ],
        "stream": True,
        "tools": [],
        "tool_choice": "auto",
    }


def _iter_sse(stream: BinaryIO) -> Iterable[dict[str, Any]]:
    data_lines: list[str] = []
    for raw_line in stream:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                raw = "\n".join(data_lines)
                data_lines.clear()
                if raw != "[DONE]":
                    event = json.loads(raw)
                    if isinstance(event, dict):
                        yield event
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


def _consume_stream(stream: BinaryIO, *, started: float) -> dict[str, Any]:
    first_text_at: float | None = None
    completed_at: float | None = None
    text_parts: list[str] = []
    completed_response: dict[str, Any] = {}

    for event in _iter_sse(stream):
        event_type = str(event.get("type") or "")
        if event_type == "error" or event_type.endswith(".failed"):
            raise BenchmarkError(f"stream failed with {event_type}")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                if first_text_at is None:
                    first_text_at = time.perf_counter()
                text_parts.append(delta)
        if event_type == "response.completed":
            response = event.get("response")
            if not isinstance(response, dict):
                raise BenchmarkError("response.completed omitted response")
            completed_response = response
            completed_at = time.perf_counter()

    if first_text_at is None:
        raise BenchmarkError("stream completed without text")
    if completed_at is None:
        raise BenchmarkError("stream ended without response.completed")

    usage = completed_response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    output_details = usage.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}
    return {
        "response_id": str(completed_response.get("id") or ""),
        "resolved_model": completed_response.get("model"),
        "provider": completed_response.get("provider"),
        "text": "".join(text_parts),
        "ttft_ms": round((first_text_at - started) * 1000, 1),
        "total_ms": round((completed_at - started) * 1000, 1),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "reasoning_tokens": output_details.get("reasoning_tokens"),
    }


def _post(*, url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _consume_stream(response, started=started)
    except urllib.error.HTTPError as exc:
        body = exc.read(1000).decode("utf-8", errors="replace")
        raise BenchmarkError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise BenchmarkError(f"request failed: {exc.reason}") from exc


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metric(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 1),
        "p50": round(_percentile(values, 0.50), 1),
        "p90": round(_percentile(values, 0.90), 1),
        "p95": round(_percentile(values, 0.95), 1),
        "max": round(max(values), 1),
    }


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [sample for sample in samples if sample["error"] is None]
    return {
        "samples": len(samples),
        "successful": len(successful),
        "errors": len(samples) - len(successful),
        "exact_matches": sum(bool(sample["exact_match"]) for sample in successful),
        "ttft_ms": _metric([sample["ttft_ms"] for sample in successful]) if successful else None,
        "total_ms": _metric([sample["total_ms"] for sample in successful]) if successful else None,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--instructions-file", type=Path, default=DEFAULT_INSTRUCTIONS)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1 or args.warmups < 0:
        parser.error("--runs must be positive and --warmups cannot be negative")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    return args


def main() -> int:
    args = _parse_args()
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required")

    session_prompt = args.instructions_file.read_text(encoding="utf-8")
    system_prompt = _voice_system_prompt(session_prompt)
    samples: list[dict[str, Any]] = []
    started_at = datetime.now(timezone.utc)

    for scenario, reason, expected in SCENARIOS:
        payload = _payload(
            model=args.model,
            system_prompt=system_prompt,
            policy_prompt=_policy_prompt(expected, reason),
        )
        for iteration in range(-args.warmups, args.runs):
            phase = "warmup" if iteration < 0 else "sample"
            print(
                f"[benchmark] {scenario} {phase} "
                f"{iteration + args.warmups + 1 if iteration < 0 else iteration + 1}",
                flush=True,
            )
            try:
                result = _post(
                    url=args.url,
                    api_key=api_key,
                    payload=payload,
                    timeout=args.timeout,
                )
                error = None
            except BenchmarkError as exc:
                result = {
                    "response_id": "",
                    "resolved_model": None,
                    "provider": None,
                    "text": "",
                    "ttft_ms": None,
                    "total_ms": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "reasoning_tokens": None,
                }
                error = str(exc)
            if iteration >= 0:
                samples.append(
                    {
                        "scenario": scenario,
                        "iteration": iteration + 1,
                        "expected": expected,
                        "exact_match": error is None and result["text"].strip() == expected,
                        "error": error,
                        **result,
                    }
                )

    report = {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "url": args.url,
        "request_shape": {
            "stream": True,
            "stateless": True,
            "tools": [],
            "tool_choice": "auto",
            "reasoning": "omitted (matches current direct policy lane)",
            "provider_routing": "omitted (matches current direct policy lane)",
        },
        "runs_per_scenario": args.runs,
        "warmups_per_scenario": args.warmups,
        "summaries": {
            "all": _summary(samples),
            **{
                scenario: _summary([sample for sample in samples if sample["scenario"] == scenario])
                for scenario, _, _ in SCENARIOS
            },
        },
        "samples": samples,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"[benchmark] report: {args.output}")
    return 0 if report["summaries"]["all"]["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
