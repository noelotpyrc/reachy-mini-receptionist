"""Control-only client; no media transport, robot IO, or reconnect loop."""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
import uuid
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect

from .protocol import (
    MAX_MESSAGE_BYTES, VERSION, SERVICE_ERRORS, Command, ProtocolError,
    identifier, validate_token, validate_url,
)


async def control_session(
    *, url: str, token: str, config_id: str, robot_id: str,
    stop_event: threading.Event,
    on_status: Callable[[dict[str, Any]], None],
    heartbeat_interval: float = 1.0,
    request_timeout: float = 3.0,
    stop_timeout: float = 12.0,
    tls: ssl.SSLContext | None = None,
) -> None:
    validate_url(url)
    validate_token(token)
    identifier(config_id)
    identifier(robot_id)
    if tls is not None and (
        not url.startswith("wss://") or not tls.check_hostname or tls.verify_mode != ssl.CERT_REQUIRED
    ):
        raise ValueError("TLS must verify the service certificate and hostname")
    if min(heartbeat_interval, request_timeout, stop_timeout) <= 0:
        raise ValueError("Timeouts must be positive")
    if stop_event.is_set():
        on_status({"phase": "stopped", "reason": "stopped_before_connect"})
        return
    session_id = uuid.uuid4().hex
    on_status({"phase": "starting", "session_id": session_id})
    try:
        async with connect(
            url, additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=request_timeout, close_timeout=1,
            ping_interval=2, ping_timeout=4,
            max_size=MAX_MESSAGE_BYTES, max_queue=4, proxy=None,
            **({"ssl": tls} if tls is not None else {}),
        ) as websocket:
            async def exchange(command: Command, timeout: float) -> dict[str, Any]:
                async def request() -> dict[str, Any]:
                    await websocket.send(command.encode())
                    status = json.loads(await websocket.recv())
                    if isinstance(status, dict) and status.get("version") == VERSION and "error" in status:
                        code = status.get("code")
                        if not isinstance(code, str) or code not in SERVICE_ERRORS:
                            code = "control_protocol_error"
                        raise ProtocolError(SERVICE_ERRORS[code], code=code)
                    if (
                        not isinstance(status, dict) or status.get("version") != VERSION
                        or status.get("session_id") != session_id
                        or status.get("config_id") != config_id or status.get("robot_id") != robot_id
                        or status.get("phase") not in {"starting", "ready", "stopping", "stopped", "faulted"}
                    ):
                        raise ValueError("Invalid service status")
                    on_status(status)
                    return status
                return await asyncio.wait_for(request(), timeout=timeout)

            # Stop during connection setup must not create a physical run later.
            if stop_event.is_set():
                on_status({"phase": "stopped", "reason": "stopped_before_start"})
                return
            status = await exchange(Command("start", session_id, config_id, robot_id), request_timeout)
            while status["phase"] not in {"stopped", "faulted"}:
                deadline = asyncio.get_running_loop().time() + heartbeat_interval
                while not stop_event.is_set() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(min(0.05, heartbeat_interval))
                if stop_event.is_set():
                    on_status({"phase": "stopping", "session_id": session_id})
                    status = await exchange(Command("stop", session_id), stop_timeout)
                    if status["phase"] not in {"stopped", "faulted"}:
                        raise ValueError("Stop did not reach a terminal state")
                    return
                status = await exchange(Command("heartbeat", session_id), request_timeout)
    except ProtocolError as exc:
        on_status({"phase": "faulted", "session_id": session_id, "reason": exc.code})
        raise
    except Exception:
        # Never expose exception text containing URLs/credentials in the UI.
        on_status({"phase": "faulted", "session_id": session_id, "reason": "control_connection_failed"})
        raise
