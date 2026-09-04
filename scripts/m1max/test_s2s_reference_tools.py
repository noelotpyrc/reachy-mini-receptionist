#!/usr/bin/env python3
"""Exercise the client-owned read-only reference loop against S2S staging."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reachy_mini_brain.official_runtime.agent_profile import compose_agent_profile
from reachy_mini_brain.official_runtime.events import InMemoryEventSink
from reachy_mini_brain.official_runtime.realtime_tools import (
    ToolExecutionContext,
    build_reference_tool_registry,
)
from reachy_mini_brain.official_runtime.s2s_realtime import S2SRealtimeHandler


PROMPT = (
    "Use the approved reference tools to answer this question: Where is overflow "
    "parking, and what must visitors obtain?"
)
EXPECTED_TOOL_ORDER = ["reference_catalog", "reference_read"]


class ReferenceToolTestError(RuntimeError):
    """Raised when the integrated reference loop violates its contract."""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:18766/v1/realtime")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path("tests/fixtures/agent_profiles/reference_tool_test"),
    )
    parser.add_argument("--profile-id", default="reference-tool-test")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    return args


def _events_of_kind(events: InMemoryEventSink, kind: str) -> list[Any]:
    return [event for event in events.events if event.kind == kind]


def _final_transcript(events: InMemoryEventSink) -> str:
    completed = _events_of_kind(
        events,
        "hf.realtime.response.output_audio_transcript.done",
    )
    for event in reversed(completed):
        text = event.data.get("transcript") or event.data.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


async def _wait_for_completion(
    events: InMemoryEventSink,
    *,
    timeout: float,
) -> None:
    async with asyncio.timeout(timeout):
        while True:
            transcript = _final_transcript(events)
            tools = [
                str(event.data.get("tool_name"))
                for event in _events_of_kind(events, "agent.tool.execution_completed")
            ]
            done = _events_of_kind(events, "hf.realtime.response.done")
            if transcript and tools == EXPECTED_TOOL_ORDER and len(done) >= 3:
                return
            failed = _events_of_kind(events, "agent.tool.execution_failed")
            coordinator_failed = _events_of_kind(
                events,
                "agent.tool.coordinator_failed",
            )
            realtime_errors = _events_of_kind(events, "hf.realtime.error")
            if failed or coordinator_failed or realtime_errors:
                raise ReferenceToolTestError(
                    "tool or realtime failure observed before completion"
                )
            await asyncio.sleep(0.01)


def _validate(events: InMemoryEventSink) -> dict[str, Any]:
    tool_order = [
        str(event.data.get("tool_name"))
        for event in _events_of_kind(events, "agent.tool.execution_completed")
    ]
    if tool_order != EXPECTED_TOOL_ORDER:
        raise ReferenceToolTestError(
            f"expected tool order {EXPECTED_TOOL_ORDER!r}, got {tool_order!r}"
        )

    submitted = _events_of_kind(events, "agent.tool.result_submitted")
    follow_ups = _events_of_kind(events, "agent.tool.follow_up_requested")
    if len(submitted) != 2 or len(follow_ups) != 2:
        raise ReferenceToolTestError(
            "each tool result must be submitted and followed by one response"
        )
    if _events_of_kind(events, "agent.tool.execution_failed"):
        raise ReferenceToolTestError("a reference tool execution failed")

    transcript = _final_transcript(events)
    normalized = transcript.casefold()
    missing = [
        concept
        for concept, alternatives in (
            ("East Lot C", ("east lot c",)),
            ("parking permit", ("parking permit", "permit")),
            ("reception", ("reception", "front desk")),
        )
        if not any(alternative in normalized for alternative in alternatives)
    ]
    if missing:
        raise ReferenceToolTestError(
            f"final response omitted {missing!r}: {transcript!r}"
        )

    response_done = _events_of_kind(events, "hf.realtime.response.done")
    statuses = [event.data.get("response_status") for event in response_done]
    if statuses != ["completed", "completed", "completed"]:
        raise ReferenceToolTestError(
            f"expected three completed responses, got {statuses!r}"
        )
    return {
        "tool_order": tool_order,
        "tool_results_submitted": len(submitted),
        "follow_up_responses": len(follow_ups),
        "response_statuses": statuses,
        "final_text": transcript,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    profile = compose_agent_profile(
        profile_id=args.profile_id,
        public_dir=args.profile_dir,
    )
    events = InMemoryEventSink()
    registry = build_reference_tool_registry(profile.reference_store)
    context = ToolExecutionContext(
        profile_id=profile.profile_id,
        visitor_session_id="pre-session",
        reference_store=profile.reference_store,
        event_sink=events,
    )
    handler = S2SRealtimeHandler(
        realtime_ws_url=args.url,
        instructions=profile.instructions,
        instructions_source=f"profile:{profile.profile_id}",
        instructions_sha256=profile.sha256,
        event_sink=events,
        tool_registry=registry,
        tool_context=context,
    )

    started = time.perf_counter()
    try:
        await handler.start_up()
        await handler.begin_conversation_session()
        if not await handler.request_text_response(PROMPT):
            raise ReferenceToolTestError("handler rejected the text request")
        await _wait_for_completion(events, timeout=args.timeout)
        result = _validate(events)
    finally:
        await handler.shutdown()

    return {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "profile": profile.provenance(),
        "prompt": PROMPT,
        "registered_tools": registry.names(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "result": result,
    }


def main() -> int:
    args = _parse_args()
    try:
        report = asyncio.run(_run(args))
    except (ReferenceToolTestError, TimeoutError) as exc:
        raise SystemExit(f"reference tool test failed: {exc}") from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["result"], indent=2, sort_keys=True))
    print(f"report -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
