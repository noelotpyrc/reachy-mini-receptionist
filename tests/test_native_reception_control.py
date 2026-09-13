"""Offline control-plane tests. No SDK instance or physical robot connection."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from reachy_mini_brain.reception_service import ControlServer, SessionController, mock_runtime
from reachy_mini_reception_app.client import control_session
from reachy_mini_reception_app.protocol import Command, ProtocolError, validate_url
from reachy_mini_reception_app.settings import Settings

TOKEN = "test-only-token-0123456789-abcdef0123456789"
START = Command("start", "run-1", "mock", "test-robot")


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


def controller(runner=mock_runtime, **kwargs):
    async def bound(stop, ready, session_id):
        await runner(stop, ready)

    return SessionController({"mock": ("test-robot", bound)}, **kwargs)


def connection(url, token=TOKEN, **kwargs):
    return connect(url, additional_headers={"Authorization": f"Bearer {token}"}, proxy=None, **kwargs)


def endpoint(listener):
    return f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}/reception/control"


def test_protocol_round_trip():
    assert Command.decode(START.encode()) == START


@pytest.mark.parametrize("payload", [
    "[]", "{}", "null", "{", b"{}", "x" * 4097,
    '{"version":true,"action":"heartbeat","session_id":"run-1"}',
    '{"version":1,"action":"shell","session_id":"run-1"}',
    '{"version":1,"action":"heartbeat","session_id":"../path"}',
    '{"version":1,"action":"heartbeat","session_id":"run-1","audio":"oops"}',
])
def test_protocol_rejects_invalid_input(payload):
    with pytest.raises(ProtocolError):
        Command.decode(payload)


@pytest.mark.parametrize("url", [
    "ws://m1max:8876/reception/control", "https://m1max/reception/control",
    "wss://user:secret@m1max/reception/control", "wss://m1max/reception/control?token=secret",
])
def test_transport_rejects_insecure_remote_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_single_owner_and_ready_only_after_runtime_callback():
    async def exercise():
        initialized = asyncio.Event()
        cleaned = []

        async def runner(stop, ready):
            try:
                await initialized.wait()
                ready()
                await stop.wait()
            finally:
                cleaned.append(True)

        service = controller(runner)
        owner = object()
        run = service.start(owner, START)
        assert run.phase == "starting"
        assert service.start(owner, START) is run
        with pytest.raises(ProtocolError):
            service.start(object(), START)
        initialized.set()
        await eventually(lambda: run.phase == "ready")
        await service.stop(run, "native_stop")
        assert run.phase == "stopped"
        assert run.reason == "native_stop"
        assert cleaned == [True]
        await service.stop(run, "duplicate_stop")
        assert cleaned == [True]

    asyncio.run(exercise())


def test_stop_during_startup_never_publishes_ready():
    async def exercise():
        async def runner(stop, ready):
            await stop.wait()
            ready()

        service = controller(runner)
        run = service.start(object(), START)
        await service.stop(run, "native_stop")
        assert run.phase == "stopped"

    asyncio.run(exercise())


def test_cleanup_timeout_latches_service_fault():
    async def exercise():
        cleaned = asyncio.Event()

        async def runner(stop, ready):
            try:
                ready()
                await asyncio.Future()
            finally:
                cleaned.set()

        service = controller(runner, stop_timeout=0.01)
        run = service.start(object(), START)
        await eventually(lambda: run.phase == "ready")
        await service.stop(run, "native_stop")
        assert cleaned.is_set()
        assert run.phase == "faulted"
        assert run.reason == "stop_timeout"
        with pytest.raises(ProtocolError):
            service.start(object(), START)

    asyncio.run(exercise())


def test_runtime_error_is_publicly_redacted():
    async def exercise():
        async def runner(stop, ready):
            raise RuntimeError("private provider details")

        service = controller(runner)
        run = service.start(object(), START)
        await run.task
        assert run.phase == "faulted"
        assert run.reason == "runtime_failed"
        assert "private provider" not in json.dumps(run.snapshot())

    asyncio.run(exercise())


def test_control_client_and_server_start_heartbeat_stop():
    async def exercise():
        service = controller()
        stop = threading.Event()
        statuses = []

        def record(status):
            statuses.append(status)
            if status["phase"] == "ready":
                stop.set()

        async with ControlServer(service, TOKEN).listen() as listener:
            await control_session(
                url=endpoint(listener), token=TOKEN, config_id="mock", robot_id="test-robot",
                stop_event=stop, on_status=record, heartbeat_interval=0.01,
            )
        assert [s["phase"] for s in statuses][-2:] == ["stopping", "stopped"]
        assert service.active.reason == "native_stop"

    asyncio.run(exercise())


@pytest.mark.parametrize("abandon", ["disconnect", "silent"])
def test_loss_of_control_stops_runtime(abandon):
    async def exercise():
        service = controller()
        server = ControlServer(service, TOKEN, lease_timeout=0.05)
        async with server.listen() as listener:
            async with connection(endpoint(listener)) as ws:
                await ws.send(START.encode())
                await ws.recv()
                if abandon == "disconnect":
                    await ws.close()
                else:
                    # WebSocket ping/pong alone must not renew application liveness.
                    await asyncio.sleep(0.1)
                await eventually(lambda: service.active.phase == "stopped")
        expected = "control_disconnected" if abandon == "disconnect" else "control_heartbeat_timeout"
        assert service.active.reason == expected

    asyncio.run(exercise())


def test_stale_or_second_connection_cannot_stop_another_owner():
    async def exercise():
        service = controller()
        async with ControlServer(service, TOKEN).listen() as listener:
            async with connection(endpoint(listener)) as first:
                await first.send(START.encode())
                await first.recv()
                async with connection(endpoint(listener)) as second:
                    await second.send(Command("stop", "run-1").encode())
                    assert "error" in json.loads(await second.recv())
                async with connection(endpoint(listener)) as second:
                    await second.send(START.encode())
                    assert "error" in json.loads(await second.recv())
                assert not service.active.stop.is_set()
                await first.send(Command("stop", "run-1").encode())
                assert json.loads(await first.recv())["phase"] == "stopped"

    asyncio.run(exercise())


def test_wrong_session_on_owner_connection_fails_closed():
    async def exercise():
        service = controller()
        async with ControlServer(service, TOKEN).listen() as listener:
            async with connection(endpoint(listener)) as ws:
                await ws.send(START.encode())
                await ws.recv()
                await ws.send(Command("heartbeat", "wrong-run").encode())
                assert "error" in json.loads(await ws.recv())
                await eventually(lambda: service.active.phase == "stopped")
        assert service.active.reason == "control_protocol_error"

    asyncio.run(exercise())


def test_auth_path_origin_and_config_rejected_without_starting():
    async def exercise():
        service = controller()
        async with ControlServer(service, TOKEN).listen() as listener:
            url = endpoint(listener)
            for target, secret, extra in [
                (url, "wrong", {}), (url + "?token=bad", TOKEN, {}),
                (url, TOKEN, {"origin": "https://untrusted.example"}),
            ]:
                with pytest.raises(InvalidStatus):
                    async with connection(target, secret, **extra):
                        pytest.fail("Unexpected connection")
            async with connection(url) as ws:
                await ws.send(Command("start", "run-1", "production", "test-robot").encode())
                assert "error" in json.loads(await ws.recv())
            assert service.active is None

    asyncio.run(exercise())


def test_stop_before_client_start_never_connects():
    stop = threading.Event()
    stop.set()
    statuses = []
    asyncio.run(control_session(
        url="ws://127.0.0.1:1/reception/control", token=TOKEN,
        config_id="mock", robot_id="test-robot", stop_event=stop, on_status=statuses.append,
    ))
    assert statuses[-1]["phase"] == "stopped"


def test_server_shutdown_stops_run_without_waiting_for_client_stop():
    async def exercise():
        service = controller()
        async with ControlServer(service, TOKEN).listen() as listener:
            async with connection(endpoint(listener)) as ws:
                await ws.send(START.encode())
                await ws.recv()
                await service.shutdown()
                assert service.active.phase == "stopped"
                with pytest.raises(ProtocolError):
                    service.start(object(), START)
                with pytest.raises(ConnectionClosed):
                    await ws.recv()

    asyncio.run(exercise())


def test_client_faults_without_reconnecting_when_service_dies():
    async def exercise():
        service = controller()
        stop = threading.Event()
        statuses = []
        async with ControlServer(service, TOKEN).listen() as listener:
            task = asyncio.create_task(control_session(
                url=endpoint(listener), token=TOKEN, config_id="mock", robot_id="test-robot",
                stop_event=stop, on_status=statuses.append, heartbeat_interval=0.01,
            ))
            await eventually(lambda: any(s["phase"] == "ready" for s in statuses))
            await asyncio.gather(*(ws.close() for ws in list(listener.connections)))
            with pytest.raises(ConnectionClosed):
                await task
            await eventually(lambda: service.active.phase == "stopped")
        assert statuses[-1]["phase"] == "faulted"
        assert len({s["session_id"] for s in statuses}) == 1
        assert TOKEN not in json.dumps(statuses)

    asyncio.run(exercise())


def test_native_run_uses_control_only_and_sets_framework_stop(monkeypatch):
    from reachy_mini_reception_app import main as native

    app = object.__new__(native.ReceptionApp)
    app.config = Settings("ws://127.0.0.1/reception/control", TOKEN, "mock", "test-robot")
    app._status_lock = threading.Lock()
    app._status = {}
    called = []

    async def fake_control(**kwargs):
        called.append(kwargs)
        kwargs["on_status"]({"phase": "stopped", "reason": "native_stop"})

    class ForbiddenSDK:
        def __getattr__(self, name):
            pytest.fail(f"Native runtime accessed robot SDK: {name}")

    monkeypatch.setattr(native, "control_session", fake_control)
    stop = threading.Event()
    app.run(ForbiddenSDK(), stop)
    assert len(called) == 1
    assert stop.is_set()
    assert app.status()["phase"] == "stopped"


def test_native_reports_service_fault_to_official_framework(monkeypatch):
    from reachy_mini_reception_app import main as native

    app = object.__new__(native.ReceptionApp)
    app.config = Settings("ws://127.0.0.1/reception/control", TOKEN, "mock", "test-robot")
    app._status_lock = threading.Lock()
    app._status = {}

    async def fake_control(**kwargs):
        kwargs["on_status"]({"phase": "faulted", "reason": "stop_timeout"})

    monkeypatch.setattr(native, "control_session", fake_control)
    stop = threading.Event()
    with pytest.raises(RuntimeError, match="operator review"):
        app.run(object(), stop)
    assert stop.is_set()
