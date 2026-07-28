from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import yaml


def _load_plugin() -> ModuleType:
    path = Path("hermes_plugins/reference_library/__init__.py")
    spec = importlib.util.spec_from_file_location("reference_library_plugin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_catalog(root: Path, references: dict | None = None) -> Path:
    catalog = root / "catalog.yaml"
    data = {
        "version": 1,
        "references": references
        or {
            "clinic.facts": {
                "path": "clinic_facts.md",
                "title": "Clinic facts",
                "summary": "Hours and directions.",
                "delivery": "on_demand",
                "tags": ["clinic", "hours"],
                "audience": "visitor",
                "max_bytes": 1024,
            }
        },
    }
    catalog.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return catalog


def test_reference_catalog_and_read(monkeypatch, tmp_path: Path) -> None:
    plugin = _load_plugin()
    catalog = _write_catalog(tmp_path)
    (tmp_path / "clinic_facts.md").write_text("Open weekdays.\n", encoding="utf-8")
    monkeypatch.setenv(plugin.CATALOG_ENV, str(catalog))

    catalog_result = json.loads(plugin.reference_catalog("hours"))
    assert catalog_result["references"] == [
        {
            "reference_id": "clinic.facts",
            "title": "Clinic facts",
            "summary": "Hours and directions.",
            "tags": ["clinic", "hours"],
            "audience": "visitor",
        }
    ]

    read_result = json.loads(plugin.reference_read("clinic.facts"))
    assert read_result["reference_id"] == "clinic.facts"
    assert read_result["content"] == "Open weekdays.\n"
    assert len(read_result["sha256"]) == 64
    assert "path" not in read_result


def test_prompt_references_are_not_exposed_as_tools(
    monkeypatch, tmp_path: Path
) -> None:
    plugin = _load_plugin()
    catalog = _write_catalog(
        tmp_path,
        {
            "clinic.facts": {
                "path": "clinic_facts.md",
                "title": "Clinic facts",
                "summary": "Hours and directions.",
                "delivery": "prompt",
                "tags": ["clinic", "hours"],
                "audience": "visitor",
                "max_bytes": 1024,
            }
        },
    )
    (tmp_path / "clinic_facts.md").write_text("Open weekdays.\n", encoding="utf-8")
    monkeypatch.setenv(plugin.CATALOG_ENV, str(catalog))
    registrations: list[dict] = []

    class Context:
        def register_tool(self, **kwargs):
            registrations.append(kwargs)

    assert json.loads(plugin.reference_catalog()) == {"references": []}
    assert "unknown reference ID" in json.loads(
        plugin.reference_read("clinic.facts")
    )["error"]

    plugin.register(Context())
    assert [item["name"] for item in registrations] == ["reference_catalog"]


def test_reference_read_rejects_path_escape(monkeypatch, tmp_path: Path) -> None:
    plugin = _load_plugin()
    root = tmp_path / "references"
    root.mkdir()
    (tmp_path / "private.txt").write_text("private\n", encoding="utf-8")
    catalog = _write_catalog(
        root,
        {
            "bad.reference": {
                "path": "../private.txt",
                "title": "Bad",
                "summary": "Must not escape.",
                "delivery": "on_demand",
                "tags": ["bad"],
                "audience": "visitor",
                "max_bytes": 1024,
            }
        },
    )
    monkeypatch.setenv(plugin.CATALOG_ENV, str(catalog))

    result = json.loads(plugin.reference_read("bad.reference"))
    assert "outside the reference root" in result["error"]
    assert "private" not in result["error"]


def test_reference_catalog_rejects_non_visitor_audience(
    monkeypatch, tmp_path: Path
) -> None:
    plugin = _load_plugin()
    catalog = _write_catalog(
        tmp_path,
        {
            "staff.notes": {
                "path": "staff.md",
                "title": "Staff notes",
                "summary": "Private operations.",
                "delivery": "on_demand",
                "tags": ["staff"],
                "audience": "staff",
                "max_bytes": 1024,
            }
        },
    )
    monkeypatch.setenv(plugin.CATALOG_ENV, str(catalog))

    result = json.loads(plugin.reference_catalog())
    assert "unsupported audience" in result["error"]


def test_reference_read_enforces_size_limit(monkeypatch, tmp_path: Path) -> None:
    plugin = _load_plugin()
    catalog = _write_catalog(tmp_path)
    (tmp_path / "clinic_facts.md").write_text("x" * 1025, encoding="utf-8")
    monkeypatch.setenv(plugin.CATALOG_ENV, str(catalog))

    result = json.loads(plugin.reference_read("clinic.facts"))
    assert "exceeds its 1024 byte limit" in result["error"]


def test_storage_errors_do_not_expose_host_paths(monkeypatch, tmp_path: Path) -> None:
    plugin = _load_plugin()
    missing = tmp_path / "private" / "catalog.yaml"
    monkeypatch.setenv(plugin.CATALOG_ENV, str(missing))

    result = json.loads(plugin.reference_catalog())
    assert result == {"error": "reference library storage is unavailable"}
    assert str(tmp_path) not in result["error"]


def test_plugin_registers_only_read_only_reference_tools(
    monkeypatch, tmp_path: Path
) -> None:
    plugin = _load_plugin()
    catalog = _write_catalog(tmp_path)
    (tmp_path / "clinic_facts.md").write_text("Open weekdays.\n", encoding="utf-8")
    monkeypatch.setenv(plugin.CATALOG_ENV, str(catalog))
    registrations: list[dict] = []

    class Context:
        def register_tool(self, **kwargs):
            registrations.append(kwargs)

    plugin.register(Context())

    assert [item["name"] for item in registrations] == [
        "reference_catalog",
        "reference_read",
    ]
    assert {item["toolset"] for item in registrations} == {"reference_readonly"}
    assert registrations[1]["schema"]["parameters"]["properties"][
        "reference_id"
    ]["enum"] == ["clinic.facts"]
    serialized = json.dumps(registrations, default=str)
    assert "write_file" not in serialized
    assert "patch" not in serialized
    assert "filesystem path" in serialized
