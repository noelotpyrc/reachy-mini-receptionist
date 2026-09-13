# Native Reception Prototype

This is a separate candidate, not the production launcher. The robot package
contains a control client and official `ReachyMiniApp` entry point. It does not
carry speech, run inference, or invoke OPS. The service retains the working
SDK WebRTC input/output path.

Build the native wheel from the repository root:

```sh
uv build --wheel native_app --out-dir /tmp/reception-native-wheel
```

Only `src/reachy_mini_reception_app` is included. Do not install the main project's
macOS inference environment on the robot. This source-relative wheel build is
for staging; source-distribution/Hugging Face publication is not finalized.

## Offline Control Test

The service's explicit `mock` mode creates no robot connection and loads no models.
Set a private `RECEPTION_CONTROL_TOKEN` with at least 32 printable non-space ASCII
characters. Do not put tokens in commands, source, or browser assets.

```sh
.venv/bin/python -m reachy_mini_brain.reception_service --mode mock
.venv/bin/python -m pytest tests/test_native_reception_control.py tests/test_reception_av_probe.py -q
```

The native app loads `$XDG_CONFIG_HOME/reachy-mini-reception/config.json`
(default `~/.config/reachy-mini-reception/config.json`). This is private,
per-app configuration, not daemon environment configuration. Example:

```json
{
  "service_url": "wss://192.168.1.163:8876/reception/control",
  "token_file": "control.token",
  "tls_ca_file": "ca.pem",
  "config_id": "av-probe",
  "robot_id": "38c4b42a3d0111dc"
}
```

Config and token files must be owner-only (0600); relative file paths resolve
beside the config. The token is never served by the status UI. A custom CA is
scoped to this connection; certificate and hostname verification remain enabled.
Configuration is validated before the framework initializes robot IO.
Existing server-side environment variables override corresponding file settings:

| Variable | Meaning |
| --- | --- |
| `RECEPTION_SERVICE_URL` | `wss://<service-host>:8876/reception/control`; plain WS only for loopback tests |
| `RECEPTION_CONTROL_TOKEN` | Shared private service credential |
| `RECEPTION_CONFIG_ID` | Server-allowlisted ID, matching `--config-id` (defaults to the mode name) |
| `RECEPTION_ROBOT_ID` | Identity allowlisted by service `--robot-id` |
| `RECEPTION_TLS_CA_FILE` | Optional trusted CA file for the WSS service |

Remote service listeners require `--tls-cert` and `--tls-key`. Normal certificate
verification remains enabled; there is no insecure verification override.
The new `reception` mode requires explicit runtime configuration and physical
authorization. It has not been deployed or approved to replace production.

`/` shows a read-only status page, backed by `/api/reception/status`, through the
SDK settings server on port 7860. Version 0.1.2 adds configuration, recording flags,
health freshness, run identifiers and stale/disconnected states. Connection-setting
editing and switching configurations during a run are not implemented.
Start/Stop are the
official app lifecycle, not additional browser commands. A fault exits the app;
there is no automatic reconnect or physical restart.

## Controlled AV Probe: Approval Required

Do not run this during a production session. First stop the existing runner and
confirm exclusive robot access. The service's `av-probe` mode additionally requires
`--confirm-physical`, `--robot-host`, and `--probe-wav`. The WAV must be mono PCM16,
16 kHz, no more than 60 seconds. `--probe-duration` is bounded to 1-120 seconds
(default 60). The probe starts only when the native control connection requests it.

It receives camera/microphone through the SDK, reports ready after both produce
samples, plays the provided WAV once through the SAME SDK return stream, logs
input counts/output submission, then flushes/closes media. No S2S, policy or
gesture model runs in this probe. SDK construction has its existing automatic
body-yaw setting side effect; this is a physical test, not passive observation.

The complete prerecorded WAV is enqueued once and SDK/GStreamer paces the
stream. It is not split into capture-paced 20 ms submissions. Stop still clears
queued playback. This tests the AV transport, not live TTS chunk production.

For first-sound diagnosis only, `--probe-lead-in-ms` prepends 0-1000 ms of
silence in memory to that single submission (default 0). It does not change the
source WAV. A 300 ms comparison was authorized after the legacy path removed
the mid-sentence cutoff but the user reported loss of the first sound of
"welcome". This is not an approved default for production speech; it delays
speech onset by the selected duration and requires listening confirmation.

Required review: normal speech, continuous AV, native framework coexistence,
Stop during playback, disconnect termination, teardown, and ordinary controls
afterward. Logs prove submissions, not physical audibility. Neither a passed
mock test nor the probe proves full production supervisor/media-liveness behavior.

Keep the official desktop open during the first probe. Its known Wi-Fi close
behavior can stop the daemon and needs separate acceptance/remediation.

September 11 follow-up: with explicit approval, SDK 1.10.0 and this native
package were installed into `/venvs/apps_venv` (Python 3.12.12). Imports and
official installed-app discovery pass; the daemon environment was unchanged.
Version 0.1.1 adds private configuration loading
and a read-only settings page. The separate m1max probe listener and robot
configuration have been provisioned and TLS/authentication verified without
sending Start. Subsequent UI probes did run and failed audio listening acceptance;
the September 12 compatibility option below awaits a new official UI check.
See `docs/reception-native-app-spec.md` for installation and verification details.

`provision_probe.py` generates test credentials, a 30-day private test CA/server
certificate, robot config, and an Interactive launchd plist on m1max. It refuses
to reuse an existing state directory and starts no service or robot itself.
Copy only the robot config, token and public CA to the robot; never copy CA or
server private keys. The listener must be explicitly registered/started.
This is probe provisioning, not a production service release.

## Service-Side Audio Compatibility

The service accepts `--audio-send-chain stock|legacy` (default `stock`). The
provisioner accepts the same flag and includes it in the generated launchd
arguments. September 12 A/B/A listening reproduced cutoff / clean / cutoff
with stock / legacy / stock under matching test conditions. The operator
approved `legacy` for the m1max candidate service, not an installed-SDK edit.

`legacy` imports the existing `reachy_mini_brain.audio._patch_bin_add_check`
before constructing the network SDK. Despite that old name, it replaces the
complete send-chain builder. It requires SDK 1.10.0 and fails if installation
does not succeed. It changes all SDK clients in that service process, not the
robot app, daemon, or other processes. No second implementation is maintained.
When staging only a subset of this package, include its unchanged `audio.py`,
`audio_pacing.py`, and `robot.py` dependencies from the matching release.

Rollback: stop the probe app, stop/unload the service, change its launch arguments
to `--audio-send-chain stock`, then reload/start the service in a fresh process.
Do not attempt to undo a class override inside a live process. Stock mode refuses
to run if the legacy marker is already present. No live receptionist is selected
by either option. Repeat official UI listening and Stop acceptance before treating
this service-side compatibility change as accepted.

## Offline Verification: September 9

Control/probe plus existing official-runtime tests: 118 passed, 2 deselected.
September 11 configuration update: focused settings/control/probe suite 47 passed;
Ruff passed. Installed robot settings page and status API passed an in-process
HTTP check without calling `wrapped_run` or creating an SDK instance.
After the first physical test, the probe's capture-paced audio loop was removed;
the focused suite now has 50 passing tests, including blocking capture and Stop
after audio submission. That pacing-only fix failed physical acceptance.
September 12 service-side compatibility update: 57 focused tests passed and
Ruff passed; the m1max probe listener selects `legacy` and awaits UI acceptance.
Ruff passed. The native wheel was inspected: it contains no reception service,
vision/model code, private profile, or data artifacts. SDK framework/OS transitive
dependencies still require review against the robot environment before installing.

The operator status UI and inert managed-service packaging are implemented locally;
deployment, live acceptance and source publication remain subsequent work. Mock mode must never be
presented as a functioning receptionist or evidence of speaker playback.

## Step 4: Shared Reception Runtime

The adapter now reuses `official_runtime.live_app.run_live_session` in a worker
thread inside this service. It does not run a CLI child or invoke OPS. The native
wheel remains control-only and needs no model/profile dependencies. The existing
CLI is retained. This implementation is locally tested, not deployed.

`reception-runtime.example.json` is a non-secret configuration template, not a
usable clinic configuration. Select the reviewed profile paths and matching
runtime settings in a private server-owned file. Keep provider/tool keys in the
existing m1max environment, not this file or the native app. `duration: null`
runs until native Stop, control loss, or a runtime fault; video recording is off
by default. Explicit tools are `none` or `time-web`, never the test reference tool.

Future candidate launch, only after configuration/environment review and approval:

```sh
.venv/bin/python -m reachy_mini_brain.reception_service \
  --mode reception --confirm-physical \
  --config-id clinic-candidate --robot-id ROBOT_ID \
  --runtime-config /ABSOLUTE/PATH/TO/private/runtime.json \
  --host SERVICE_LAN_IP --port 8876 \
  --token-file /ABSOLUTE/PATH/TO/control.token \
  --tls-cert /ABSOLUTE/PATH/TO/server.pem \
  --tls-key /ABSOLUTE/PATH/TO/server.key
```

The listener alone does not start a physical session. Select the same config ID
in the native app and use official Start. Do not use the probe provisioner to
deploy reception mode; Step 5 will package the managed runtime and environment
(including GStreamer setup and Interactive scheduling). The reception path uses
the existing runner's legacy audio helper; `--audio-send-chain` and
`--probe-lead-in-ms` apply only to AV-probe mode.

Sessions preserve existing run artifacts with `native-<session_id>` identifiers.
Stop cancels output/tasks, flushes the SDK player and disconnects only its client.
It does not release shared daemon media or stop the S2S backend. Faulted/incomplete
cleanup blocks further Starts until operator review and service restart; do not
start the CLI while the failed worker might still own the robot.

The user accepted September 12 probe speech with the legacy chain and 300 ms
lead-in, then clean Stop/disconnect/restart. That lead-in remains probe-only.
Full receptionist physical acceptance, managed packaging, and UI work remain.
See [the specification](../docs/reception-native-app-spec.md#september-12-step-4-shared-runtime-sessions).

## Step 5: Review And Release Preparation

See the [candidate runbook](../docs/reception-native-service-runbook.md). The new
`python -m reachy_mini_brain.reception_package` generates an inert, private bundle
for one committed service/native-wheel pair. It preserves Interactive scheduling,
uses a separate candidate label/port, and does not install dependencies, register
launchd, start the robot, replace production or create another provider-key `.env`.

The run configuration is reviewed outside the browser and fixed per session.
Official Start/Stop remain unchanged. The lightweight status UI is read-only and
adds no robot streaming. Runtime receipts survive official app exit on m1max;
their failure to write cannot prevent Stop cleanup.

For local visual review only, run `python native_app/preview_status.py`. It binds
loopback, uses fabricated status, and imports no robot/service runtime. Preview
states are selected with `?state=starting`, `stopping`, `stopped`, `faulted`, `stale`
or `disconnected`; default is ready. No preview mode is included in the native
wheel. Browser verification optionally uses `check_status_ui.cjs` with Playwright
and installed Chrome. Keep generated screenshots out of Git.
