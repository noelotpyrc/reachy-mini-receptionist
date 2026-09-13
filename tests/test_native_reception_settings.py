"""Private configuration tests without SDK or physical connections."""

import asyncio
import json
import ssl
import threading

import pytest

from reachy_mini_reception_app.client import control_session
from reachy_mini_reception_app.settings import load_settings

TOKEN = "test-only-token-0123456789-abcdef0123456789"


def config(tmp_path, **changes):
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    path = tmp_path / "config.json"
    data = dict(service_url="wss://service.example/reception/control", token_file="token",
                config_id="av-probe", robot_id="test-robot")
    data.update(changes)
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def test_private_config_loads_relative_token_and_verifying_tls(tmp_path):
    settings = load_settings(config(tmp_path), environ={})
    assert settings.token == TOKEN
    assert TOKEN not in repr(settings)
    assert settings.tls.check_hostname
    assert settings.tls.verify_mode == ssl.CERT_REQUIRED
    assert settings.tls.minimum_version == ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize("filename", ["token", "config.json"])
def test_rejects_public_private_files(tmp_path, filename):
    path = config(tmp_path)
    (tmp_path / filename).chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        load_settings(path, environ={})


def test_environment_compatibility_and_xdg_path(tmp_path):
    directory = tmp_path / "reachy-mini-reception"
    directory.mkdir()
    config(directory)
    settings = load_settings(environ={"XDG_CONFIG_HOME": str(tmp_path), "RECEPTION_CONFIG_ID": "mock"})
    assert settings.config_id == "mock"
    settings = load_settings(tmp_path / "absent.json", environ={
        "RECEPTION_SERVICE_URL": "ws://127.0.0.1/reception/control",
        "RECEPTION_CONTROL_TOKEN": TOKEN, "RECEPTION_CONFIG_ID": "mock",
        "RECEPTION_ROBOT_ID": "test-robot",
    })
    assert settings.tls is None


@pytest.mark.parametrize("changes", [
    {"service_url": "ws://remote/reception/control"}, {"config_id": 1},
    {"unknown_field": "value"}, {"token_file": ""},
])
def test_invalid_configuration_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        load_settings(config(tmp_path, **changes), environ={})


def test_bad_json_does_not_expose_secret(tmp_path):
    path = config(tmp_path)
    path.write_text("{secret:" + TOKEN)
    with pytest.raises(ValueError) as exc:
        load_settings(path, environ={})
    assert TOKEN not in str(exc.value)


def test_explicit_ca_is_used(tmp_path, monkeypatch):
    path = config(tmp_path, tls_ca_file="ca.pem")
    real_context = ssl.create_default_context()
    calls = []

    def create(*, cafile):
        calls.append(cafile)
        return real_context

    monkeypatch.setattr(ssl, "create_default_context", create)
    assert load_settings(path, environ={}).tls is real_context
    assert calls == [str(tmp_path / "ca.pem")]


def test_unverifying_tls_context_is_rejected():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="verify"):
        asyncio.run(control_session(
            url="wss://service.example/reception/control", token=TOKEN,
            config_id="mock", robot_id="test-robot", tls=context,
            stop_event=threading.Event(), on_status=lambda _: None,
        ))
