"""Read-only access to an allowlisted library of profile references."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

LOGGER = logging.getLogger(__name__)

TOOLSET = "reference_readonly"
CATALOG_ENV = "REFERENCE_LIBRARY_CATALOG"
DEFAULT_CATALOG_RELATIVE_PATH = Path("context/reference_library/catalog.yaml")
MAX_CATALOG_ENTRIES = 128
MAX_REFERENCE_BYTES = 64 * 1024
REFERENCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class ReferenceLibraryError(ValueError):
    """Raised when the catalog or a requested reference violates policy."""


def _json_error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _catalog_path() -> Path:
    override = os.getenv(CATALOG_ENV, "").strip()
    if override:
        return Path(override).expanduser()

    try:
        from hermes_cli.config import load_config

        config = load_config()
        configured = (config.get("reference_library") or {}).get("catalog")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser()
    except Exception:
        pass

    hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / DEFAULT_CATALOG_RELATIVE_PATH


def _load_catalog() -> tuple[Path, dict[str, dict[str, Any]]]:
    catalog_path = _catalog_path().resolve(strict=True)
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    except UnicodeDecodeError as exc:
        raise ReferenceLibraryError("reference catalog must be UTF-8") from exc

    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ReferenceLibraryError("reference catalog must be a version 1 mapping")

    references = raw.get("references")
    if not isinstance(references, dict):
        raise ReferenceLibraryError("reference catalog requires a references mapping")
    if len(references) > MAX_CATALOG_ENTRIES:
        raise ReferenceLibraryError(
            f"reference catalog exceeds {MAX_CATALOG_ENTRIES} entries"
        )

    normalized: dict[str, dict[str, Any]] = {}
    for reference_id, entry in references.items():
        if not isinstance(reference_id, str) or not REFERENCE_ID_RE.fullmatch(reference_id):
            raise ReferenceLibraryError(f"invalid reference ID: {reference_id!r}")
        if not isinstance(entry, dict):
            raise ReferenceLibraryError(f"reference {reference_id!r} must be a mapping")

        path = entry.get("path")
        title = entry.get("title")
        summary = entry.get("summary")
        audience = entry.get("audience")
        delivery = entry.get("delivery")
        tags = entry.get("tags", [])
        max_bytes = entry.get("max_bytes", MAX_REFERENCE_BYTES)

        if not isinstance(path, str) or not path.strip() or Path(path).is_absolute():
            raise ReferenceLibraryError(
                f"reference {reference_id!r} requires a relative path"
            )
        if not isinstance(title, str) or not title.strip():
            raise ReferenceLibraryError(f"reference {reference_id!r} requires a title")
        if not isinstance(summary, str) or not summary.strip():
            raise ReferenceLibraryError(f"reference {reference_id!r} requires a summary")
        if audience != "visitor":
            raise ReferenceLibraryError(
                f"reference {reference_id!r} has unsupported audience {audience!r}"
            )
        if delivery not in {"prompt", "on_demand"}:
            raise ReferenceLibraryError(
                f"reference {reference_id!r} delivery must be 'prompt' or 'on_demand'"
            )
        if not isinstance(tags, list) or not all(
            isinstance(tag, str) and tag.strip() for tag in tags
        ):
            raise ReferenceLibraryError(
                f"reference {reference_id!r} tags must be non-empty strings"
            )
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes < 1
            or max_bytes > MAX_REFERENCE_BYTES
        ):
            raise ReferenceLibraryError(
                f"reference {reference_id!r} max_bytes must be between 1 and "
                f"{MAX_REFERENCE_BYTES}"
            )

        normalized[reference_id] = {
            "path": path,
            "title": title.strip(),
            "summary": summary.strip(),
            "audience": audience,
            "delivery": delivery,
            "tags": [tag.strip() for tag in tags],
            "max_bytes": max_bytes,
        }

    return catalog_path, normalized


def reference_catalog(topic: str = "") -> str:
    """Return approved reference IDs and routing metadata, never file paths."""
    try:
        _, references = _load_catalog()
        query = topic.strip().casefold()
        if len(query) > 128:
            raise ReferenceLibraryError("catalog topic must be at most 128 characters")

        items = []
        for reference_id, entry in sorted(references.items()):
            if entry["delivery"] != "on_demand":
                continue
            searchable = " ".join(
                [reference_id, entry["title"], entry["summary"], *entry["tags"]]
            ).casefold()
            if query and query not in searchable:
                continue
            items.append(
                {
                    "reference_id": reference_id,
                    "title": entry["title"],
                    "summary": entry["summary"],
                    "tags": entry["tags"],
                    "audience": entry["audience"],
                }
            )
        return json.dumps({"references": items}, ensure_ascii=False)
    except ReferenceLibraryError as exc:
        LOGGER.warning("reference catalog request failed: %s", exc)
        return _json_error(str(exc))
    except OSError as exc:
        LOGGER.warning(
            "reference catalog storage unavailable: %s", type(exc).__name__
        )
        return _json_error("reference library storage is unavailable")
    except yaml.YAMLError:
        LOGGER.warning("reference catalog contains invalid YAML")
        return _json_error("reference catalog is invalid")


def reference_read(reference_id: str) -> str:
    """Read one approved UTF-8 reference while enforcing catalog confinement."""
    try:
        catalog_path, references = _load_catalog()
        entry = references.get(reference_id)
        if entry is None or entry["delivery"] != "on_demand":
            raise ReferenceLibraryError(f"unknown reference ID: {reference_id!r}")

        root = catalog_path.parent.resolve(strict=True)
        candidate = (root / entry["path"]).resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ReferenceLibraryError(
                f"reference {reference_id!r} resolves outside the reference root"
            ) from exc
        if not candidate.is_file():
            raise ReferenceLibraryError(f"reference {reference_id!r} is not a regular file")

        content_bytes = candidate.read_bytes()
        if len(content_bytes) > entry["max_bytes"]:
            raise ReferenceLibraryError(
                f"reference {reference_id!r} exceeds its {entry['max_bytes']} byte limit"
            )
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReferenceLibraryError(
                f"reference {reference_id!r} must be UTF-8 text"
            ) from exc

        digest = hashlib.sha256(content_bytes).hexdigest()
        LOGGER.info(
            "reference read id=%s sha256=%s bytes=%d",
            reference_id,
            digest,
            len(content_bytes),
        )
        return json.dumps(
            {
                "reference_id": reference_id,
                "title": entry["title"],
                "audience": entry["audience"],
                "sha256": digest,
                "content": content,
            },
            ensure_ascii=False,
        )
    except ReferenceLibraryError as exc:
        LOGGER.warning("reference read failed id=%r: %s", reference_id, exc)
        return _json_error(str(exc))
    except OSError as exc:
        LOGGER.warning(
            "reference storage unavailable id=%r: %s",
            reference_id,
            type(exc).__name__,
        )
        return _json_error("reference library storage is unavailable")
    except yaml.YAMLError:
        LOGGER.warning("reference catalog contains invalid YAML")
        return _json_error("reference catalog is invalid")


def _handle_catalog(args: dict[str, Any], **_: Any) -> str:
    return reference_catalog(str(args.get("topic", "")))


def _handle_read(args: dict[str, Any], **_: Any) -> str:
    reference_id = args.get("reference_id")
    if not isinstance(reference_id, str):
        return _json_error("reference_id must be a string")
    return reference_read(reference_id)


def register(ctx: Any) -> None:
    # Fail plugin loading early when its allowlist is absent or invalid.
    _, references = _load_catalog()
    on_demand_ids = sorted(
        reference_id
        for reference_id, entry in references.items()
        if entry["delivery"] == "on_demand"
    )

    ctx.register_tool(
        name="reference_catalog",
        toolset=TOOLSET,
        schema={
            "name": "reference_catalog",
            "description": (
                "List approved on-demand visitor-safe reference documents and their IDs. "
                "Use this only when you do not already know which reference to read."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Optional topic used to filter reference metadata.",
                    }
                },
                "additionalProperties": False,
            },
        },
        handler=_handle_catalog,
        description="Discover approved profile reference documents.",
    )
    if on_demand_ids:
        ctx.register_tool(
            name="reference_read",
            toolset=TOOLSET,
            schema={
                "name": "reference_read",
                "description": (
                    "Read one approved on-demand visitor-safe reference by reference ID. "
                    "This tool accepts no filesystem path and cannot modify files."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reference_id": {
                            "type": "string",
                            "enum": on_demand_ids,
                            "description": "An exact approved on-demand reference ID.",
                        }
                    },
                    "required": ["reference_id"],
                    "additionalProperties": False,
                },
            },
            handler=_handle_read,
            description="Read an approved on-demand profile reference by ID.",
        )
