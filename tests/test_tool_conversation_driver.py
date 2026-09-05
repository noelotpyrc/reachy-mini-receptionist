import asyncio
import importlib.util
from pathlib import Path

import pytest


def _driver():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts/test_reception_tool_conversation.py"
    )
    spec = importlib.util.spec_from_file_location("tool_conversation_driver", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_waits_for_release_before_new_session(monkeypatch):
    driver = _driver()
    calls = []
    states = iter(["busy", "busy", "idle"])

    def read(url):
        calls.append(url)
        return {"units": [{"state": next(states)}]}

    monkeypatch.setattr(driver, "_read_pool", read)
    asyncio.run(driver.wait_for_idle_slot("ws://127.0.0.1:18766/v1/realtime"))
    assert calls == ["http://127.0.0.1:18766/v1/pool"] * 3


def test_slot_wait_is_bounded(monkeypatch):
    driver = _driver()
    monkeypatch.setattr(
        driver, "_read_pool", lambda url: {"units": [{"state": "busy"}]}
    )
    with pytest.raises(TimeoutError):
        asyncio.run(
            driver.wait_for_idle_slot("wss://example.com/v1/realtime", timeout=0.05)
        )


@pytest.mark.parametrize(
    "text",
    [
        "Visit https://example.com/renew.",
        "Go to www.example.com/renew.",
        "Use [renewal](https://example.com).",
    ],
)
def test_spoken_check_rejects_links(text):
    with pytest.raises(RuntimeError, match="explicit URL or link"):
        _driver().check_spoken_format(text)


@pytest.mark.parametrize(
    "text",
    [
        "Visit NJMVC.gov and select Online Services.",
        "Go to www.example.com.",
        "Choose **Online Services**.",
        "Choose __Online Services__.",
        "Visit the New Jersey Motor Vehicle Commission website. "
        "Choose Online Services, then Renew License or ID.",
    ],
)
def test_spoken_check_accepts_navigation(text):
    _driver().check_spoken_format(text)
