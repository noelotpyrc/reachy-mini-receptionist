import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["serve", "legacy"])
@pytest.mark.parametrize("instruction", ["", "Speak warmly, with short natural pauses."])
def test_production_launcher_passes_optional_tts_instruction(
    tmp_path: Path, mode: str, instruction: str,
) -> None:
    backend = tmp_path / "backend"
    cli = backend / ".venv" / "bin" / "speech-to-speech"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    cli.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    nc = fake_bin / "nc"
    nc.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    nc.chmod(0o755)
    result = subprocess.run(
        ["bash", "scripts/m1max/run_s2s_backend.sh"],
        check=False, capture_output=True, text=True,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "BACKEND_DIR": str(backend),
            "S2S_ENV_LOADED": "1",
            "S2S_CLI_MODE": mode,
            "OPENROUTER_API_KEY": "test-only-key",
            "S2S_TTS_INSTRUCT": instruction,
        },
    )
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[args.index("--qwen3_tts_speaker") + 1] == "Sohee"
    if instruction:
        assert args.count("--qwen3_tts_instruct") == 1
        assert args[args.index("--qwen3_tts_instruct") + 1] == instruction
    else:
        assert "--qwen3_tts_instruct" not in args
    assert "test-only-key" not in result.stdout


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
    assert "2e4449c345c305e4ee6b9761f86c1849bbf3cb08" in result.stderr
    assert "mlx==0.31.1" in result.stderr
    assert "mlx-audio==0.4.2" in result.stderr
    assert "mlx-lm==0.31.1" in result.stderr
    assert "mlx-metal==0.31.1" in result.stderr
    assert "/Users/leon/projects/speech_to_speech_backend_migration" in wrapper
    assert "S2S_STAGING_PORT:-8766" in wrapper
    assert not backend_dir.exists()


def test_s2s_setup_bootstraps_unseeded_uv_venv_without_python_pip(
    tmp_path: Path,
) -> None:
    backend_dir = tmp_path / "staging-backend"
    uv_calls = tmp_path / "uv-calls.txt"
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$UV_CALLS\"\n"
        "if [[ \"$1\" == venv ]]; then\n"
        "  mkdir -p \"${@: -1}/bin\"\n"
        "  cp \"$PYTHON\" \"${@: -1}/bin/python\"\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            "scripts/m1max/setup_s2s_backend_staging.sh",
            "--dry-run",
            "--uv",
            str(fake_uv),
            "--skip-running-check",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "S2S_STAGING_BACKEND_DIR": str(backend_dir),
            "UV_CALLS": str(uv_calls),
            "PYTHON": sys.executable,
        },
    )

    assert result.returncode == 0, result.stderr
    assert "pip install --python" in result.stderr
    assert "python -m pip" not in result.stderr


def test_s2s_staging_launcher_uses_new_cli_without_hermes_state(
    tmp_path: Path,
) -> None:
    backend_dir = tmp_path / "staging-backend"
    cli = backend_dir / ".venv" / "bin" / "speech-to-speech"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    cli.chmod(0o755)
    env_file = tmp_path / "production.env"
    env_file.write_text(
        "OPENROUTER_API_KEY=test-only-key\n"
        "S2S_HOST=0.0.0.0\n"
        "S2S_PORT=8765\n"
        "S2S_MODEL_NAME=wrapper-routed\n"
        "S2S_RESPONSES_BASE_URL=http://127.0.0.1:8642/v1\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", "scripts/m1max/run_s2s_backend_staging.sh"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "S2S_STAGING_BACKEND_DIR": str(backend_dir),
            "ENV_FILE": str(env_file),
            "S2S_STAGING_PORT": "65433",
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
    assert args[args.index("--model_name") + 1] == "openai/gpt-5.6-luna"
    assert "--responses_api_conversation" not in args
    assert "--responses_api_direct_base_url" not in args
    assert "--log_transcripts" not in args
    assert "--event_trace_dir" not in args
    assert "test-only-key" not in result.stdout


def test_s2s_staging_launcher_can_enable_provider_payload_tracing(
    tmp_path: Path,
) -> None:
    backend_dir = tmp_path / "staging-backend"
    cli = backend_dir / ".venv" / "bin" / "speech-to-speech"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    cli.chmod(0o755)
    env_file = tmp_path / "staging.env"
    env_file.write_text("OPENROUTER_API_KEY=test-only-key\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", "scripts/m1max/run_s2s_backend_staging.sh"],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "S2S_STAGING_BACKEND_DIR": str(backend_dir),
            "ENV_FILE": str(env_file),
            "S2S_STAGING_PORT": "65434",
            "S2S_STAGING_LOG_LEVEL": "debug",
            "S2S_STAGING_LOG_TRANSCRIPTS": "1",
            "S2S_STAGING_EVENT_TRACE_DIR": str(tmp_path / "backend-trace"),
        },
    )

    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[args.index("--log_level") + 1] == "debug"
    assert "--log_transcripts" in args
    assert args[args.index("--event_trace_dir") + 1] == str(tmp_path / "backend-trace")
