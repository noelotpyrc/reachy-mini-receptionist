#!/usr/bin/env python3
"""Human-reviewed, sequential text-only tool checks against isolated S2S staging."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

import websockets

from reachy_mini_brain.official_runtime.agent_profile import (
    compose_hermes_agent_profile,
    with_session_date,
)
from reachy_mini_brain.official_runtime.env import load_project_env
from reachy_mini_brain.official_runtime.events import InMemoryEventSink
from reachy_mini_brain.official_runtime.realtime_tools import (
    RealtimeToolCoordinator,
    ToolExecutionContext,
)
from reachy_mini_brain.official_runtime.reception_tools import (
    build_reception_tool_registry,
    with_reception_tool_instructions,
)


STEPS = [
    (
        1,
        [
            "What are the clinic's opening hours?",
            "Thanks. I'm just relaxing while I wait. How are you?",
        ],
        [],
    ),
    (2, ["What time is it here, and what day of the week is it?"], ["time_now"]),
    (3, ["What time is it in Seoul right now?"], ["time_now"]),
    (
        4,
        [
            "Could you look up the official New Jersey MVC website and tell me where I can renew my driver's license online?"
        ],
        ["web_search"],
    ),
    (
        5,
        [
            "Could you find one recent New Jersey news item and briefly tell me what happened and when it was reported?"
        ],
        ["web_search"],
    ),
    (6, ["Which publication reported that story?"], []),
    (
        7,
        [
            "Please look up the Riverside Science Museum visitor information. What are its standard opening hours, and are reservations required?"
        ],
        ["web_search"],
    ),
]


def long_search_result(query, key, source="web"):
    fact = "The Riverside Science Museum opens Tuesday-Sunday, 10 am-6 pm. Reservations are required for all visitors. "
    excerpt = (fact + "Background exhibition details for visitors. " * 100)[:1500]
    return {
        "success": True,
        "data": {
            "web": [
                {
                    "title": ("Riverside Science Museum visitor information " * 6)[
                        :200
                    ],
                    "url": f"https://museum.example/visiting/{i}",
                    "description": excerpt,
                }
                for i in range(3)
            ]
        },
    }


def check_spoken_format(text):
    # This catches common formats; the integration run still requires human review.
    if re.search(
        r"https?://|\]\(|\b(?:[a-z0-9-]+\.)+[a-z]{2,}/\S+",
        text,
        flags=re.IGNORECASE,
    ):
        raise RuntimeError("Spoken answer contains an explicit URL or link")
    if len(text.split()) > 80:
        raise RuntimeError("Answer exceeded the 80-word review threshold")


def _read_pool(url):
    with urlopen(url, timeout=2) as response:
        return json.loads(response.read(64 * 1024))


async def wait_for_idle_slot(ws_url, timeout=10):
    parts = urlsplit(ws_url)
    pool_url = urlunsplit(
        ("https" if parts.scheme == "wss" else "http", parts.netloc, "/v1/pool", "", "")
    )
    async with asyncio.timeout(timeout):
        while True:
            pool = await asyncio.to_thread(_read_pool, pool_url)
            if any(unit.get("state") == "idle" for unit in pool.get("units", [])):
                return
            await asyncio.sleep(0.1)


class Conversation:
    def __init__(self, profile, output):
        self.profile = profile
        self.output = output
        self.events = InMemoryEventSink()
        self.records = []
        self.coordinator = None
        self.ws = None

    async def send(self, payload):
        if payload.get("type") == "response.create":
            payload.setdefault("response", {})["output_modalities"] = ["text"]
        self.records.append({"ts": time.time(), "direction": "send", "event": payload})
        await self.ws.send(json.dumps(payload))

    async def connect(self, url):
        self.profile = with_session_date(self.profile)
        await wait_for_idle_slot(url)
        self.ws = await websockets.connect(url, max_size=2 * 1024 * 1024)
        await self.ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "instructions": self.profile.instructions,
                        "tools": build_reception_tool_registry().schemas(),
                        "tool_choice": "auto",
                    },
                }
            )
        )
        async with asyncio.timeout(30):
            while True:
                event = json.loads(await self.ws.recv())
                if event["type"] == "error":
                    raise RuntimeError("Backend rejected session initialization")
                if event["type"] == "session.created":
                    session_id = event["session"]["id"]
                    break
        context = ToolExecutionContext(
            self.profile.profile_id,
            session_id,
            self.profile.reference_store,
            self.events,
        )
        self.coordinator = RealtimeToolCoordinator(
            registry=build_reception_tool_registry(), context=context, send=self.send
        )
        return session_id

    async def close(self):
        if self.coordinator:
            await self.coordinator.close()
        if self.ws:
            await self.ws.close()

    async def turn(self, prompt):
        begin = len(self.records)
        event_begin = len(self.events.events)
        start = time.time()
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            }
        )
        await self.send({"type": "response.create", "response": {}})
        final_text = ""
        calls = []
        responses = []
        async with asyncio.timeout(60):
            while True:
                event = json.loads(await self.ws.recv())
                self.records.append(
                    {"ts": time.time(), "direction": "receive", "event": event}
                )
                kind = event.get("type", "")
                if kind == "error" or "audio" in kind:
                    raise RuntimeError(f"Unexpected event in text-only test: {kind}")
                self.coordinator.handle_event(event)
                if kind == "response.output_text.done":
                    final_text = event.get("text", "")
                if kind == "response.done":
                    response = event.get("response", {})
                    responses.append(
                        {"id": response.get("id"), "status": response.get("status")}
                    )
                    if response.get("status") != "completed":
                        raise RuntimeError("A response did not complete")
                    function_calls = [
                        item
                        for item in response.get("output", [])
                        if item.get("type") == "function_call"
                    ]
                    calls.extend(function_calls)
                    if len(calls) > 6:
                        raise RuntimeError("More than six tool calls for one request")
                    if not function_calls:
                        if not final_text:
                            final_text = "".join(
                                part.get("text", "")
                                for item in response.get("output", [])
                                for part in item.get("content", [])
                                if part.get("type") == "output_text"
                            )
                        if not final_text:
                            raise RuntimeError("No final assistant text")
                        break
        current_events = self.events.events[event_begin:]
        results = [
            r["event"]["item"]
            for r in self.records[begin:]
            if r["direction"] == "send"
            and r["event"].get("item", {}).get("type") == "function_call_output"
        ]
        return {
            "prompt": prompt,
            "text": final_text,
            "word_count": len(final_text.split()),
            "started_ts": start,
            "elapsed_ms": round((time.time() - start) * 1000, 3),
            "calls": [
                {
                    "name": c.get("name"),
                    "arguments": c.get("arguments"),
                    "call_id": c.get("call_id"),
                }
                for c in calls
            ],
            "tool_results": results,
            "responses": responses,
            "tool_events": [{"kind": e.kind, "data": e.data} for e in current_events],
        }


async def run(args):
    profile = with_reception_tool_instructions(
        compose_hermes_agent_profile(
            profile_id="reachyclinic",
            source_dir=Path("private/profiles/clinic_receptionist"),
            soul_path=Path("private/profiles/clinic_receptionist/personality.md"),
            session_instructions_path=Path(
                "profiles/clinic_receptionist/session_instructions.txt"
            ),
        )
    )
    reports = []
    conversation = None
    try:
        for step, prompts, expected in STEPS:
            if step < args.start_step:
                continue
            if step != 6:
                if conversation:
                    await conversation.close()
                conversation = Conversation(profile, args.output)
                session_id = await conversation.connect(args.url)
            report = {
                "step": step,
                "session_id": session_id,
                "profile": conversation.profile.provenance(),
                "turns": [],
                "status": "running",
            }
            reports.append(report)
            try:
                for prompt in prompts:
                    print(f"STEP {step} INPUT: {prompt}", flush=True)
                    if step == 7:
                        with patch(
                            "reachy_mini_brain.official_runtime.reception_tools._post_search",
                            long_search_result,
                        ):
                            result = await conversation.turn(prompt)
                    else:
                        result = await conversation.turn(prompt)
                    report["turns"].append(result)
                    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                    actual = [call["name"] for call in result["calls"]]
                    if actual != expected:
                        raise RuntimeError(f"Expected tools {expected}, got {actual}")
                    failures = [
                        e
                        for e in result["tool_events"]
                        if e["kind"]
                        in (
                            "agent.tool.execution_failed",
                            "agent.tool.coordinator_failed",
                        )
                    ]
                    if failures:
                        raise RuntimeError("Tool execution failed")
                    check_spoken_format(result["text"])
                report["status"] = "awaiting_semantic_review"
            except Exception as exc:
                report["status"] = "failed"
                report["error"] = str(exc)
                raise
            finally:
                (args.output / f"step-{step:02}.json").write_text(
                    json.dumps(report, indent=2, ensure_ascii=False) + "\n"
                )
                (args.output / f"step-{step:02}-events.json").write_text(
                    json.dumps(conversation.records, indent=2, ensure_ascii=False)
                    + "\n"
                )
            answer = await asyncio.to_thread(
                input, f"STEP {step} REVIEW: type continue or stop > "
            )
            if answer.strip() != "continue":
                break
            report["status"] = "passed"
            (args.output / f"step-{step:02}.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n"
            )
    finally:
        if conversation:
            await conversation.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:18766/v1/realtime")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--start-step",
        type=int,
        choices=(1, 2, 3, 4, 5, 7),
        default=1,
        help="Resume an independent case; step 6 requires step 5's session.",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    load_project_env()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
