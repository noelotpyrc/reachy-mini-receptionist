#!/usr/bin/env python3
"""Run a two-turn, text-only profile check against an S2S Realtime server."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

from reachy_mini_brain.official_runtime.agent_profile import compose_agent_profile


TURNS = (
    {
        "input": (
            "Please remember that my name is Morgan for this conversation. "
            "What are the clinic's weekday hours?"
        ),
        "required_all": (("monday",), ("friday",), ("9",), ("5",)),
    },
    {
        "input": "What name did I ask you to use, and what floor is the clinic on?",
        "required_all": (("morgan",), ("second", "2nd")),
    },
)


class TextProfileTestError(RuntimeError):
    """Raised when the staging server violates the text-turn contract."""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8766/v1/realtime")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path("profiles/clinic_receptionist"),
    )
    parser.add_argument("--profile-id", default="lakeside-test")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    return args


def _session_update(instructions: str) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "tools": [],
            "tool_choice": "auto",
        },
    }


async def _wait_for_session(ws: Any, timeout: float) -> dict[str, Any]:
    async with asyncio.timeout(timeout):
        while True:
            event = json.loads(await ws.recv())
            if event.get("type") == "session.created":
                return event
            if event.get("type") == "error":
                raise TextProfileTestError(f"session error: {event.get('error')!r}")


def _response_id(event: dict[str, Any]) -> str | None:
    response_id = event.get("response_id")
    if isinstance(response_id, str):
        return response_id
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        return response["id"]
    return None


async def _run_turn(ws: Any, prompt: str, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    await ws.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            }
        )
    )
    await ws.send(
        json.dumps(
            {
                "type": "response.create",
                "response": {"output_modalities": ["text"]},
            }
        )
    )

    response_id: str | None = None
    deltas: list[str] = []
    final_text: str | None = None
    event_types: list[str] = []
    status: str | None = None
    async with asyncio.timeout(timeout):
        while True:
            event = json.loads(await ws.recv())
            event_type = str(event.get("type") or "unknown")
            event_types.append(event_type)
            if event_type == "error":
                raise TextProfileTestError(f"response error: {event.get('error')!r}")
            event_response_id = _response_id(event)
            if event_type == "response.created":
                response_id = event_response_id
            elif response_id is not None and event_response_id not in {None, response_id}:
                continue
            elif event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    deltas.append(delta)
            elif event_type == "response.output_text.done":
                text = event.get("text")
                if isinstance(text, str):
                    final_text = text
            elif event_type == "response.done":
                response = event.get("response")
                if isinstance(response, dict):
                    status = str(response.get("status"))
                break

    text = final_text if final_text is not None else "".join(deltas)
    if status != "completed":
        raise TextProfileTestError(f"response status was {status!r}")
    if not text.strip():
        raise TextProfileTestError("response completed without text")
    audio_events = [event_type for event_type in event_types if "audio" in event_type]
    if audio_events:
        raise TextProfileTestError(f"text-only response emitted audio events: {audio_events}")
    return {
        "response_id": response_id,
        "status": status,
        "text": text,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "event_types": event_types,
    }


def _check_response(text: str, required_all: tuple[tuple[str, ...], ...]) -> None:
    normalized = text.casefold()
    missing = [options for options in required_all if not any(option in normalized for option in options)]
    if missing:
        raise TextProfileTestError(
            f"response omitted required concepts {missing!r}: {text!r}"
        )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    profile = compose_agent_profile(
        profile_id=args.profile_id,
        public_dir=args.profile_dir,
    )
    turns: list[dict[str, Any]] = []
    async with websockets.connect(args.url, max_size=None) as ws:
        await ws.send(json.dumps(_session_update(profile.instructions)))
        session_event = await _wait_for_session(ws, args.timeout)
        for index, scenario in enumerate(TURNS, start=1):
            print(f"[s2s-text-profile] turn {index}: {scenario['input']}", flush=True)
            result = await _run_turn(ws, str(scenario["input"]), args.timeout)
            _check_response(result["text"], scenario["required_all"])
            print(f"[s2s-text-profile] response {index}: {result['text']}", flush=True)
            turns.append({"turn": index, "input": scenario["input"], **result})

    return {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "url": args.url,
        "session_id": session_event.get("session", {}).get("id"),
        "profile": profile.provenance(),
        "checks": {
            "profile_facts": "passed",
            "conversation_history": "passed",
            "text_only_output": "passed",
        },
        "turns": turns,
    }


def main() -> int:
    args = _parse_args()
    try:
        report = asyncio.run(_run(args))
    except (TextProfileTestError, TimeoutError) as exc:
        raise SystemExit(f"text profile test failed: {exc}") from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"report -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
