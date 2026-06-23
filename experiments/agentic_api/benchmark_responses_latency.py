#!/usr/bin/env python3
"""Benchmark text LLM endpoints for the agentic API exploration.

This intentionally uses only the Python standard library so it can run in the
repo venv, on m1max, or in a temporary Hermes install without dependency churn.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SCENARIO = Path(__file__).parent / "scenarios" / "clinic_smoke.json"
TARGET_ORDER = [
    "raw_cold",
    "raw_history",
    "hermes_responses_cold",
    "hermes_responses_history",
    "hermes_conversation",
    "hermes_chat_cold",
    "hermes_chat_history",
]
TARGET_GROUPS = {
    "raw": ["raw_cold"],
    "hermes": ["hermes_conversation"],
    "hermes_stateless": ["hermes_responses_cold"],
    "hermes_chat": ["hermes_chat_cold"],
    "both": ["raw_cold", "hermes_conversation"],
    "all": TARGET_ORDER,
}


@dataclass(frozen=True)
class TargetConfig:
    name: str
    api: str
    base_url: str
    api_key: str
    model: str
    extra_headers: dict[str, str]
    memory_mode: str = "cold"
    store_response: bool = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=sorted(set(TARGET_ORDER) | set(TARGET_GROUPS)),
        default="all",
    )
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup-runs", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", type=Path, default=None, help="Optional JSONL output path.")
    parser.add_argument("--dry-run", action="store_true", help="Print resolved config and first payload only.")
    parser.add_argument(
        "--conversation-prefix",
        default="reachy-agentic-api-bench",
        help="Conversation id prefix for Hermes stateful Responses calls.",
    )
    args = parser.parse_args()

    scenario = _load_scenario(args.scenario)
    targets = _resolve_targets(args.target)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        _print_dry_run(targets, scenario, args.conversation_prefix)
        return 0

    failures = 0
    rows: list[dict[str, Any]] = []
    run_nonce = time.strftime("%Y%m%d-%H%M%S")
    for target in targets:
        for run_index in range(args.warmup_runs + args.runs):
            warmup = run_index < args.warmup_runs
            for scene in scenario["scenes"]:
                history: list[dict[str, str]] = []
                conversation = None
                if target.memory_mode == "conversation":
                    conversation = (
                        f"{args.conversation_prefix}-{args.scenario.stem}-"
                        f"{target.name}-{scene['name']}-{run_nonce}-{run_index}"
                    )
                for turn_index, turn in enumerate(scene["turns"]):
                    include_instructions = _should_send_instructions(target, turn_index)
                    row = _run_one(
                        target=target,
                        scene_name=scene["name"],
                        turn=turn,
                        turn_index=turn_index,
                        run_index=run_index,
                        instructions=scenario["instructions"],
                        timeout=args.timeout,
                        conversation=conversation,
                        history=history,
                        include_instructions=include_instructions,
                        warmup=warmup,
                    )
                    if not row["ok"]:
                        failures += 1
                    rows.append(row)
                    if target.memory_mode == "history" and row["ok"] and row["output_text"]:
                        history.append({"role": "user", "content": turn["input"]})
                        history.append({"role": "assistant", "content": row["output_text"]})
                    if not warmup:
                        _print_row(row)
                        if args.output is not None:
                            with args.output.open("a", encoding="utf-8") as fh:
                                fh.write(json.dumps(row, sort_keys=True) + "\n")

    _print_summary([row for row in rows if not row.get("warmup")])
    return 1 if failures else 0


def _load_scenario(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("instructions"), str):
        raise ValueError(f"{path}: missing string instructions")
    scenes = data.get("scenes")
    if scenes is None:
        turns = data.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"{path}: missing turns")
        scenes = [{"name": "default", "turns": turns}]
    if not isinstance(scenes, list) or not scenes:
        raise ValueError(f"{path}: missing scenes")

    normalized_scenes: list[dict[str, Any]] = []
    for scene in scenes:
        if not isinstance(scene, dict) or not isinstance(scene.get("name"), str):
            raise ValueError(f"{path}: each scene needs a string name")
        turns = scene.get("turns")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"{path}: scene {scene['name']!r} missing turns")
        for turn in turns:
            if not isinstance(turn, dict) or not isinstance(turn.get("name"), str) or not isinstance(turn.get("input"), str):
                raise ValueError(f"{path}: each turn needs name and input")
        normalized_scenes.append({"name": scene["name"], "turns": turns})
    data["scenes"] = normalized_scenes
    return data


def _resolve_targets(target: str) -> list[TargetConfig]:
    names = TARGET_GROUPS.get(target, [target])
    targets: list[TargetConfig] = []
    for name in names:
        targets.append(_resolve_target_name(name))
    return targets


def _resolve_target_name(name: str) -> TargetConfig:
    if name in {"raw_cold", "raw_history"}:
        raw_key = _env_first("RAW_RESPONSES_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")
        return TargetConfig(
            name=name,
            api="responses",
            base_url=os.environ.get("RAW_RESPONSES_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=raw_key,
            model=os.environ.get("RAW_RESPONSES_MODEL", "openai/gpt-5.4-mini"),
            extra_headers={},
            memory_mode="history" if name.endswith("_history") else "cold",
            store_response=False,
        )
    if name == "hermes_conversation":
        return _hermes_target(name=name, api="responses", memory_mode="conversation")
    if name in {"hermes_responses_cold", "hermes_responses_history"}:
        return _hermes_target(
            name=name,
            api="responses",
            memory_mode="history" if name.endswith("_history") else "cold",
        )
    if name in {"hermes_chat_cold", "hermes_chat_history"}:
        return _hermes_target(
            name=name,
            api="chat_completions",
            memory_mode="history" if name.endswith("_history") else "cold",
        )
    raise ValueError(f"unsupported target: {name}")


def _hermes_target(*, name: str, api: str, memory_mode: str) -> TargetConfig:
    hermes_key = _env_first("HERMES_API_KEY", "API_SERVER_KEY")
    session_key = os.environ.get("HERMES_SESSION_KEY")
    headers = {"X-Hermes-Session-Key": session_key} if session_key else {}
    return TargetConfig(
        name=name,
        api=api,
        base_url=os.environ.get("HERMES_BASE_URL", "http://127.0.0.1:8642/v1"),
        api_key=hermes_key,
        model=os.environ.get("HERMES_MODEL", "hermes-agent"),
        extra_headers=headers,
        memory_mode=memory_mode,
    )


def _env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    joined = ", ".join(names)
    raise SystemExit(f"Missing API key env; set one of: {joined}")


def _print_dry_run(targets: list[TargetConfig], scenario: dict[str, Any], conversation_prefix: str) -> None:
    first_scene = scenario["scenes"][0]
    first_turn = first_scene["turns"][0]
    for target in targets:
        conversation = f"{conversation_prefix}-{first_scene['name']}" if target.memory_mode == "conversation" else None
        payload = _payload(
            target=target,
            input_text=first_turn["input"],
            instructions=scenario["instructions"],
            conversation=conversation,
            history=[],
            include_instructions=True,
        )
        print(
            json.dumps(
                {
                    "target": target.name,
                    "api": target.api,
                    "memory_mode": target.memory_mode,
                    "url": _endpoint(target),
                    "model": target.model,
                    "api_key_set": bool(target.api_key),
                    "extra_headers": sorted(target.extra_headers),
                    "payload": payload,
                },
                indent=2,
                sort_keys=True,
            )
        )


def _run_one(
    *,
    target: TargetConfig,
    scene_name: str,
    turn: dict[str, str],
    turn_index: int,
    run_index: int,
    instructions: str,
    timeout: float,
    conversation: str | None,
    history: list[dict[str, str]],
    include_instructions: bool,
    warmup: bool,
) -> dict[str, Any]:
    payload = _payload(
        target=target,
        input_text=turn["input"],
        instructions=instructions,
        conversation=conversation,
        history=history,
        include_instructions=include_instructions,
    )
    request_chars = len(json.dumps(payload, ensure_ascii=False))
    start = time.perf_counter()
    try:
        status, response = _post_json(_endpoint(target), target, payload, timeout=timeout)
        elapsed = time.perf_counter() - start
        output_text = _extract_output_text(response, api=target.api)
        return {
            "ok": 200 <= status < 300,
            "target": target.name,
            "api": target.api,
            "memory_mode": target.memory_mode,
            "scene": scene_name,
            "turn": turn["name"],
            "turn_index": turn_index,
            "run_index": run_index,
            "warmup": warmup,
            "status": status,
            "elapsed_s": round(elapsed, 3),
            "response_id": response.get("id"),
            "conversation": conversation,
            "history_messages": len(history),
            "instructions_sent": include_instructions,
            "input_chars": len(turn["input"]),
            "request_chars": request_chars,
            "output_chars": len(output_text),
            "output_text": output_text,
            "output_preview": output_text[:240],
            "error": _response_error_text(response) if not (200 <= status < 300) else None,
        }
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - start
        return {
            "ok": False,
            "target": target.name,
            "api": target.api,
            "memory_mode": target.memory_mode,
            "scene": scene_name,
            "turn": turn["name"],
            "turn_index": turn_index,
            "run_index": run_index,
            "warmup": warmup,
            "status": None,
            "elapsed_s": round(elapsed, 3),
            "response_id": None,
            "conversation": conversation,
            "history_messages": len(history),
            "instructions_sent": include_instructions,
            "input_chars": len(turn["input"]),
            "request_chars": request_chars,
            "output_chars": 0,
            "output_text": "",
            "output_preview": "",
            "error": repr(exc),
        }


def _payload(
    *,
    target: TargetConfig,
    input_text: str,
    instructions: str,
    conversation: str | None,
    history: list[dict[str, str]],
    include_instructions: bool,
) -> dict[str, Any]:
    if target.api == "chat_completions":
        messages: list[dict[str, str]] = []
        if include_instructions:
            messages.append({"role": "system", "content": instructions})
        messages.extend(history)
        messages.append({"role": "user", "content": input_text})
        return {
            "model": target.model,
            "messages": messages,
            "stream": False,
        }
    if target.api == "responses":
        payload: dict[str, Any] = {
            "model": target.model,
            "store": target.store_response,
        }
        if target.memory_mode == "history" and history:
            payload["input"] = [*history, {"role": "user", "content": input_text}]
        else:
            payload["input"] = input_text
        if include_instructions:
            payload["instructions"] = instructions
        if conversation is not None:
            payload["conversation"] = conversation
        return payload
    raise ValueError(f"unsupported target api: {target.api}")


def _should_send_instructions(target: TargetConfig, turn_index: int) -> bool:
    if target.memory_mode == "conversation":
        return turn_index == 0
    return True


def _endpoint(target: TargetConfig) -> str:
    if target.api == "chat_completions":
        return f"{target.base_url.rstrip('/')}/chat/completions"
    if target.api == "responses":
        return f"{target.base_url.rstrip('/')}/responses"
    raise ValueError(f"unsupported target api: {target.api}")


def _post_json(url: str, target: TargetConfig, payload: dict[str, Any], *, timeout: float) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {target.api_key}",
        "Content-Type": "application/json",
        **target.extra_headers,
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"error": raw}
        return exc.code, parsed


def _extract_output_text(response: dict[str, Any], *, api: str) -> str:
    if api == "chat_completions":
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                return content
        return ""

    direct = response.get("output_text")
    if isinstance(direct, str):
        return direct

    chunks: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks)


def _response_error_text(response: dict[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if isinstance(message, str) and isinstance(code, str):
            return f"{code}: {message}"
        if isinstance(message, str):
            return message
    if isinstance(error, str):
        return error
    return json.dumps(response, sort_keys=True)[:1000]


def _print_row(row: dict[str, Any]) -> None:
    status = "ok" if row["ok"] else "fail"
    print(
        f"{status:4} {row['target']:24} {row['scene']:14} {row['turn']:18} "
        f"{row['elapsed_s']:7.3f}s hist={row['history_messages']:2} "
        f"req={row['request_chars']:5} chars={row['output_chars']:4} {row['output_preview']!r}"
    )
    if row["error"]:
        print(f"      error={row['error']}", file=sys.stderr)


def _print_summary(rows: list[dict[str, Any]]) -> None:
    print("\nsummary")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["target"], []).append(row)
    for target, target_rows in sorted(grouped.items()):
        ok_rows = [row for row in target_rows if row["ok"]]
        latencies = [float(row["elapsed_s"]) for row in ok_rows]
        if not latencies:
            print(f"{target:24} no successful rows")
            continue
        print(
            f"{target:24} n={len(latencies)} "
            f"p50={statistics.median(latencies):.3f}s "
            f"mean={statistics.mean(latencies):.3f}s "
            f"min={min(latencies):.3f}s max={max(latencies):.3f}s"
        )


if __name__ == "__main__":
    raise SystemExit(main())
