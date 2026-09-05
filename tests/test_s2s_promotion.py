import importlib.util
import subprocess

import pytest


def test_managed_new_cli_preserves_loaded_environment(tmp_path):
    backend = tmp_path / "backend"
    cli = backend / ".venv/bin/speech-to-speech"
    cli.parent.mkdir(parents=True)
    cli.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    cli.chmod(0o755)
    env_file = tmp_path / "old.env"
    env_file.write_text("S2S_RESPONSES_CONVERSATION=1\nS2S_MODEL_NAME=old-hermes\n")
    trace = tmp_path / "traces"
    result = subprocess.run(
        ["bash", "scripts/m1max/run_s2s_backend.sh"], capture_output=True, text=True,
        env={"PATH":"/usr/bin:/bin:/usr/sbin:/sbin", "BACKEND_DIR":str(backend),
             "ENV_FILE":str(env_file), "S2S_ENV_LOADED":"1", "S2S_CLI_MODE":"serve",
             "S2S_PORT":"65432", "S2S_RESPONSES_CONVERSATION":"0",
             "S2S_MODEL_NAME":"openai/gpt-5.6-luna", "OPENROUTER_API_KEY":"test-only",
             "S2S_RESPONSES_BASE_URL":"https://openrouter.ai/api/v1",
             "S2S_EVENT_TRACE_DIR":str(trace), "S2S_LOG_TRANSCRIPTS":"1"},
    )
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[:5] == ["serve", "--host", "127.0.0.1", "--port", "65432"]
    assert "--no_smart_turn" in args
    assert "--responses_api_conversation" not in args
    assert args[args.index("--model_name")+1] == "openai/gpt-5.6-luna"
    assert "--no_responses_api_disable_thinking" in args
    assert args[args.index("--event_trace_dir")+1] == str(trace)
    assert "--log_transcripts" in args
    assert "test-only" not in result.stdout


def test_backend_pin_verifier_rejects_drift(monkeypatch):
    spec = importlib.util.spec_from_file_location("verify_s2s", "scripts/m1max/verify_s2s_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in ("S2S_BACKEND_VERSION", "S2S_EXPECTED_MLX_VERSION", "S2S_EXPECTED_MLX_AUDIO_VERSION", "S2S_EXPECTED_MLX_LM_VERSION", "S2S_EXPECTED_MLX_METAL_VERSION"):
        monkeypatch.setenv(key, "expected")
    monkeypatch.setattr(module, "version", lambda name: "wrong")
    with pytest.raises(SystemExit, match="dependency mismatch"):
        module.main()
