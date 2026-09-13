"""Private native-app settings, independent of the daemon's environment."""

from __future__ import annotations

import json
import os
import ssl
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import identifier, validate_token, validate_url


def read_private_file(path: Path) -> str:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Reception private files must be regular files with owner-only permissions")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("Reception private files must belong to the app user")
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Settings:
    url: str
    token: str = field(repr=False)
    config_id: str
    robot_id: str
    tls: ssl.SSLContext | None = field(default=None, repr=False)


def load_settings(
    path: Path | None = None, *, environ: Mapping[str, str] | None = None,
) -> Settings:
    env = os.environ if environ is None else environ
    if path is None:
        root = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        path = root / "reachy-mini-reception" / "config.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(read_private_file(path))
        except json.JSONDecodeError:
            raise ValueError("Invalid Reception configuration JSON") from None
        fields = {"service_url", "token_file", "config_id", "robot_id", "tls_ca_file"}
        if not isinstance(data, dict) or set(data) - fields or any(
            not isinstance(value, str) or not value for value in data.values()
        ):
            raise ValueError("Invalid Reception configuration fields")

    url = validate_url(env.get("RECEPTION_SERVICE_URL", data.get("service_url", "")))
    config_id = identifier(env.get("RECEPTION_CONFIG_ID", data.get("config_id", "")))
    robot_id = identifier(env.get("RECEPTION_ROBOT_ID", data.get("robot_id", "")))

    def file_path(value: str) -> Path:
        result = Path(value).expanduser()
        return result if result.is_absolute() else path.parent / result

    token = env.get("RECEPTION_CONTROL_TOKEN")
    if token is None:
        token_file = data.get("token_file")
        if not token_file:
            raise ValueError("Reception control credential is not configured")
        token = read_private_file(file_path(token_file)).strip()
    validate_token(token)
    ca_file = env.get("RECEPTION_TLS_CA_FILE", data.get("tls_ca_file"))
    tls = None
    if ca_file and not url.startswith("wss://"):
        raise ValueError("A custom CA requires a wss connection")
    if url.startswith("wss://"):
        tls = ssl.create_default_context(cafile=str(file_path(ca_file)) if ca_file else None)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
    return Settings(url, token, config_id, robot_id, tls)
