"""Generate private probe configuration on m1max; never start a robot run.

Requires the already-installed openssl executable. Refuses to overwrite files.
Copy only robot-config.json, control.token and ca.pem to the robot, renaming
robot-config.json to config.json. Keep the CA/server private keys on m1max.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--host", required=True)
    parser.add_argument("--robot-host", required=True)
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--wav", required=True, type=Path)
    parser.add_argument("--audio-send-chain", choices=["stock", "legacy"], default="stock")
    parser.add_argument("--probe-lead-in-ms", type=float, default=0)
    args = parser.parse_args()
    if not 0 <= args.probe_lead_in_ms <= 1000:
        parser.error("--probe-lead-in-ms must be between 0 and 1000")
    host = str(ipaddress.ip_address(args.host))
    ipaddress.ip_address(args.robot_host)
    source = args.source_dir.resolve(strict=True)
    # Preserve the venv entry point rather than resolving its interpreter symlink.
    python = args.python.expanduser().absolute()
    if not python.is_file():
        parser.error("The selected Python interpreter does not exist")
    wav = args.wav.resolve(strict=True)
    openssl = shutil.which("openssl")
    if openssl is None:
        parser.error("openssl is required")
    state = args.state_dir.expanduser().absolute()
    # An exclusive new directory prevents accidental credential rotation.
    os.umask(0o077)
    state.mkdir(mode=0o700, parents=True, exist_ok=False)

    def command(*parts: str) -> None:
        subprocess.run([openssl, *parts], cwd=state, check=True, capture_output=True)

    command("req", "-x509", "-newkey", "rsa:2048", "-noenc", "-days", "30",
            "-keyout", "ca.key", "-out", "ca.pem", "-subj", "/CN=Reception Probe Test CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    command("req", "-new", "-newkey", "rsa:2048", "-noenc",
            "-keyout", "service.key", "-out", "service.csr", "-subj", "/CN=Reception Probe",
            "-addext", f"subjectAltName=IP:{host}",
            "-addext", "basicConstraints=critical,CA:FALSE",
            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
            "-addext", "extendedKeyUsage=serverAuth")
    command("x509", "-req", "-in", "service.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-set_serial", "0x" + secrets.token_hex(16), "-days", "30",
            "-copy_extensions", "copy", "-out", "service.pem")
    command("verify", "-CAfile", "ca.pem", "-verify_ip", host, "service.pem")
    (state / "control.token").write_text(secrets.token_urlsafe(48) + "\n")
    robot_config = {
        "service_url": f"wss://{host}:8876/reception/control",
        "token_file": "control.token", "tls_ca_file": "ca.pem",
        "config_id": "av-probe", "robot_id": args.robot_id,
    }
    (state / "robot-config.json").write_text(json.dumps(robot_config, indent=2) + "\n")
    # Keep the source WAV unchanged, including its original provenance.
    shutil.copyfile(wav, state / "greet.wav")
    label = "com.reachy.reception.native-probe"
    plist = {
        "Label": label,
        "ProgramArguments": [str(python), "-m", "reachy_mini_brain.reception_service",
                             "--mode", "av-probe", "--host", host, "--port", "8876",
                             "--robot-id", args.robot_id, "--robot-host", args.robot_host,
                             "--confirm-physical", "--probe-duration", "60",
                             "--probe-wav", str(state / "greet.wav"),
                             "--audio-send-chain", args.audio_send_chain,
                             "--probe-lead-in-ms", str(args.probe_lead_in_ms),
                             "--tls-cert", str(state / "service.pem"),
                             "--tls-key", str(state / "service.key"),
                             "--token-file", str(state / "control.token")],
        "WorkingDirectory": str(source),
        "EnvironmentVariables": {"PYTHONPATH": str(source), "PYTHONUNBUFFERED": "1"},
        "ProcessType": "Interactive", "RunAtLoad": False, "KeepAlive": False,
        "StandardOutPath": str(state / "service.stdout.log"),
        "StandardErrorPath": str(state / "service.stderr.log"),
    }
    with (state / f"{label}.plist").open("xb") as stream:
        plistlib.dump(plist, stream)
    print(json.dumps({"state_dir": str(state), "service_url": robot_config["service_url"],
                      "label": label, "certificate_days": 30,
                      "wav_source": str(wav), "started": False}))


if __name__ == "__main__":
    main()
