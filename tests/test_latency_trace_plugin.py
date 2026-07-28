from __future__ import annotations

import importlib.util
import json
import stat
from pathlib import Path
from types import ModuleType


def _load_plugin() -> ModuleType:
    path = Path("hermes_plugins/latency_trace/__init__.py")
    spec = importlib.util.spec_from_file_location("latency_trace_plugin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_latency_trace_records_allowlisted_metadata_only(
    monkeypatch, tmp_path: Path
) -> None:
    plugin = _load_plugin()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registrations: dict[str, object] = {}

    class Context:
        def register_hook(self, name, callback):
            registrations[name] = callback

    plugin.register(Context())
    callback = registrations["post_api_request"]
    callback(
        session_id="session-1",
        task_id="task-1",
        turn_id="turn-1",
        api_request_id="request-1",
        platform="api_server",
        model="openai/gpt-5.4-mini",
        provider="openrouter",
        api_mode="chat_completions",
        api_call_count=1,
        api_duration=2.5,
        started_at=100.0,
        ended_at=102.5,
        finish_reason="stop",
        message_count=4,
        assistant_content_chars=42,
        assistant_tool_call_count=0,
        usage={
            "input_tokens": 123,
            "output_tokens": 45,
            "total_tokens": 168,
            "raw_usage": "must not be recorded",
        },
        user_message="private visitor text",
        conversation_history=[{"content": "private history"}],
        request={"headers": {"Authorization": "secret"}},
        response={"output": "private assistant text"},
        assistant_message={"content": "private assistant text"},
    )

    destination = tmp_path / "logs" / "latency-trace.jsonl"
    record = json.loads(destination.read_text(encoding="utf-8"))
    assert record["event"] == "post_api_request"
    assert record["session_id"] == "session-1"
    assert record["api_duration"] == 2.5
    assert record["usage"] == {
        "input_tokens": 123,
        "output_tokens": 45,
        "total_tokens": 168,
    }
    serialized = json.dumps(record)
    assert "private" not in serialized
    assert "secret" not in serialized
    assert "raw_usage" not in serialized
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_latency_trace_error_omits_error_message(
    monkeypatch, tmp_path: Path
) -> None:
    plugin = _load_plugin()
    destination = tmp_path / "trace.jsonl"
    monkeypatch.setenv(plugin.TRACE_PATH_ENV, str(destination))

    plugin.append_record(
        plugin.build_record(
            "api_request_error",
            {
                "api_request_id": "request-2",
                "status_code": 500,
                "error": {
                    "type": "ProviderError",
                    "message": "private provider response",
                },
            },
        )
    )

    record = json.loads(destination.read_text(encoding="utf-8"))
    assert record["error_type"] == "ProviderError"
    assert record["status_code"] == 500
    assert "message" not in record


def test_latency_trace_registers_expected_hooks() -> None:
    plugin = _load_plugin()
    registrations: list[str] = []

    class Context:
        def register_hook(self, name, callback):
            assert callable(callback)
            registrations.append(name)

    plugin.register(Context())

    assert registrations == list(plugin.HOOKS)
