"""Candidate native-app control service, separate from the production launcher.

The runtime factory runs inside this service, never through OPS or a subprocess.
Mock, bounded AV probe, and the opt-in shared reception runtime remain separate modes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import os
import signal
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from reachy_mini_reception_app.protocol import (
    MAX_MESSAGE_BYTES, VERSION, Command, ProtocolError, identifier, validate_token,
)
from reachy_mini_reception_app.settings import read_private_file

LOGGER = logging.getLogger(__name__)
Runner = Callable[[asyncio.Event, Callable[[], None], str], Awaitable[None]]


@dataclass
class Run:
    owner: object
    session_id: str
    config_id: str
    robot_id: str
    phase: str = "starting"
    reason: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    cancel_sent: bool = False
    ended_at: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    status_reader: Callable[[str], dict[str, Any]] | None = None

    def snapshot(self) -> dict[str, Any]:
        if self.ended_at is None and self.status_reader is not None:
            self.details = self.status_reader(self.session_id)
        return {
            "version": VERSION, "session_id": self.session_id,
            "config_id": self.config_id, "robot_id": self.robot_id,
            "phase": self.phase, "reason": self.reason,
            "elapsed_s": round((self.ended_at or time.monotonic()) - self.started_at, 3),
            "run_id": f"native-{self.session_id}",
            **self.details,
        }


class SessionController:
    """One active runtime, with ownership held until cleanup finishes."""

    def __init__(self, configurations: dict[str, tuple[str, Runner]], *, stop_timeout: float = 5.0,
                 status_reader: Callable[[str], dict[str, Any]] | None = None,
                 receipt_dir: Path | None = None):
        if not configurations or stop_timeout <= 0:
            raise ValueError("Configurations and a positive stop timeout are required")
        for name, (robot_id, _) in configurations.items():
            identifier(name)
            identifier(robot_id)
        self.configurations = dict(configurations)
        self.stop_timeout = stop_timeout
        self.active: Run | None = None
        self.fault_latched = False
        self.closing = False
        self.status_reader = status_reader
        self.receipt_dir = receipt_dir

    def start(self, owner: object, command: Command) -> Run:
        if self.closing or self.fault_latched:
            raise ProtocolError("Service unavailable; operator review required")
        existing = self.active
        if existing is not None and existing.task is not None and not existing.task.done():
            if (
                existing.owner is owner and existing.session_id == command.session_id
                and existing.config_id == command.config_id and existing.robot_id == command.robot_id
            ):
                return existing
            raise ProtocolError("Reception is already owned by another session")
        config = self.configurations.get(command.config_id or "")
        if config is None or config[0] != command.robot_id:
            raise ProtocolError("Unknown configuration or robot identity")
        run = Run(owner, command.session_id, command.config_id or "", config[0])
        run.status_reader = self.status_reader
        self._receipt(run)
        self.active = run
        run.task = asyncio.create_task(self._run(run, config[1]), name="reception-service-runtime")
        return run

    async def _run(self, run: Run, runner: Runner) -> None:
        def ready() -> None:
            if run.phase == "starting" and not run.stop.is_set():
                run.phase = "ready"
                self._receipt(run)

        try:
            await runner(run.stop, ready, run.session_id)
        except asyncio.CancelledError:
            if run.reason is None:
                run.reason = "runtime_cancelled"
            raise
        except Exception:
            LOGGER.exception("Reception runtime failed, session=%s", run.session_id)
            run.phase = "faulted"
            run.reason = "runtime_failed"
            self.fault_latched = True
        finally:
            run.stop.set()
            if run.phase != "faulted":
                run.phase = "stopped"
                run.reason = run.reason or "runtime_completed"
            run.snapshot()
            run.ended_at = time.monotonic()
            self._receipt(run)

    def _receipt(self, run: Run) -> None:
        LOGGER.info("reception lifecycle session=%s phase=%s reason=%s", run.session_id, run.phase, run.reason)
        if self.receipt_dir is None:
            return
        try:
            self.receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.receipt_dir / f"native-{run.session_id}.json"
            record = {**run.snapshot(), "updated_at": time.time(), "service_pid": os.getpid()}
            temporary = path.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(record, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, path)
        except OSError:
            # Receipt storage must never prevent output cancellation or SDK cleanup.
            LOGGER.exception("Could not persist lifecycle receipt session=%s", run.session_id)

    async def stop(self, run: Run, reason: str) -> None:
        if run.task is None or run.task.done():
            return
        run.reason = run.reason or reason
        if run.phase != "faulted":
            run.phase = "stopping"
        run.stop.set()
        self._receipt(run)
        done, _ = await asyncio.wait({run.task}, timeout=self.stop_timeout)
        if not done:
            # A timeout is not proof that physical resources have been released.
            self.fault_latched = True
            run.phase = "faulted"
            run.reason = "stop_timeout"
            self._receipt(run)
            if not run.cancel_sent:
                run.cancel_sent = True
                run.task.cancel()
            await asyncio.wait({run.task}, timeout=self.stop_timeout)

    async def shutdown(self) -> None:
        self.closing = True
        if self.active is not None:
            await self.stop(self.active, "service_shutdown")


class ControlServer:
    def __init__(self, controller: SessionController, token: str, *, lease_timeout: float = 6.0):
        if lease_timeout <= 0:
            raise ValueError("lease_timeout must be positive")
        self.controller = controller
        self.token = validate_token(token)
        self.lease_timeout = lease_timeout

    def authorize(self, connection: ServerConnection, request: Any) -> Any:
        values = request.headers.get_all("Authorization")
        authorized = len(values) == 1 and hmac.compare_digest(
            values[0].encode("utf-8"), f"Bearer {self.token}".encode("utf-8")
        )
        if request.path != "/reception/control" or request.headers.get_all("Origin") or not authorized:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        return None

    def listen(self, host: str = "127.0.0.1", port: int = 0, *, tls: ssl.SSLContext | None = None):
        if host not in {"127.0.0.1", "::1", "localhost"} and tls is None:
            raise ValueError("Non-loopback listeners require TLS")
        return serve(
            self.handle, host, port, process_request=self.authorize, ssl=tls,
            max_size=MAX_MESSAGE_BYTES, max_queue=4,
            ping_interval=2, ping_timeout=4, close_timeout=1,
        )

    async def handle(self, websocket: ServerConnection) -> None:
        owner = object()
        run: Run | None = None
        reason = "control_disconnected"
        try:
            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=self.lease_timeout)
                except TimeoutError:
                    reason = "control_heartbeat_timeout"
                    break
                command = Command.decode(message)
                if run is None:
                    if command.action != "start":
                        raise ProtocolError("Start required")
                    run = self.controller.start(owner, command)
                else:
                    if command.session_id != run.session_id:
                        raise ProtocolError("Session mismatch")
                    if command.action == "start" and (
                        command.config_id != run.config_id or command.robot_id != run.robot_id
                    ):
                        raise ProtocolError("Configuration cannot change during a session")
                    if command.action == "stop":
                        reason = "native_stop"
                        await self.controller.stop(run, reason)
                await websocket.send(json.dumps(run.snapshot()))
                if run.phase in {"stopped", "faulted"}:
                    break
        except ProtocolError as exc:
            reason = "control_protocol_error"
            with contextlib.suppress(ConnectionClosed):
                await websocket.send(json.dumps({"version": VERSION, "error": str(exc)}))
        except ConnectionClosed:
            pass
        finally:
            if run is not None:
                await self.controller.stop(run, reason)


async def mock_runtime(stop: asyncio.Event, ready: Callable[[], None], session_id: str = "") -> None:
    ready()
    await stop.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Candidate Reception control service (not production OPS)")
    parser.add_argument("--mode", choices=["mock", "av-probe", "reception"], required=True)
    parser.add_argument("--runtime-config", type=Path,
                        help="Explicit server-side reception settings; required in reception mode")
    parser.add_argument("--env-file", type=Path, help="Existing private service environment; never sent to robot")
    parser.add_argument("--receipt-dir", type=Path, help="Private lifecycle receipts; no conversation contents")
    parser.add_argument("--config-id", default=None, help="Allowlisted native configuration ID")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--robot-id", default="test-robot")
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--confirm-physical", action="store_true")
    parser.add_argument("--robot-host")
    parser.add_argument("--probe-wav", type=Path)
    parser.add_argument("--probe-duration", type=float, default=60)
    parser.add_argument("--probe-lead-in-ms", type=float, default=0,
                        help="Test-only silence prepended to the WAV in memory (0-1000 ms)")
    parser.add_argument("--audio-send-chain", choices=["stock", "legacy"], default="stock",
                        help="AV probe SDK send chain; legacy reuses the production override (SDK 1.10.0 only)")
    args = parser.parse_args()
    if args.env_file is not None:
        read_private_file(args.env_file)
        from .official_runtime.env import load_project_env

        load_project_env(args.env_file)
    token = validate_token(
        read_private_file(args.token_file).strip() if args.token_file
        else os.environ.get("RECEPTION_CONTROL_TOKEN", "")
    )
    tls = None
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key must be supplied together")
    if args.tls_cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.tls_cert, args.tls_key)
    runner = mock_runtime
    status_reader = None
    if args.mode == "av-probe":
        if not args.confirm_physical or not args.robot_host or args.probe_wav is None:
            parser.error("av-probe requires --confirm-physical, --robot-host and --probe-wav")
        from .reception_av_probe import make_av_probe

        probe = make_av_probe(host=args.robot_host, wav_path=args.probe_wav, duration=args.probe_duration,
                              audio_send_chain=args.audio_send_chain, lead_in_ms=args.probe_lead_in_ms)

        async def runner(stop, ready, session_id):
            await probe(stop, ready)
    elif args.mode == "reception":
        if not args.confirm_physical or args.runtime_config is None or args.robot_id == "test-robot":
            parser.error("reception requires --confirm-physical, --runtime-config and an explicit --robot-id")
        from .reception_runtime import load_runtime_options, make_reception_runtime
        from .reception_status import ReceptionStatus

        options = load_runtime_options(args.runtime_config)
        public_status = ReceptionStatus(options)
        status_reader = public_status.snapshot
        runner = make_reception_runtime(options, status=public_status)
    controller = SessionController({args.config_id or args.mode: (args.robot_id, runner)},
                                   status_reader=status_reader, receipt_dir=args.receipt_dir)
    server = ControlServer(controller, token)

    async def run() -> None:
        stop_service = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop_service.set)
        try:
            async with server.listen(args.host, args.port, tls=tls) as listener:
                port = listener.sockets[0].getsockname()[1]
                print(f"Reception {args.mode} control listening on {args.host}:{port}", flush=True)
                await stop_service.wait()
                await controller.shutdown()
        finally:
            try:
                await controller.shutdown()
            finally:
                loop.remove_signal_handler(signal.SIGTERM)

    try:
        logging.basicConfig(level=logging.INFO)
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
