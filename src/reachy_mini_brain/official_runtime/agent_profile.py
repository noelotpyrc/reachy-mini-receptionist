"""Deterministic receptionist profile composition and read-only references."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from reachy_mini_brain.profile_context import compose_context_document


MAX_PROFILE_CHARS = 20_000
DEFAULT_TIMEZONE = "America/New_York"
MAX_CATALOG_ENTRIES = 128
MAX_REFERENCE_BYTES = 64 * 1024
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
REFERENCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class AgentProfileError(ValueError):
    """Raised when profile material violates the application policy."""


@dataclass(frozen=True, slots=True)
class ReferenceEntry:
    reference_id: str
    title: str
    summary: str
    delivery: str
    tags: tuple[str, ...]
    audience: str
    max_bytes: int
    path: Path
    source_id: str


class ReferenceStore:
    """Read visitor-safe documents through catalog IDs, never model paths."""

    def __init__(self, entries: dict[str, ReferenceEntry]) -> None:
        self._entries = dict(entries)

    @classmethod
    def load(cls, catalog_path: Path, *, source_scope: str) -> "ReferenceStore":
        root = catalog_path.parent.resolve(strict=True)
        try:
            raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
        except UnicodeDecodeError as exc:
            raise AgentProfileError("reference catalog must be UTF-8") from exc
        except yaml.YAMLError as exc:
            raise AgentProfileError("reference catalog contains invalid YAML") from exc
        except OSError as exc:
            raise AgentProfileError("reference catalog is unavailable") from exc

        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise AgentProfileError("reference catalog must be a version 1 mapping")
        references = raw.get("references")
        if not isinstance(references, dict):
            raise AgentProfileError("reference catalog requires a references mapping")
        if len(references) > MAX_CATALOG_ENTRIES:
            raise AgentProfileError(
                f"reference catalog exceeds {MAX_CATALOG_ENTRIES} entries"
            )

        entries: dict[str, ReferenceEntry] = {}
        for reference_id, raw_entry in references.items():
            if not isinstance(reference_id, str) or not REFERENCE_ID_RE.fullmatch(
                reference_id
            ):
                raise AgentProfileError(f"invalid reference ID: {reference_id!r}")
            if not isinstance(raw_entry, dict):
                raise AgentProfileError(
                    f"reference {reference_id!r} must be a mapping"
                )
            entries[reference_id] = _parse_reference_entry(
                reference_id,
                raw_entry,
                root=root,
                source_scope=source_scope,
            )
        return cls(entries)

    def on_demand_ids(self) -> list[str]:
        return sorted(
            reference_id
            for reference_id, entry in self._entries.items()
            if entry.delivery == "on_demand"
        )

    def prompt_sections(self) -> list[tuple[str, str, str]]:
        sections = []
        for entry in self._entries.values():
            if entry.delivery == "prompt":
                sections.append(
                    (entry.title, self._read_content(entry), entry.source_id)
                )
        return sections

    def catalog(self, topic: str = "") -> dict[str, Any]:
        query = topic.strip().casefold()
        if len(query) > 128:
            raise AgentProfileError("catalog topic must be at most 128 characters")
        items = []
        for reference_id in self.on_demand_ids():
            entry = self._entries[reference_id]
            searchable = " ".join(
                [reference_id, entry.title, entry.summary, *entry.tags]
            ).casefold()
            if query and query not in searchable:
                continue
            items.append(
                {
                    "reference_id": reference_id,
                    "title": entry.title,
                    "summary": entry.summary,
                    "tags": list(entry.tags),
                    "audience": entry.audience,
                }
            )
        return {"references": items}

    def read(self, reference_id: str) -> dict[str, Any]:
        entry = self._entries.get(reference_id)
        if entry is None or entry.delivery != "on_demand":
            raise AgentProfileError(f"unknown reference ID: {reference_id!r}")
        content = self._read_content(entry)
        content_bytes = content.encode("utf-8")
        return {
            "reference_id": reference_id,
            "title": entry.title,
            "audience": entry.audience,
            "sha256": hashlib.sha256(content_bytes).hexdigest(),
            "content": content,
        }

    @staticmethod
    def _read_content(entry: ReferenceEntry) -> str:
        try:
            content_bytes = entry.path.read_bytes()
        except OSError as exc:
            raise AgentProfileError(
                f"reference {entry.reference_id!r} is unavailable"
            ) from exc
        if len(content_bytes) > entry.max_bytes:
            raise AgentProfileError(
                f"reference {entry.reference_id!r} exceeds its "
                f"{entry.max_bytes} byte limit"
            )
        try:
            return content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AgentProfileError(
                f"reference {entry.reference_id!r} must be UTF-8 text"
            ) from exc


@dataclass(frozen=True, slots=True)
class ComposedAgentProfile:
    profile_id: str
    instructions: str
    sha256: str
    source_ids: tuple[str, ...]
    reference_store: ReferenceStore

    @property
    def chars(self) -> int:
        return len(self.instructions)

    def provenance(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "instructions_source": f"profile:{self.profile_id}",
            "instructions_sha256": self.sha256,
            "instructions_chars": self.chars,
            "profile_source_ids": list(self.source_ids),
        }


def with_session_date(
    profile: ComposedAgentProfile, *, now: datetime | None = None
) -> ComposedAgentProfile:
    """Append a clock-derived date snapshot to a freshly assembled profile."""
    instant = now if now is not None else datetime.now(timezone.utc)
    if instant.utcoffset() is None:
        raise AgentProfileError("session date requires a timezone-aware clock")
    local = instant.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    instructions = (
        f"{profile.instructions.rstrip()}\n\n# Current date context\n\n"
        f"Local date at instruction assembly: {local.date().isoformat()} "
        f"({local.strftime('%A')}).\n"
        f"Local timezone: {DEFAULT_TIMEZONE} ({local.tzname()}).\n"
        "Use this date to interpret requests such as today, latest, and recent. "
        "This is a date snapshot, not a live clock. Use time_now when available "
        "for exact time or when the local date may have changed.\n"
    )
    if len(instructions) > MAX_PROFILE_CHARS:
        raise AgentProfileError(
            f"composed profile exceeds the {MAX_PROFILE_CHARS} character limit"
        )
    return replace(
        profile,
        instructions=instructions,
        sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        source_ids=(*profile.source_ids, "runtime:local_date"),
    )


def compose_agent_profile(
    *,
    profile_id: str,
    public_dir: Path,
    private_dir: Path | None = None,
    source_format: str = "overlay",
) -> ComposedAgentProfile:
    """Compose public defaults and private overrides without logging their text."""

    if source_format == "hermes":
        if private_dir is None:
            raise AgentProfileError("Hermes-source profiles require a private source directory")
        return compose_hermes_agent_profile(
            profile_id=profile_id,
            source_dir=private_dir,
            soul_path=private_dir / "personality.md",
            session_instructions_path=public_dir / "session_instructions.txt",
        )
    if source_format != "overlay":
        raise AgentProfileError(f"unknown profile source format: {source_format!r}")
    if not PROFILE_ID_RE.fullmatch(profile_id):
        raise AgentProfileError(f"invalid profile ID: {profile_id!r}")
    public_root = _resolve_directory(public_dir, label="public profile")
    private_root = (
        _resolve_directory(private_dir, label="private profile")
        if private_dir is not None
        else None
    )

    sections: list[tuple[str, str]] = []
    source_ids: list[str] = []
    for filename, title in (
        ("instructions.txt", "Core instructions"),
        ("personality.md", "Personality"),
        ("session_instructions.txt", "Session behavior"),
    ):
        path, source_id = _select_profile_file(
            filename,
            public_root=public_root,
            private_root=private_root,
        )
        sections.append((title, _read_profile_text(path, source_id=source_id)))
        source_ids.append(source_id)

    catalog_path, catalog_source_id = _select_profile_file(
        "reference_catalog.yaml",
        public_root=public_root,
        private_root=private_root,
    )
    catalog_scope = catalog_source_id.split(":", 1)[0]
    reference_store = ReferenceStore.load(
        catalog_path,
        source_scope=catalog_scope,
    )
    source_ids.append(catalog_source_id)
    for title, content, source_id in reference_store.prompt_sections():
        sections.append((title, content))
        source_ids.append(source_id)

    rendered = "\n\n".join(
        f"## {title}\n\n{_strip_matching_heading(content, title)}"
        for title, content in sections
    ).strip()
    rendered += "\n"
    if len(rendered) > MAX_PROFILE_CHARS:
        raise AgentProfileError(
            f"composed profile exceeds the {MAX_PROFILE_CHARS} character limit"
        )
    return ComposedAgentProfile(
        profile_id=profile_id,
        instructions=rendered,
        sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        source_ids=tuple(source_ids),
        reference_store=reference_store,
    )


def compose_hermes_agent_profile(
    *,
    profile_id: str,
    source_dir: Path,
    soul_path: Path,
    session_instructions_path: Path,
) -> ComposedAgentProfile:
    """Assemble existing Hermes sources for S2S without registering any tools.

    source_dir contains the base HERMES.md, not its deployed generated copy.
    No public-profile fallback is used for this migration path.
    """

    if not PROFILE_ID_RE.fullmatch(profile_id):
        raise AgentProfileError(f"invalid profile ID: {profile_id!r}")
    root = _resolve_directory(source_dir, label="Hermes profile source")
    base_id = "private:HERMES.md"
    base_path = _confined_profile_file(root / "HERMES.md", root=root, source_id=base_id)
    catalog_path = _confined_profile_file(
        root / "reference_catalog.yaml",
        root=root,
        source_id="private:reference_catalog.yaml",
    )
    store = ReferenceStore.load(catalog_path, source_scope="private")
    prompt_sections = store.prompt_sections()
    try:
        context = compose_context_document(
            _read_profile_text(base_path, source_id=base_id),
            [(title, content) for title, content, _ in prompt_sections],
        )
    except ValueError as exc:
        raise AgentProfileError(str(exc)) from exc
    soul = _read_profile_text(soul_path, source_id="private:SOUL.md")
    spoken = _read_profile_text(
        session_instructions_path, source_id="session:spoken_instructions"
    )
    instructions = (
        f"{soul}\n\n{context.rstrip()}\n\n"
        f"# Spoken-response instructions\n\n{spoken}\n"
    )
    if len(instructions) > MAX_PROFILE_CHARS:
        raise AgentProfileError(
            f"composed profile exceeds the {MAX_PROFILE_CHARS} character limit"
        )
    return ComposedAgentProfile(
        profile_id=profile_id,
        instructions=instructions,
        sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        source_ids=(
            "private:SOUL.md",
            base_id,
            "private:reference_catalog.yaml",
            *(source_id for _, _, source_id in prompt_sections),
            "session:spoken_instructions",
        ),
        reference_store=store,
    )


def _resolve_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise AgentProfileError(f"{label} directory is unavailable") from exc
    if not resolved.is_dir():
        raise AgentProfileError(f"{label} is not a directory")
    return resolved


def _select_profile_file(
    filename: str,
    *,
    public_root: Path,
    private_root: Path | None,
) -> tuple[Path, str]:
    if private_root is not None:
        private_candidate = private_root / filename
        if private_candidate.is_file():
            return (
                _confined_profile_file(
                    private_candidate,
                    root=private_root,
                    source_id=f"private:{filename}",
                ),
                f"private:{filename}",
            )
    public_candidate = public_root / filename
    if public_candidate.is_file():
        return (
            _confined_profile_file(
                public_candidate,
                root=public_root,
                source_id=f"public:{filename}",
            ),
            f"public:{filename}",
        )
    raise AgentProfileError(f"profile requires {filename!r}")


def _confined_profile_file(path: Path, *, root: Path, source_id: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AgentProfileError(f"{source_id} resolves outside its profile root") from exc
    if not resolved.is_file():
        raise AgentProfileError(f"{source_id} is not a regular file")
    return resolved


def _read_profile_text(path: Path, *, source_id: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise AgentProfileError(f"{source_id} must be UTF-8") from exc
    except OSError as exc:
        raise AgentProfileError(f"{source_id} is unavailable") from exc
    if not text:
        raise AgentProfileError(f"{source_id} must not be empty")
    return text


def _parse_reference_entry(
    reference_id: str,
    raw: dict[str, Any],
    *,
    root: Path,
    source_scope: str,
) -> ReferenceEntry:
    relative_path = raw.get("path")
    title = raw.get("title")
    summary = raw.get("summary")
    audience = raw.get("audience")
    delivery = raw.get("delivery")
    tags = raw.get("tags", [])
    max_bytes = raw.get("max_bytes", MAX_REFERENCE_BYTES)
    if (
        not isinstance(relative_path, str)
        or not relative_path.strip()
        or Path(relative_path).is_absolute()
    ):
        raise AgentProfileError(
            f"reference {reference_id!r} requires a relative path"
        )
    if not isinstance(title, str) or not title.strip():
        raise AgentProfileError(f"reference {reference_id!r} requires a title")
    if not isinstance(summary, str) or not summary.strip():
        raise AgentProfileError(f"reference {reference_id!r} requires a summary")
    if audience != "visitor":
        raise AgentProfileError(
            f"reference {reference_id!r} has unsupported audience {audience!r}"
        )
    if delivery not in {"prompt", "on_demand"}:
        raise AgentProfileError(
            f"reference {reference_id!r} delivery must be 'prompt' or 'on_demand'"
        )
    if not isinstance(tags, list) or not all(
        isinstance(tag, str) and tag.strip() for tag in tags
    ):
        raise AgentProfileError(
            f"reference {reference_id!r} tags must be non-empty strings"
        )
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 1 <= max_bytes <= MAX_REFERENCE_BYTES
    ):
        raise AgentProfileError(
            f"reference {reference_id!r} max_bytes must be between 1 and "
            f"{MAX_REFERENCE_BYTES}"
        )

    try:
        candidate = (root / relative_path).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AgentProfileError(
            f"reference {reference_id!r} is unavailable or outside its profile root"
        ) from exc
    if not candidate.is_file():
        raise AgentProfileError(
            f"reference {reference_id!r} is not a regular file"
        )
    return ReferenceEntry(
        reference_id=reference_id,
        title=title.strip(),
        summary=summary.strip(),
        delivery=delivery,
        tags=tuple(tag.strip() for tag in tags),
        audience=audience,
        max_bytes=max_bytes,
        path=candidate,
        source_id=f"{source_scope}:{relative_path}",
    )


def _strip_matching_heading(content: str, title: str) -> str:
    lines = content.strip().splitlines()
    if (
        lines
        and lines[0].startswith("# ")
        and lines[0][2:].strip().casefold() == title.strip().casefold()
    ):
        lines = lines[1:]
    return "\n".join(lines).strip()
