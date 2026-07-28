"""Privacy-safe local latency traces for Hermes agent and provider hooks."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

TRACE_PATH_ENV = "HERMES_LATENCY_TRACE_PATH"
DEFAULT_TRACE_NAME = "latency-trace.jsonl"
HOOKS = (
    "pre_llm_call",
    "post_llm_call",
    "pre_api_request",
    "post_api_request",
    "api_request_error",
)
IDENTITY_FIELDS = (
    "session_id",
    "task_id",
    "turn_id",
    "api_request_id",
)
RUNTIME_FIELDS = (
    "platform",
    "model",
    "provider",
    "api_mode",
)
INTEGER_FIELDS = (
    "api_call_count",
    "message_count",
    "tool_count",
    "approx_input_tokens",
    "request_char_count",
    "max_tokens",
    "assistant_content_chars",
    "assistant_tool_call_count",
    "status_code",
    "retry_count",
    "max_retries",
)
TEXT_FIELDS = (
    "finish_reason",
    "response_model",
)
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "cached_tokens",
)

_WRITE_LOCK = threading.Lock()


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()


def trace_path() -> Path:
    override = os.getenv(TRACE_PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _hermes_home() / "logs" / DEFAULT_TRACE_NAME


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _safe_usage(raw: Any) -> dict[str, int | float]:
    if not isinstance(raw, dict):
        return {}
    usage: dict[str, int | float] = {}
    for key in USAGE_FIELDS:
        value = _number(raw.get(key))
        if value is not None:
            usage[key] = value
    return usage


def build_record(event: str, fields: dict[str, Any]) -> dict[str, Any]:
    if event not in HOOKS:
        raise ValueError(f"unsupported latency event: {event}")

    record: dict[str, Any] = {
        "schema_version": 1,
        "event": event,
        "observed_at": time.time(),
    }
    for key in (*IDENTITY_FIELDS, *RUNTIME_FIELDS, *TEXT_FIELDS):
        value = fields.get(key)
        if isinstance(value, str) and value:
            record[key] = value
    for key in INTEGER_FIELDS:
        value = _number(fields.get(key))
        if value is not None:
            record[key] = value
    for key in ("started_at", "ended_at", "api_duration"):
        value = _number(fields.get(key))
        if value is not None:
            record[key] = value

    usage = _safe_usage(fields.get("usage"))
    if usage:
        record["usage"] = usage

    error = fields.get("error")
    if event == "api_request_error" and isinstance(error, dict):
        error_type = error.get("type")
        if isinstance(error_type, str) and error_type:
            record["error_type"] = error_type

    return record


def append_record(record: dict[str, Any]) -> None:
    destination = trace_path()
    payload = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")

    with _WRITE_LOCK:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            destination,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)


def _handler(event: str) -> Callable[..., None]:
    def record_hook(**kwargs: Any) -> None:
        try:
            append_record(build_record(event, kwargs))
        except Exception as exc:
            LOGGER.warning(
                "latency trace write failed event=%s error=%s",
                event,
                type(exc).__name__,
            )

    return record_hook


def register(ctx: Any) -> None:
    for hook in HOOKS:
        ctx.register_hook(hook, _handler(hook))
