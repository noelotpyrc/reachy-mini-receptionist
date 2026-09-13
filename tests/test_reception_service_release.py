"""Status and inert packaging tests; no robot, launchctl, or provider calls."""

import asyncio
import json
import plistlib
from types import SimpleNamespace
import zipfile

import pytest

from reachy_mini_brain.reception_package import LABEL, prepare_bundle
from reachy_mini_brain.reception_service import SessionController, mock_runtime
from reachy_mini_brain.reception_status import ReceptionStatus
from reachy_mini_reception_app.protocol import Command


def options(root):
    return dict(robot_host="192.0.2.10", artifact_root=str(root / "artifacts"),
                hf_realtime_ws_url="ws://127.0.0.1:8765/v1/realtime", agent_profile_id="test",
                agent_profile_format="overlay", agent_tools="time-web", visitor_trigger_profile="legacy")


def test_public_status_does_not_expose_private_fields(tmp_path):
    status = ReceptionStatus({**options(tmp_path), "token": "private-token", "instructions": "private-text"})
    status.begin("first")
    status.update({"phase": "ready", "fault": "private-exception", "event_loop_age_s": 0.1,
                   "audio": {"expected": True, "sequence": 50, "age_s": 0.01, "private": "secret"}},
                  fault="runtime_failed:private-exception")
    snapshot = status.snapshot("first")
    assert snapshot["configuration"]["record_video"] is False
    assert snapshot["health"]["fault"] == "runtime_failed"
    assert "private" not in json.dumps(snapshot)
    assert snapshot["health"]["audio"]["sequence"] == 50
    snapshot["health"]["audio"]["sequence"] = 0
    assert status.snapshot("first")["health"]["audio"]["sequence"] == 50
    status.begin("second")
    assert status.snapshot("first")["health"] == {}


def test_receipt_freezes_terminal_elapsed_and_contains_no_credentials(tmp_path):
    async def exercise():
        service = SessionController({"test": ("robot", mock_runtime)}, receipt_dir=tmp_path)
        run = service.start(object(), Command("start", "receipt", "test", "robot"))
        await asyncio.sleep(0.01)
        await service.stop(run, "native_stop")
        elapsed = run.snapshot()["elapsed_s"]
        await asyncio.sleep(0.02)
        assert run.snapshot()["elapsed_s"] == elapsed
        path = tmp_path / "native-receipt.json"
        record = json.loads(path.read_text())
        assert record["phase"] == "stopped" and record["reason"] == "native_stop"
        assert record["run_id"] == "native-receipt"
        assert path.stat().st_mode & 0o077 == 0

    asyncio.run(exercise())


def test_receipt_write_failure_does_not_prevent_stop(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("test")

    async def exercise():
        service = SessionController({"test": ("robot", mock_runtime)}, receipt_dir=blocked)
        run = service.start(object(), Command("start", "no-disk", "test", "robot"))
        await asyncio.sleep(0)
        await service.stop(run, "native_stop")
        assert run.phase == "stopped"

    asyncio.run(exercise())


@pytest.fixture
def bundle_inputs(tmp_path, monkeypatch):
    repo = tmp_path / "release"
    (repo / "src/reachy_mini_brain").mkdir(parents=True)
    (repo / "uv.lock").write_text("test lock")
    (repo / "src/reachy_mini_brain/reception_service.py").write_text("test service")
    python = repo / ".release-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("test")
    python.chmod(0o700)
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"schema_version": 1, "options": options(tmp_path)}))
    files = {}
    for name in ("env_file", "token_file", "key", "cert", "ca"):
        files[name] = tmp_path / name
        files[name].write_text("test-only-token-abcdefghijklmnopqrstuvwxyz0123456789" if name == "token_file" else "private-value")
        files[name].chmod(0o600)
    wheel = tmp_path / "native.whl"
    (repo / "src/reachy_mini_reception_app").mkdir()
    (repo / "src/reachy_mini_reception_app/main.py").write_text("test")
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("reachy_mini_reception_app/main.py", "test")
        archive.writestr("native.dist-info/entry_points.txt", "[reachy_mini_apps]\nreachy_mini_reception_app = reachy_mini_reception_app.main:ReceptionApp")
    from reachy_mini_brain import reception_package as package

    calls = []
    monkeypatch.setattr(package.ssl, "SSLContext", lambda *_: SimpleNamespace(load_cert_chain=lambda *_: None))
    monkeypatch.setattr(package.ssl, "create_default_context", lambda **_: None)
    monkeypatch.setattr(package.shutil, "which", lambda _: "/test/openssl")
    monkeypatch.setattr(package.subprocess, "run", lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(package.subprocess, "check_output", lambda args, **kwargs: "a" * 40 if "rev-parse" in args else "")
    return dict(output=tmp_path / "bundle", repo=repo, python=python, runtime_config=runtime,
                wheel=wheel, host="192.0.2.2", robot_id="robot", config_id="candidate", **files), calls


def test_bundle_is_inert_separate_interactive_and_secret_free(bundle_inputs):
    inputs, calls = bundle_inputs
    result = prepare_bundle(**inputs)
    assert result["started"] is False and result["installed"] is False
    plist = plistlib.loads((inputs["output"] / f"{LABEL}.plist").read_bytes())
    assert plist["ProcessType"] == "Interactive"
    assert plist["RunAtLoad"] is False and plist["KeepAlive"] is False
    assert plist["ExitTimeOut"] == 15
    assert plist["ProgramArguments"][0] == str(inputs["python"])
    assert "--mode" in plist["ProgramArguments"] and "reception" in plist["ProgramArguments"]
    assert "GST_PLUGIN_PATH" not in plist["EnvironmentVariables"]
    assert "private-value" not in json.dumps(plist)
    assert all(command[0] == "/test/openssl" for command in calls)
    robot = json.loads((inputs["output"] / "robot-config.json").read_text())
    assert ":8877/" in robot["service_url"]
    assert set(robot) == {"service_url", "config_id", "robot_id", "token_file", "tls_ca_file"}
    assert set(p.name for p in inputs["output"].iterdir()) == {f"{LABEL}.plist", "bundle.json", "runtime.json", "robot-config.json"}
    with pytest.raises(FileExistsError):
        prepare_bundle(**inputs)


def test_dirty_release_rejected_before_any_output(bundle_inputs, monkeypatch):
    inputs, _ = bundle_inputs
    monkeypatch.setattr("reachy_mini_brain.reception_package.subprocess.check_output", lambda *_, **__: "modified")
    with pytest.raises(ValueError, match="Commit"):
        prepare_bundle(**inputs)
    assert not inputs["output"].exists()


def test_native_wheel_cannot_contain_server_or_private_packages(bundle_inputs):
    inputs, _ = bundle_inputs
    with zipfile.ZipFile(inputs["wheel"], "a") as archive:
        archive.writestr("reachy_mini_brain/reception_service.py", "test")
    with pytest.raises(ValueError, match="unexpected"):
        prepare_bundle(**inputs)
    assert not inputs["output"].exists()


def test_wheel_must_match_service_source_release(bundle_inputs):
    inputs, _ = bundle_inputs
    (inputs["repo"] / "src/reachy_mini_reception_app/main.py").write_text("different release")
    with pytest.raises(ValueError, match="does not match"):
        prepare_bundle(**inputs)


def test_native_diagnostics_participate_in_report_only_retention(tmp_path):
    import os
    from reachy_mini_brain.official_runtime.ops_core import recording_retention_report

    for name in ("native-service/release/service.stderr.log", "service-receipts/native-test.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True)
        path.write_text("test")
        os.utime(path, (1, 1))
    result = recording_retention_report(tmp_path, retention_days=30, now_ts=40 * 86400)
    assert result["due_file_count"] == 2
    assert result["deletion_performed"] is False
    assert all((tmp_path / item["path"]).exists() for item in result["due_files"])


def test_settings_page_and_assets_use_official_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from reachy_mini_reception_app import main
    from reachy_mini_reception_app.settings import Settings

    monkeypatch.setattr(main, "load_settings", lambda: Settings(
        "ws://127.0.0.1/reception/control", "test-token", "candidate", "robot"))
    app = main.ReceptionApp()
    app._on_status({"phase": "ready", "session_id": "test", "elapsed_s": 4})
    app._on_status({"phase": "stopping"})
    with TestClient(app.settings_app) as client:
        result = client.get("/api/reception/status").json()
        assert result["phase"] == "stopping"
        assert result["config_id"] == "candidate" and result["session_id"] == "test"
        assert "token" not in json.dumps(result)
        assert result["control_age_s"] >= 0
        page = client.get("/")
        assert page.status_code == 200 and "Runtime health" in page.text
        for asset in ("style.css", "status.js", "reachy-icon.png"):
            assert client.get(f"/static/{asset}").status_code == 200


def test_service_process_sigterm_closes_mock_session_and_writes_receipt(tmp_path):
    import signal
    import sys
    from websockets.asyncio.client import connect

    token = "test-only-token-abcdefghijklmnopqrstuvwxyz0123456789"
    token_file = tmp_path / "control.token"
    token_file.write_text(token)
    token_file.chmod(0o600)

    async def exercise():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "reachy_mini_brain.reception_service", "--mode", "mock",
            "--port", "0", "--token-file", str(token_file), "--receipt-dir", str(tmp_path / "receipts"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 8)
            port = int(line.decode().strip().rsplit(":", 1)[1])
            async with connect(f"ws://127.0.0.1:{port}/reception/control", proxy=None,
                               additional_headers={"Authorization": f"Bearer {token}"}) as websocket:
                await websocket.send(Command("start", "sigterm", "mock", "test-robot").encode())
                assert json.loads(await websocket.recv())["phase"] in {"starting", "ready"}
                process.send_signal(signal.SIGTERM)
                assert await asyncio.wait_for(process.wait(), 5) == 0
            receipt = json.loads((tmp_path / "receipts/native-sigterm.json").read_text())
            assert receipt["phase"] == "stopped"
            assert receipt["reason"] == "service_shutdown"
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(exercise())
