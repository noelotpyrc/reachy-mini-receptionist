import asyncio
import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from reachy_mini_brain.official_runtime import InMemoryEventSink
from reachy_mini_brain.official_runtime.agent_profile import (
    ReferenceStore,
    compose_agent_profile,
)
from reachy_mini_brain.official_runtime.live_app import cli
from reachy_mini_brain.official_runtime.realtime_tools import (
    ToolCallMetadata,
    ToolError,
    ToolExecutionContext,
)
from reachy_mini_brain.official_runtime import reception_tools as tools


@pytest.mark.parametrize("month,offset", [(1, "-05:00"), (7, "-04:00")])
def test_clock_uses_clinic_timezone_and_dst(monkeypatch, month, offset):
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, month, 4, 16, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(tools, "datetime", Clock)
    result = tools.current_time()
    assert result["timezone"] == "America/New_York"
    assert result["local_datetime"].endswith(offset)
    assert result["utc_datetime"] == f"2026-{month:02}-04T16:30:00+00:00"
    assert tools.current_time("Asia/Seoul")["local_datetime"].endswith("+09:00")


def test_clock_rejects_invalid_timezone():
    with pytest.raises(ToolError, match="valid IANA"):
        tools.current_time("not-a-timezone")


def _payload(count=1):
    return {
        "success": True,
        "data": {
            "web": [
                {
                    "title": f"Source {i}",
                    "url": f"https://example.com/{i}",
                    "description": "Useful information.",
                    "markdown": "FULL PAGE OMITTED",
                }
                for i in range(count)
            ]
        },
    }


def test_search_bounds_and_labels_excerpts():
    payload = _payload(5)
    payload["data"]["web"][0]["description"] = "x" * 5000
    payload["data"]["web"][0]["title"] = "t" * 500
    result = tools._bounded_results(payload)
    assert len(result["results"]) == 3
    assert len(result["results"][0]["excerpt"]) == 1500
    assert len(result["results"][0]["title"]) == 200
    assert result["results"][0]["truncated"] is True
    assert result["omitted_results"] == 2
    assert result["truncated"] is True
    assert "FULL PAGE OMITTED" not in json.dumps(result)
    assert result["results"][0]["content_type"] == "search_description"


def test_bad_urls_and_malformed_rows_are_omitted():
    payload = _payload(1)
    payload["data"]["web"].extend(
        [
            None,
            {"url": "https://example.com"},
            {"title": "x", "url": "javascript:alert(1)", "description": "x"},
            {
                "title": "x",
                "url": "https://name:secret@example.com",
                "description": "x",
            },
        ]
    )
    result = tools._bounded_results(payload)
    assert len(result["results"]) == 1
    assert result["omitted_results"] == 4
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "payload", [None, [], {"success": False}, {"success": True, "data": {}}]
)
def test_invalid_search_shape_is_a_bounded_failure(payload):
    with pytest.raises(ToolError):
        tools._bounded_results(payload)


def test_empty_results_are_not_an_error():
    assert tools._bounded_results(_payload(0))["results"] == []


def test_news_and_combined_sources_keep_dates_and_total_limit():
    payload = _payload(3)
    payload["data"]["news"] = [
        {
            "title": f"News {i}",
            "url": f"https://example.com/news/{i}",
            "snippet": "News excerpt.",
            "date": "2 hours ago",
        }
        for i in range(3)
    ]
    news = tools._bounded_results(payload, "news")
    assert len(news["results"]) == 3
    assert all(row["source_type"] == "news" for row in news["results"])
    assert news["results"][0]["published_date_reported"] == "2 hours ago"
    assert news["results"][0]["content_type"] == "news_snippet"
    both = tools._bounded_results(payload, "both")
    assert [row["source_type"] for row in both["results"]] == ["web", "news", "web"]
    assert both["omitted_results"] == 3
    assert tools._search_sources("both") == ["web", "news"]
    with pytest.raises(ToolError):
        tools._search_sources("images")


def test_shared_key_and_only_explicit_query_are_sent(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-secret")
    calls = []

    def post(query, key, source):
        calls.append((query, key, source))
        return _payload()

    monkeypatch.setattr(tools, "_post_search", post)
    result = asyncio.run(tools.search_web(" public query "))
    assert calls == [("public query", "test-secret", "web")]
    assert "test-secret" not in json.dumps(result)


def _execute(name, arguments):
    events = InMemoryEventSink()
    context = ToolExecutionContext("test", "visitor", ReferenceStore({}), events)
    result = asyncio.run(
        tools.build_reception_tool_registry().execute(
            name=name,
            arguments=json.dumps(arguments),
            context=context,
            metadata=ToolCallMetadata("response", 0, "call"),
        )
    )
    return result, events


def test_no_reference_tools_and_missing_key_is_observable(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert tools.build_reception_tool_registry().names() == ["time_now", "web_search"]
    result, events = _execute("web_search", {"query": "public information"})
    assert not result.ok
    assert result.category == "configuration_error"
    assert "FIRECRAWL_API_KEY" in result.output
    assert any(event.kind == "agent.tool.execution_failed" for event in events.events)


def test_registry_schema_rejects_unapproved_parameters():
    result, _ = _execute("web_search", {"query": "test", "api_key": "model-key"})
    assert result.category == "invalid_arguments"
    result, _ = _execute("web_search", {"query": "x" * 501})
    assert result.category == "invalid_arguments"
    result, _ = _execute("time_now", {})
    assert result.ok


def test_http_request_and_bounded_body(monkeypatch):
    calls = []

    class Response:
        status = 200

        def read(self, limit):
            calls.append(("read", limit))
            return json.dumps(_payload()).encode()

    class Connection:
        def __init__(self, host, timeout):
            calls.append((host, timeout))

        def request(self, method, path, body, headers):
            calls.append((method, path, json.loads(body), headers))

        def getresponse(self):
            return Response()

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(tools.http.client, "HTTPSConnection", Connection)
    assert tools._post_search("question", "secret") == _payload()
    assert calls[0] == ("api.firecrawl.dev", 12)
    assert calls[1][0:2] == ("POST", "/v2/search")
    assert calls[1][2] == {
        "query": "question",
        "limit": 3,
        "sources": ["web"],
        "timeout": 10000,
    }
    assert calls[1][3]["Authorization"] == "Bearer secret"
    assert calls[2] == ("read", tools.MAX_HTTP_BYTES + 1)
    assert calls[-1] == "closed"


@pytest.mark.parametrize(
    "status,category",
    [
        (401, "search_authentication"),
        (429, "search_unavailable"),
        (503, "search_unavailable"),
    ],
)
def test_http_errors_do_not_expose_response_body(monkeypatch, status, category):
    class Response:
        def __init__(self):
            self.status = status

        def read(self, _limit):
            raise AssertionError("Error body should not be read")

    class Connection:
        def __init__(self, *_a, **_kw):
            pass

        def request(self, *_a, **_kw):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(tools.http.client, "HTTPSConnection", Connection)
    with pytest.raises(ToolError) as caught:
        tools._post_search("q", "secret")
    assert caught.value.category == category
    assert "secret" not in str(caught.value)


def test_tool_guidance_is_opt_in_and_updates_provenance():
    from pathlib import Path

    profile = compose_agent_profile(
        profile_id="test", public_dir=Path("profiles/clinic_receptionist")
    )
    updated = tools.with_reception_tool_instructions(profile)
    assert "# Tool use" not in profile.instructions
    assert "# Tool use" in updated.instructions
    assert updated.sha256 != profile.sha256
    assert updated.source_ids[-1] == "builtin:time_web_tool_usage"
    guidance = updated.instructions.split("# Tool use\n", 1)[1]
    headings = [line for line in guidance.splitlines() if line.startswith("##")]
    assert headings == [
        "## Selection",
        "## Available tools",
        "### time_now",
        "### web_search",
        "## Evidence and authority",
        "## Spoken answers",
    ]
    for name in tools.TOOL_NAMES:
        section = guidance.split(f"### {name}\n", 1)[1].split("\n##", 1)[0]
        for label in ("Purpose", "When to use", "Inputs", "Result"):
            assert f"- {label}:" in section


def test_live_tool_enablement_requires_profile():
    result = CliRunner().invoke(cli, ["--agent-tools", "time-web"])
    assert result.exit_code != 0
    assert "requires --agent-profile-id" in result.output
    assert (
        cli.params[[param.name for param in cli.params].index("agent_tools")].default
        == "time-web"
    )
