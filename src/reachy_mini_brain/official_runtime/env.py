"""Environment helpers for the isolated official-style runtime."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def load_project_env(path: Path | None = None, *, override: bool = False) -> Path:
    """Load simple KEY=VALUE pairs from the project .env file.

    Existing shell environment values win by default. The parser intentionally
    handles the subset we use for local credentials/config and avoids adding a
    dependency just for CLI bootstrap.
    """

    env_path = path or DEFAULT_ENV_PATH
    if not env_path.exists():
        return env_path

    for line in env_path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value
    return env_path


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped.removeprefix("export ").lstrip()

    key, value = stripped.split("=", 1)
    key = key.strip()
    if not key or key.startswith("#"):
        return None

    value = value.strip()
    if value and value[0] in {"'", '"'}:
        return key, _strip_quoted_value(value)
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return key, value


def _strip_quoted_value(value: str) -> str:
    quote = value[0]
    end = value.find(quote, 1)
    if end == -1:
        return value[1:]
    return value[1:end]
