"""Environment helpers for the isolated official-style runtime."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


PROJECT_ROOT = Path(
    os.environ.get("REACHY_REPO", str(Path(__file__).resolve().parents[3]))
).expanduser().resolve()
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

_GSTREAMER_GENERATED_KEYS = {
    "GST_PLUGIN_PATH",
    "GST_PLUGIN_PATH_1_0",
    "GST_PLUGIN_SCANNER",
    "GST_PLUGIN_SCANNER_1_0",
    "GST_PLUGIN_SYSTEM_PATH",
    "GST_PLUGIN_SYSTEM_PATH_1_0",
    "GST_PYTHONPATH",
    "GST_PYTHONPATH_1_0",
    "GST_REGISTRY",
    "GST_REGISTRY_1_0",
}
_GSTREAMER_PATH_KEYS = {
    "DYLD_LIBRARY_PATH",
    "GI_TYPELIB_PATH",
    "GIO_EXTRA_MODULES",
    "LD_LIBRARY_PATH",
    "PATH",
    "PYGI_DLL_DIRS",
    "PYTHONPATH",
    "XDG_CONFIG_DIRS",
    "XDG_DATA_DIRS",
}


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


def clean_gstreamer_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove path and registry values injected by the GStreamer wheel bootstrap.

    The wheel's ``.pth`` file prepends these values at every Python startup. Long-running
    OPS uses nested Python interpreters, so each process boundary must start clean and let
    the selected child interpreter apply its own environment exactly once.
    """

    cleaned = dict(os.environ if environment is None else environment)
    for key in _GSTREAMER_GENERATED_KEYS:
        cleaned.pop(key, None)
    for key in _GSTREAMER_PATH_KEYS:
        value = cleaned.get(key)
        if not value:
            continue
        retained = [part for part in value.split(os.pathsep) if not _is_gstreamer_wheel_path(part)]
        if retained:
            cleaned[key] = os.pathsep.join(retained)
        else:
            cleaned.pop(key, None)
    return cleaned


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


def _is_gstreamer_wheel_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return "/site-packages/gstreamer_" in normalized


def _strip_quoted_value(value: str) -> str:
    quote = value[0]
    end = value.find(quote, 1)
    if end == -1:
        return value[1:]
    return value[1:end]
