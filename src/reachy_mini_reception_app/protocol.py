"""Versioned control messages; audio stays on the existing SDK connection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

VERSION = 1
MAX_MESSAGE_BYTES = 4096
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")


class ProtocolError(ValueError):
    pass


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ProtocolError("Invalid identifier")
    return value


def validate_token(token: str) -> str:
    if len(token) < 32 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ValueError("Control token must contain at least 32 printable non-space ASCII characters")
    return token


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/reception/control"
    ):
        raise ValueError("Expected a ws(s) URL ending in /reception/control without credentials or query")
    if parsed.scheme == "ws" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Non-loopback control connections require wss")
    return url


@dataclass(frozen=True)
class Command:
    action: str
    session_id: str
    config_id: str | None = None
    robot_id: str | None = None

    def encode(self) -> str:
        result: dict[str, Any] = {
            "version": VERSION, "action": self.action, "session_id": self.session_id,
        }
        if self.action == "start":
            result.update(config_id=self.config_id, robot_id=self.robot_id)
        return json.dumps(result)

    @classmethod
    def decode(cls, payload: str | bytes) -> Command:
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ProtocolError("Expected a bounded JSON text message")
        try:
            value = json.loads(payload)
        except (ValueError, RecursionError) as exc:
            raise ProtocolError("Invalid JSON") from exc
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != VERSION:
            raise ProtocolError("Unsupported protocol version")
        action = value.get("action")
        if action not in ("start", "heartbeat", "stop"):
            raise ProtocolError("Unknown action")
        fields = {"version", "action", "session_id"}
        if action == "start":
            fields |= {"config_id", "robot_id"}
        if set(value) != fields:
            raise ProtocolError("Unexpected message fields")
        return cls(
            action, identifier(value["session_id"]),
            identifier(value["config_id"]) if action == "start" else None,
            identifier(value["robot_id"]) if action == "start" else None,
        )
