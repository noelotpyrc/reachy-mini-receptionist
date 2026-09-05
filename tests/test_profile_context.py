import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from reachy_mini_brain.official_runtime.agent_profile import (
    AgentProfileError,
    compose_hermes_agent_profile,
    compose_agent_profile,
    with_session_date,
)
from reachy_mini_brain.profile_context import compose_context_document


def _sources(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    (source / "HERMES.md").write_text("# Operating Context\n\nUse approved facts.\n")
    (source / "facts.md").write_text("# Clinic Facts\n\n## Hours\n\nOpen at nine.\n")
    (source / "parking.md").write_text("Optional parking reference.\n")
    entries = {}
    for key, delivery in (("facts", "prompt"), ("parking", "on_demand")):
        entries[key] = {
            "path": f"{key}.md",
            "title": f"Clinic {key}",
            "summary": f"Approved {key}.",
            "delivery": delivery,
            "audience": "visitor",
            "tags": [key],
        }
    (source / "reference_catalog.yaml").write_text(
        yaml.safe_dump({"version": 1, "references": entries}, sort_keys=False)
    )
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Identity\n\nYou are a calm receptionist.\n")
    spoken = tmp_path / "spoken.txt"
    spoken.write_text("Use short spoken answers.\n")
    return dict(
        profile_id="clinic-test",
        source_dir=source,
        soul_path=soul,
        session_instructions_path=spoken,
    )


def test_shared_renderer_preserves_hermes_sync_format():
    assert compose_context_document(
        "# Base\n\nInstructions.\n",
        [
            ("Clinic facts", "# Clinic Facts\n\n## Hours\n\nNine.\n"),
            ("Capabilities", "# Different heading\n\nAnswer questions.\n"),
        ],
    ) == (
        "# Base\n\nInstructions.\n\n## Clinic facts\n\n"
        "### Hours\n\nNine.\n\n## Capabilities\n\n"
        "## Different heading\n\nAnswer questions.\n"
    )


def test_hermes_sources_assemble_once_without_on_demand_content(tmp_path):
    args = _sources(tmp_path)
    profile = compose_hermes_agent_profile(**args)
    assert profile.instructions == (
        "# Identity\n\nYou are a calm receptionist.\n\n"
        "# Operating Context\n\nUse approved facts.\n\n"
        "## Clinic facts\n\n### Hours\n\nOpen at nine.\n\n"
        "# Spoken-response instructions\n\nUse short spoken answers.\n"
    )
    assert profile.sha256 == hashlib.sha256(profile.instructions.encode()).hexdigest()
    again = compose_hermes_agent_profile(**args)
    assert (profile.instructions, profile.sha256) == (again.instructions, again.sha256)
    assert profile.source_ids == (
        "private:SOUL.md",
        "private:HERMES.md",
        "private:reference_catalog.yaml",
        "private:facts.md",
        "session:spoken_instructions",
    )
    provenance = json.dumps(profile.provenance())
    assert str(tmp_path) not in provenance
    assert "Open at nine" not in provenance
    assert "Optional parking" not in profile.instructions


def test_final_size_limit_includes_soul_and_voice_rules(tmp_path):
    args = _sources(tmp_path)
    args["soul_path"].write_text("x" * 20_000)
    with pytest.raises(AgentProfileError, match="composed profile exceeds"):
        compose_hermes_agent_profile(**args)
    with pytest.raises(ValueError, match="generated HERMES.md exceeds"):
        compose_context_document("x" * 20_000, [])


def test_hermes_composer_rejects_path_escape(tmp_path):
    args = _sources(tmp_path)
    catalog = args["source_dir"] / "reference_catalog.yaml"
    data = yaml.safe_load(catalog.read_text())
    data["references"]["facts"]["path"] = "../SOUL.md"
    catalog.write_text(yaml.safe_dump(data))
    with pytest.raises(AgentProfileError, match="outside its profile root"):
        compose_hermes_agent_profile(**args)


def test_missing_hermes_base_does_not_fall_back_to_public_profile(tmp_path):
    args = _sources(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    args["source_dir"] = empty
    with pytest.raises(AgentProfileError, match="HERMES.md"):
        compose_hermes_agent_profile(**args)


def test_preview_cli_is_private_and_never_overwrites(tmp_path):
    args = _sources(tmp_path)
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "instructions.md"
    command = [
        sys.executable,
        str(root / "scripts/compose_s2s_profile.py"),
        "--tools",
        "none",
        "--profile-id",
        args["profile_id"],
        "--source-dir",
        str(args["source_dir"]),
        "--soul",
        str(args["soul_path"]),
        "--spoken-instructions",
        str(args["session_instructions_path"]),
        "--output",
        str(output),
    ]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    completed = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stderr
    profile = with_session_date(compose_hermes_agent_profile(**args))
    assert output.read_text() == profile.instructions
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(completed.stdout) == profile.provenance()
    repeated = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=30
    )
    assert repeated.returncode != 0
    assert output.read_text() == profile.instructions


@pytest.mark.parametrize(
    "instant, date, weekday, abbreviation",
    [
        ("2026-09-05T02:00:00+00:00", "2026-09-04", "Friday", "EDT"),
        ("2026-01-01T02:00:00+00:00", "2025-12-31", "Wednesday", "EST"),
    ],
)
def test_session_date_uses_new_york_clock_and_updates_provenance(
    tmp_path, instant, date, weekday, abbreviation
):
    base = compose_hermes_agent_profile(**_sources(tmp_path))
    dated = with_session_date(base, now=datetime.fromisoformat(instant))
    assert f"{date} ({weekday})" in dated.instructions
    assert f"America/New_York ({abbreviation})" in dated.instructions
    assert "# Current date context" not in base.instructions
    assert dated.reference_store is base.reference_store
    assert dated.source_ids == (*base.source_ids, "runtime:local_date")
    assert dated.sha256 != base.sha256
    assert dated.sha256 == hashlib.sha256(dated.instructions.encode()).hexdigest()
    assert dated == with_session_date(base, now=datetime.fromisoformat(instant))


def test_session_date_uses_runtime_clock_by_default(tmp_path, monkeypatch):
    import reachy_mini_brain.official_runtime.agent_profile as module

    class Clock:
        @staticmethod
        def now(zone):
            assert zone is timezone.utc
            return datetime(2026, 9, 5, 2, tzinfo=timezone.utc)

    monkeypatch.setattr(module, "datetime", Clock)
    dated = with_session_date(compose_hermes_agent_profile(**_sources(tmp_path)))
    assert "2026-09-04 (Friday)" in dated.instructions


def test_session_date_rejects_naive_clock_and_checks_final_size(tmp_path):
    base = compose_hermes_agent_profile(**_sources(tmp_path))
    with pytest.raises(AgentProfileError, match="timezone-aware"):
        with_session_date(base, now=datetime(2026, 9, 4))
    with pytest.raises(AgentProfileError, match="composed profile exceeds"):
        with_session_date(replace(base, instructions="x" * 20_000))


def test_live_hermes_profile_path_matches_review_composer(tmp_path):
    args = _sources(tmp_path)
    private = args["source_dir"]
    (private / "personality.md").write_text(args["soul_path"].read_text())
    public = tmp_path / "public"
    public.mkdir()
    (public / "session_instructions.txt").write_text(args["session_instructions_path"].read_text())
    (public / "instructions.txt").write_text("Lakeside fictional fallback MUST NOT APPEAR")
    actual = compose_agent_profile(
        profile_id=args["profile_id"], public_dir=public,
        private_dir=private, source_format="hermes",
    )
    expected = compose_hermes_agent_profile(**args)
    assert actual.instructions == expected.instructions
    assert actual.sha256 == expected.sha256
    assert "Lakeside" not in actual.instructions
    with pytest.raises(AgentProfileError, match="private source directory"):
        compose_agent_profile(profile_id="clinic", public_dir=public, source_format="hermes")


@pytest.mark.parametrize("tool_args, expected_tools", [([], ["time_now", "web_search"]), (["--agent-tools", "none"], [])])
def test_live_startup_selects_hermes_sources_and_default_tools(tmp_path, monkeypatch, tool_args, expected_tools):
    from click.testing import CliRunner
    import reachy_mini_brain.official_runtime.live_app as app

    args = _sources(tmp_path)
    private = args["source_dir"]
    (private / "personality.md").write_text(args["soul_path"].read_text())
    public = tmp_path / "public"
    public.mkdir()
    (public / "session_instructions.txt").write_text(args["session_instructions_path"].read_text())
    captured = {}
    class StopBeforeRuntime(Exception):
        pass
    def recorder(*args, **kwargs):
        captured.update(kwargs["config"])
        raise StopBeforeRuntime()
    monkeypatch.setattr(app, "ArtifactRecorder", recorder)
    for key in ("RECEPTION_AGENT_TOOLS", "RECEPTION_AGENT_PROFILE_FORMAT"):
        monkeypatch.delenv(key, raising=False)
    result = CliRunner().invoke(app.cli, [
        "--backend", "s2s-local", "--agent-profile-id", "clinic-test",
        "--agent-profile-format", "hermes", "--agent-profile-private-dir", str(private),
        "--agent-profile-public-dir", str(public), *tool_args,
    ])
    assert isinstance(result.exception, StopBeforeRuntime), result.output
    assert captured["agent_profile"]["source_format"] == "hermes"
    assert captured["agent_profile"]["tool_names"] == expected_tools
    assert "runtime:local_date" in captured["profile_source_ids"]
