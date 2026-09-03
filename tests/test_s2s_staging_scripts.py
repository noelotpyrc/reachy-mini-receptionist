import os
import subprocess
import sys
from pathlib import Path


def test_s2s_staging_setup_is_isolated_and_pinned(tmp_path: Path) -> None:
    backend_dir = tmp_path / "staging-backend"
    wrapper = Path("scripts/m1max/setup_s2s_backend_staging.sh").read_text(
        encoding="utf-8"
    )
    result = subprocess.run(
        [
            "bash",
            "scripts/m1max/setup_s2s_backend_staging.sh",
            "--dry-run",
            "--python",
            sys.executable,
            "--skip-running-check",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "S2S_STAGING_BACKEND_DIR": str(backend_dir),
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"backend_dir={backend_dir}" in result.stderr
    assert "speech-to-speech==0.2.12" in result.stderr
    assert "aaa7c75e1f16a6ccdcd902ea94af92e325ebd455" in result.stderr
    assert "/Users/leon/projects/speech_to_speech_backend_migration" in wrapper
    assert "S2S_STAGING_PORT:-8766" in wrapper
    assert not backend_dir.exists()


def test_s2s_staging_launcher_uses_new_cli_without_hermes_state(
    tmp_path: Path,
) -> None:
    backend_dir = tmp_path / "staging-backend"
    cli = backend_dir / ".venv" / "bin" / "speech-to-speech"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    cli.chmod(0o755)

    result = subprocess.run(
        ["bash", "scripts/m1max/run_s2s_backend_staging.sh"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "S2S_STAGING_BACKEND_DIR": str(backend_dir),
            "ENV_FILE": str(tmp_path / "missing.env"),
            "S2S_STAGING_PORT": "65433",
            "OPENROUTER_API_KEY": "test-only-key",
            "S2S_RESPONSES_CONVERSATION": "1",
            "S2S_RESPONSES_BASE_URL": "http://127.0.0.1:8642/v1",
        },
    )

    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[:5] == ["serve", "--host", "127.0.0.1", "--port", "65433"]
    assert "--no_smart_turn" in args
    assert (
        args[args.index("--responses_api_base_url") + 1]
        == "https://openrouter.ai/api/v1"
    )
    assert "--responses_api_conversation" not in args
    assert "--responses_api_direct_base_url" not in args
    assert "test-only-key" not in result.stdout
