"""Prepare an inert, private native-service deployment bundle. Never install/start it."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import shutil
import ssl
import subprocess
from typing import Any
import zipfile

from reachy_mini_reception_app.protocol import identifier, validate_token
from reachy_mini_reception_app.settings import read_private_file

from .reception_runtime import load_runtime_options

LABEL = "com.reachy.reception.native-candidate"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_bundle(*, output: Path, repo: Path, python: Path, runtime_config: Path,
                   env_file: Path, token_file: Path, cert: Path, key: Path, ca: Path,
                   wheel: Path, host: str, robot_id: str, config_id: str,
                   port: int = 8877) -> dict[str, Any]:
    """All inputs already exist. The only writes are in a new output directory."""
    host = str(ipaddress.ip_address(host))
    robot_id, config_id = identifier(robot_id), identifier(config_id)
    if not 1024 <= port <= 65535:
        raise ValueError("Select an unprivileged service port")
    repo = repo.expanduser().resolve(strict=True)
    # Resolving this symlink would lose venv isolation.
    python = python.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Runtime Python must exist and be executable")
    options = load_runtime_options(runtime_config)
    for name in ("agent_profile_public_dir", "agent_profile_private_dir", "vision_pipelines_config"):
        if name in options and not options[name].exists():
            raise ValueError(f"Configured {name} is unavailable")
    if options["agent_profile_format"] == "hermes" and "agent_profile_private_dir" not in options:
        raise ValueError("Hermes-source profile requires its private directory")
    read_private_file(env_file)
    validate_token(read_private_file(token_file).strip())
    read_private_file(key)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    ssl.create_default_context(cafile=str(ca))
    openssl = shutil.which("openssl")
    if openssl is None:
        raise ValueError("openssl is required to verify certificate trust, dates and service address")
    subprocess.run([openssl, "verify", "-CAfile", str(ca), "-verify_ip", host, str(cert)],
                   check=True, capture_output=True)
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        entrypoints = [name for name in members if name.endswith(".dist-info/entry_points.txt")]
        entries = configparser.ConfigParser()
        if len(entrypoints) == 1:
            entries.read_string(archive.read(entrypoints[0]).decode())
        if entries.get("reachy_mini_apps", "reachy_mini_reception_app", fallback=None) != "reachy_mini_reception_app.main:ReceptionApp":
            raise ValueError("Native wheel is missing official app discovery")
        if any(not (name.startswith("reachy_mini_reception_app/") or ".dist-info/" in name) for name in members):
            raise ValueError("Native wheel contains unexpected runtime packages")
        for name in members:
            if name.startswith("reachy_mini_reception_app/") and not name.endswith("/"):
                source = repo / "src" / name
                if not source.is_file() or source.read_bytes() != archive.read(name):
                    raise ValueError("Native wheel does not match the selected source release")
    lock = repo / "uv.lock"
    service = repo / "src/reachy_mini_brain/reception_service.py"
    if not lock.is_file() or not service.is_file():
        raise ValueError("Selected repository lacks the locked service release")
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip())
    if dirty:
        raise ValueError("Commit the reviewed candidate before preparing a release bundle")

    state = output.expanduser().absolute()
    state.mkdir(parents=True, exist_ok=False, mode=0o700)

    def write(name: str, content: bytes) -> None:
        with (state / name).open("xb") as stream:
            os.chmod(state / name, 0o600)
            stream.write(content)

    runtime_bytes = json.dumps({"schema_version": 1, "options": options}, default=str, indent=2).encode() + b"\n"
    write("runtime.json", runtime_bytes)
    env = {"PYTHONUNBUFFERED": "1", "REACHY_REPO": str(repo), "ENV_FILE": str(env_file.absolute()),
           "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    python_paths = [str(repo / "src")]
    # Match OPS bootstrap: selected venv's GI path, no inherited GST wheel paths.
    gi_paths = sorted(python.parent.parent.glob("lib/python*/site-packages/gstreamer_python/lib/python*/site-packages"))
    if gi_paths:
        python_paths.append(str(gi_paths[0]))
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    receipt_dir = options["artifact_root"] / "service-receipts"
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "reachy_mini_brain.reception_service",
                             "--mode", "reception", "--confirm-physical", "--host", host,
                             "--port", str(port), "--robot-id", robot_id, "--config-id", config_id,
                             "--runtime-config", str(state / "runtime.json"),
                             "--env-file", str(env_file.absolute()), "--receipt-dir", str(receipt_dir),
                             "--token-file", str(token_file.absolute()), "--tls-cert", str(cert.absolute()),
                             "--tls-key", str(key.absolute())],
        "WorkingDirectory": str(repo), "EnvironmentVariables": env,
        "ProcessType": "Interactive", "RunAtLoad": False, "KeepAlive": False,
        "ExitTimeOut": 15, "Umask": 0o077,
        "StandardOutPath": str(state / "service.stdout.log"),
        "StandardErrorPath": str(state / "service.stderr.log"),
    }
    write(f"{LABEL}.plist", plistlib.dumps(plist))
    robot_config = {"service_url": f"wss://{'[' + host + ']' if ':' in host else host}:{port}/reception/control",
                    "config_id": config_id, "robot_id": robot_id,
                    "token_file": "control.token", "tls_ca_file": "ca.pem"}
    write("robot-config.json", json.dumps(robot_config, indent=2).encode() + b"\n")
    manifest = {
        "schema_version": 1, "label": LABEL, "source_revision": revision,
        "source_repo": str(repo), "runtime_python": str(python),
        "lock_sha256": sha256(lock), "runtime_config_sha256": sha256(state / "runtime.json"),
        "native_wheel": str(wheel.absolute()), "native_wheel_sha256": sha256(wheel),
        "tls_certificate_sha256": sha256(cert), "service_url": robot_config["service_url"],
        "receipt_dir": str(receipt_dir), "started": False, "installed": False,
    }
    write("bundle.json", json.dumps(manifest, indent=2).encode() + b"\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "repo", "python", "runtime-config", "env-file", "token-file", "cert", "key", "ca", "wheel"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in ("host", "robot-id", "config-id"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    print(json.dumps(prepare_bundle(**vars(args)), indent=2))


if __name__ == "__main__":
    main()
