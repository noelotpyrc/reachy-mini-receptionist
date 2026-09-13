# Native Reception App Specification

Date: September 9, 2026.
Status: September 12 probe speech and Stop/disconnect/restart accepted by the
user. Steps 4-5 shared-runtime integration, status UI and inert service packaging
are implemented locally and offline-tested, not deployed. Native 0.1.2 is built;
the robot still has 0.1.1. The candidate listener remains in AV-probe mode.
No production replacement is authorized.
The earlier first-start network failure has no confirmed root cause; repeat
cold-start acceptance remains part of the later end-to-end gate.
The official app guide and Conversation playback flow were reviewed with the
user; local native playback and m1max WebRTC playback are different architectures.

The architecture below is approved for implementation. It supersedes the split native-playback
proposal in the [source investigation](reception-native-app-feasibility.md) and
the desktop-fork/OPS-wrapper proposals in [control-app history](reception-control-app.md).

## Architecture Decision

Preserve the working bidirectional SDK WebRTC path. The reception service
replaces the current runner's deployment role, with the same processing modules
and hardware I/O. The native app provides official lifecycle, configuration,
and status; it does not implement a second speaker pipeline.

```text
ROBOT                                  M1MAX

Daemon camera + microphone
    ---- existing SDK WebRTC AV ------> Reception service
Daemon speaker                          |-- Vision + reception policies
    <--- same WebRTC return audio ----- |-- Chat + profiles/tools
Daemon movement                         |-- Existing S2S backend
    <--- existing SDK/control calls ----|

Native Reception app
    <--- authenticated control-only ---> Reception service
         start / stop / status /
         application heartbeat
```

Generated speech uses one SDK return stream, exactly as in the existing runtime.
Correction to the original draft: the existing runtime's legacy send-chain
override does not use the stock SDK silence mixer. Step 4 reuses that existing
helper, not a new implementation or an installed-SDK edit. There is no second
speaker transport, receive-only SDK replacement, or native microphone forwarding.

The control connection owns a reception run, not a new LLM/chat session. Existing
S2S conversation-session management remains unchanged.

## Responsibilities

| Component | Responsibilities |
| --- | --- |
| Robot daemon | Hardware access, AV streaming, returned audio playback, motor control |
| Native Reception app | Official app lifecycle, start/stop authorization, named configuration, run status, control liveness |
| Reception service on m1max | SDK AV input/output and movement, vision, reception policies, chat lifecycle, profiles/tools, S2S coordination, recordings and diagnostics |
| Existing S2S backend on m1max | Current VAD, STT, LLM, TTS; unchanged |

Use the official `ReachyMiniApp` lifecycle and framework-supplied SDK instance.
The native app must not start local microphone capture or playback, release
daemon media with `no_media`, or launch the old runner via OPS/subprocesses.
The framework still constructs a default LOCAL media manager and sets automatic
body yaw; its incidental IPC camera reader and setup/cleanup effects must be
checked on the robot. Do not claim zero SDK initialization overhead.

Reception service sessions are in-process runtimes controlled by the native app.
No separate vision microservice, new agent framework, or public shell endpoint.

## Runtime Reuse And Isolation

- Preserve the existing SDK AV consumer, `ReachyAudioSource`, `ReachyAudioSink`,
  and movement/cue paths. No second physical-output transport.
- Extract CLI runtime initialization into a callable service session, retaining
  the CLI and its default behavior for rollback.
- Reuse policies, runtime ticks, chat latch, frame broker, detectors, S2S client,
  production profile composition, approved tools, and artifact formats.
- Preserve fixed-text `tts.create`, model/voice settings, validated MLX pins,
  and backend `ProcessType=Interactive`.
- Retain daemon video encoding and the current 15 FPS broker configuration.
  Native camera IPC does not define our processing input cadence.
- Keep models, clinic context, provider keys, and archives on m1max. Package the
  native app separately from those dependencies.
- No default production launcher/config change. Never run the candidate and
  current production runner against the same robot simultaneously.

## Lifecycle And Failure Contract

1. Official Start launches the native app. It opens an authenticated control
   connection and requests a named configuration for the configured robot.
2. The already-running service reserves a single run and initializes its SDK,
   processing, and S2S session. It reports `starting`, not ready just because a
   subprocess or WebSocket exists.
3. Report `ready` only when the selected runtime's input/output initialization
   and health criteria are satisfied.
4. Official Stop signals the control client, which requests session stop.
   Service stop first prevents new policy/chat output, cancels generation/cues,
   flushes the SDK player, then closes this client's media and finalizes artifacts.
   This does not prove that audio already buffered on the daemon has stopped.
5. Report `stopped` only after the runtime's cleanup finishes. A cleanup timeout
   reports `faulted` and blocks Start while cleanup is pending or unverified.
   A later explicit Start may recover only after the actual reception worker
   returns cleanly and the previous controller task ends. Failed cleanup still
   requires operator review. There is no automatic restart.

User requirement clarified September 11: ordinary Reception Stop must not put
the robot to sleep. Stop ends reception processing, audio/cues and ownership;
the official control app's separate Sleep action owns intentional sleep. An
already sleeping robot must not be woken by Reception cleanup. This behavior
is NOT currently satisfied by the installed official daemon. The user subsequently
deferred it and accepted the official sleep behavior for current testing; it is
not a current acceptance blocker. See the stop/sleep note below.
Crash/disconnection safety behavior is a separate policy question,
not authorization to disable all daemon safety cleanup.

Repeated Start/Stop must not create parallel runs. Bind commands to their
connection and run ID. An old connection cannot adopt or stop a replacement run.
Stop during startup must not be undone by late readiness or pending startup work.

Application heartbeats prove that the native control loop is advancing; transport
ping/pong alone is insufficient. Missed heartbeats or disconnect ends the owned
run. Do not reconnect automatically or silently resume physical behavior.

The prototype uses a 1-second client heartbeat and 6-second service lease, with a
3-second ordinary request timeout and a 12-second Stop response budget. Runtime
cooperative-stop timeout is 5 seconds, followed by at most 5 seconds observing
cancellation. These are candidate values to validate, not measured physical-stop
latencies. Local stop checks happen at most every 50 ms between requests; an
in-flight ordinary request may take up to its timeout before disconnect.

App sessions support explicit run-until-stopped semantics. Preserve timed CLI
runs and bounded test runs. A run-until-stopped control session does not disable
media liveness, fault termination, or processing watchdogs.

The native app no longer owns a local speaker queue to flush. A service hang,
network partition, or abrupt process death may leave daemon-buffered audio or
motion in progress. Therefore instant local physical stop is NOT established by
this prototype. Verify the existing supervisor protections and whether a
supported robot-local fallback is needed before production acceptance. Do not
claim a clean stop solely from control-message delivery.

Fit native exit within the daemon app manager's 20-second stop budget. Separate
output shutdown from slower archive finalization where needed. Normal Stop and
robot sleep remain separate actions.

## Control Protocol And Security

Use a persistent WebSocket with versioned JSON messages; it carries no audio,
video, profile text, provider key, or arbitrary command invocation.

- Client: `start` (run ID, configuration ID, robot ID), `heartbeat`, `stop`.
- Server: bounded run status with phase, elapsed time, identifiers, terminal reason.
- The service allowlists configurations and binds them to robot identity.
- Authenticate during the upgrade with a private bearer token. Refuse browser
  Origin requests; the native backend, not browser JavaScript, holds the token.
- Use WSS off loopback with normal certificate verification. Plain WS is allowed
  only on loopback for offline development.
- Maximum control message: 4096 bytes; bounded receive queues. No per-audio-chunk
  acknowledgements and no detection-result round trips through the robot.
- Terminal/runtime errors are redacted in client-visible status; detailed runtime
  diagnostics remain on m1max under the existing retention policy.

The embedded UI will show configuration, recording flags, run ID, elapsed time,
startup/stop progress, health and terminal reason. Prototype exposes read-only
JSON status; full UI and config selection follow transport acceptance.

## Logging, Deployment, And Rollback

Retain run/generation/frame IDs and capture timestamps in the existing artifacts.
Add control ownership, heartbeat loss, stop request and cleanup outcome events.
No new logging path may imply physical audibility from an SDK write or a WebSocket
acknowledgement. Keep the 30-day report-only retention workflow and recording defaults.

Build a separate lean native wheel from `native_app/pyproject.toml`. Prototype
source is shared with the main repo under `src/reachy_mini_reception_app`; model
modules are excluded from the native wheel. Source-distribution/app-store
publication packaging is a later release task, not accepted yet.

Inspect robot Python/SDK/shared apps dependencies before installation. Do not
change that environment, production's frozen release, or its launcher without
approval. Managed S2S remains independent; stopping reception must not routinely
stop/restart the model backend.

Rollback: stop the candidate, verify processing/media/movement ownership has
ended, then start the existing production runtime. No simultaneous owners, file
deletion, automated dependency upgrade, or silent production switch.

## Implementation And Acceptance Sequence

September 9 implementation evidence:

- Steps 1-2 completed: revised contract, official native entry, authenticated
  control client/service, explicit single-owner sessions, heartbeat loss handling,
  bounded stop with fault latch, and a mock-only default test configuration.
- Step 3 prepared: opt-in AV probe uses the existing SDK return path and provided
  speech WAV. It is tested against a fake SDK, not a physical robot.
- Focused suite: 118 passed, 2 existing tests deselected. This covers 36 new
  control/probe cases and 82 existing official-runtime cases. Ruff and whitespace
  checks passed for changed code. This is not a full repository suite.
- Native wheel builds independently; inspected contents include only the native
  package and metadata, with direct dependencies on SDK 1.10.0 and websockets.
- No production runtime extraction, managed deployment, robot installation,
  live probe, SDK patch, profile change, commit or push has been performed.
- Next checkpoint is an agreed robot test window and review of installation,
  TLS/service credentials, and exclusive ownership before running the probe.

| Step | Work | Acceptance boundary |
| --- | --- | --- |
| 1 | Correct spec; define the control contract and SDK setup/cleanup boundaries | No split playback, custom receiver, or dependency patch |
| 2 | Native framework entry, authenticated service/client, single-run ownership, heartbeat and stop; explicit mock runtime first | Offline transport/lifecycle tests and lean wheel inspection |
| 3 | Opt-in bounded AV probe using existing SDK capture/playback alongside native control | Agreed robot window; listen for speech/echo, verify advancing AV, app Stop/disconnect and normal controls afterward |
| 4 | Extract existing runtime into service sessions, preserve CLI, wire output cancellation/flush and liveness | Focused regression tests; no policy/backend tuning |
| 5 | Integrate embedded UI/config/status, artifacts, managed service packaging and rollback | Offline tests/builds and review before deployment |
| 6 | Controlled end-to-end acceptance and release freeze | Attended conversation/policy checks; preview/close, startup Stop, disconnect and restart behavior |
| 7 | Promote | Explicit approval; existing production retained for rollback |

### September 11 Attended Probe Preparation

The user was at the clinic and authorized the proposed attended tests. The
production run `official-live-20260911-093117` had completed normally at 13:31:56
EDT with closed artifacts and successful cleanup. The current native-app status
was null. No run needed to be interrupted.

Read-only SSH inspection established:

- Robot daemon: `/venvs/mini_daemon/bin/python`, Python 3.12.12, SDK 1.10.0.
- Official app manager resolves the native app interpreter to
  `/venvs/apps_venv/bin/python`, also Python 3.12.12.
- Shared apps environment: websockets 15.0.1 and FastAPI 0.141.1 are installed;
  `importlib.util.find_spec` cannot find `reachy_mini`, `numpy`, or `uvicorn`.
- An older Conversation app distribution 0.7.0 is registered in that environment.
  Registration alone does not establish that it can run.
- The running daemon has no `PYTHONPATH` or `PYTHONHOME` environment override
  that would make its own packages available to the app subprocess.

Stopped before package installation, native startup, service deployment, or
speech playback. This is a missing app-environment prerequisite, not an AV
coexistence test failure. No dependencies were changed and no physical test was
performed. Review a dependency-resolution/install plan for the shared apps
environment before proceeding; preserve the daemon and m1max production envs.
This preparation should have been completed before scheduling attended listening.

### September 11 Installation Review

Review only, no installation. Candidate wheel SHA-256:
`3c249960895b2e65818dc76096d74b338868e73e76ce6d84ab9f0f96afb7a356`.
Copied the wheel into `/tmp` on m1max and the robot for resolution; no app source
or existing package was replaced. Metadata resolution may populate uv caches.

The official installer creates/reuses the shared apps environment and resolves
the selected app's declared dependencies. Missing SDK dependencies before first
installation do not alone establish a damaged environment or require manual
installation of each library. Our wheel declares SDK 1.10.0 and websockets
>=15.0.1,<16, consistent with that packaging mechanism.

However, an actual baseline `uv pip check --python /venvs/apps_venv/bin/python`
reported 59 dependency inconsistencies across 109 installed distributions.
Examples: missing NumPy/Pydantic/Uvicorn, and Conversation app 0.7.0 requiring
huggingface-hub==1.3.0 while 1.29.0 is installed. These predate our installation.

Unconstrained review command:

```sh
uv pip install --dry-run --python /venvs/apps_venv/bin/python \
  /tmp/reachy_mini_reception_app-0.1.0-py3-none-any.whl
```

Resolved 74 packages; would download 7 and add 30, with no existing package
replacement/removal listed. The proposed additions were:

```text
aiohappyeyeballs==2.7.1
annotated-doc==0.0.5
anyio==4.15.1
asgiref==3.12.1
charset-normalizer==3.5.1
click==8.5.0
filelock==3.32.6
hf-xet==1.6.0
libusb-package==1.0.30.0
numpy==2.5.3
onnxruntime==1.27.0
packaging==26.3
pip==26.2.1
platformdirs==4.11.8
prompt-toolkit==3.0.53
protobuf==7.36.1
pulsectl==24.12.0
pycairo==1.29.1
pydantic==2.13.5
pydantic-core==2.46.5
python-dotenv==1.2.3
reachy-mini==1.10.0
reachy-mini-reception-app==0.1.0 (local wheel)
rich==15.0.0
tqdm==4.70.1
typing-inspection==0.4.4
uvicorn==0.52.4
watchfiles==1.2.0
wcwidth==0.8.3
yarl==1.24.5
```

These are resolution results, not approved pins or successful build/import
results. The SDK brings ONNX Runtime as a dependency even though our native
package contains no reception inference models.

**Confirmed incompatible requirements:** installed Conversation app 0.7.0 pins
Gradio 5.50.1.dev1, which requires Pydantic <=2.12.3. SDK 1.10.0 requires
Pydantic >=2.12.5,<3. Adding `pydantic<=2.12.3` to the dry run returned
`No solution found`. It is impossible to satisfy both versions in this shared
environment as-is. The unconstrained resolver's successful plan is not proof
that every already-installed app remains compatible.

Recommendation: do not install this plan as-is. First decide whether to retain
and update the old Conversation app/dependency set, or retire it with explicit
approval. Preserve an environment inventory/recovery copy before any shared
environment change; do not downgrade the tested SDK or patch dependencies to
force this conflict. Any removal or environment rebuild requires approval.
Re-resolve and inspect the full retained app set before installation.

Post-review check still reported 109 installed distributions and no Reception
app installation. Daemon and m1max production environments were untouched.
Native installation/AV acceptance remains pending.

The desktop 0.9.34 Wi-Fi window-close handler requests daemon stop. Native
packaging alone does not satisfy close-and-continue operation. This needs an
upstream-supported solution or separately approved narrow desktop change before
production, not an unrelated rewrite of media transport.

No robot installation, physical probe or production run is authorized by the
offline implementation work. Stop when a dependency/design decision or attended
acceptance is needed. Mock readiness is not evidence of physical media readiness.

### September 11 Follow-up: Robot App Dependencies Installed

Subsequent explicit user approval authorized clearing the failed Conversation
status and installing the native app's dependencies, not starting Reception.
The user had uninstalled Conversation through the official control UI. Inspection
found no app entry points or running app processes, but 108 leftover packages.
`current-app-status` retained Conversation's startup error caused by missing
`tqdm`. The official `POST /api/apps/stop-current-app` cleared it to `null`.

The native package's direct requirements remain `reachy-mini==1.10.0` and
`websockets>=15.0.1,<16`. WebSockets 15.0.1 was already installed. The initial
SDK install added 16 packages before rejecting the cached pulsectl wheel:
its `METADATA` file was zero bytes, dated March 6. A fresh in-memory download
of pulsectl 24.12.0 from PyPI contained valid Name/Version metadata, passed ZIP
integrity validation, and matched the published SHA-256:
`13a60be940594f03ead3245b3dfe3aff4a3f9a792af347674bde5e716d4f76d2`.
The original cache was not deleted or repaired. Retrying with a separate cache
completed the remaining 13 installations:

```sh
/opt/uv/uv --no-progress --cache-dir /tmp/reception-sdk-cache-20260911-retry1 \
  pip install --python /venvs/apps_venv/bin/python \
  'reachy-mini==1.10.0' 'websockets>=15.0.1,<16'
```

Verification:

- SDK 1.10.0, `ReachyMiniApp`, WebSockets 15.0.1, tqdm 4.70.1 and pulsectl
  imported successfully without creating an SDK connection.
- Recursive requirement checks, including active markers/extras, found all 73
  required packages satisfied, with no missing or incompatible requirements.
- The environment now contains 137 packages. Global `uv pip check` still
  reports nine warnings from leftover packages outside that dependency set
  (Gradio, aiortc, PyOpenSSL, Google Auth, librosa, fastrtc and pandas).
  These packages were not removed or upgraded to satisfy unused requirements.
- No native Reception app was installed; app entry points remain empty.
- Daemon environment was unchanged at SDK 1.10.0; daemon status remained
  running/ready, media enabled, without a reported backend error.
- No physical AV acceptance or control-app streaming retest was performed.
  Missing tqdm was confirmed as the Conversation startup failure, not as the
  cause of the desktop preview failure; daemon-environment tqdm already worked.

### September 11 Follow-up: Native Package Installed, Probe Not Started

With explicit user approval, installed the previously inspected native wheel
(SHA-256 `3c249960895b2e65818dc76096d74b338868e73e76ce6d84ab9f0f96afb7a356`)
into `/venvs/apps_venv` using the separate fresh cache. Only the native package
was added; no dependency replacement was needed. The production runner was
confirmed stopped before preparation; its backend and release were not changed.

Verification:

- `ReceptionApp` imports and subclasses the official `ReachyMiniApp`.
- Official `GET /api/apps/list-available/installed` lists
  `reachy_mini_reception_app`, its shared venv and status-server URL.
- Focused local control/probe suite: 36 passed in 9 seconds.
- `current-app-status` remains `null`; no app startup or physical probe occurred.

Pre-start blocker: the current prototype reads four `RECEPTION_*` connection
settings only from environment variables. None is present in the running
daemon's inherited environment. The framework does not provision them for our
app, and no prototype listener was reported on m1max port 8876. Starting the
installed app now would fail configuration validation after SDK initialization.
This is our remaining provisioning gap, not an SDK dependency or media failure.

Before the physical probe, add app-specific private configuration loading and
provision the allowlisted m1max probe service, authentication credential and
verified TLS trust. Avoid putting our settings into the daemon environment or
substituting a manually launched process for official Start/Stop acceptance.
Then test through official Start/Stop with the agreed bounded WAV/AV probe.

### September 11 Follow-up: Configured for Official UI Acceptance

The user approved one-time connection provisioning, then personally clicking
Start/Stop in the official UI. No manual native process launch substitutes for
that acceptance. The configuration gap above is now resolved:

- Native 0.1.1 loads per-app private JSON/token files and optional CA trust,
  validates before SDK initialization, and serves a read-only status page.
  Environment overrides remain available for development; daemon environment
  was not changed. No insecure TLS option was added.
- Installed wheel SHA-256:
  `2a143398c72aeb70e4ad406d29818ff442d0b8e78bb11d3652d5f39a3a9be04d`.
- Robot config: `/home/pollen/.config/reachy-mini-reception/config.json`, token
  and `ca.pem` alongside it. Credentials are outside Git and owner-readable only.
- Isolated m1max source: `/Users/leon/projects/reception_native_probe_20260911/src`.
- Private m1max state: `/Users/leon/.config/reachy-reception/native-probe-20260911`.
  The generated launchd plist uses label `com.reachy.reception.native-probe`,
  Interactive process type, `RunAtLoad=false`, and `KeepAlive=false`. It was
  explicitly bootstrapped and started, not added to production's launcher.
- Endpoint: `wss://192.168.1.163:8876/reception/control`. Test certificates expire
  after 30 days and bind that LAN IP. Changes to IP/expiry need reprovisioning.
- The service reuses the existing ce95a49 release interpreter read-only, with
  candidate source on PYTHONPATH; no packages or code in that release changed.
- Selected configuration: `av-probe`, hardware ID `38c4b42a3d0111dc`, robot
  `192.168.1.165`, duration 60 seconds. Start waits for microphone and camera
  samples, plays the existing mono PCM16/16-kHz greeting WAV once (4.224 seconds),
  then continues the bounded AV probe or responds to native Stop. It runs no
  LLM/TTS/vision inference. WAV source:
  `reachy_mini_receptionist_deploy/artifacts/benchmarks/policy-tts-smoke-20260805-wav/greet.wav`.

Verification before user Start:

- Focused local settings/control/probe suite: 47 passed; Ruff clean.
- Robot loaded config and completed trusted TLS/authentication against m1max.
  A heartbeat without Start correctly returned `Start required`, so no runtime
  was created. Missing authentication returned 401; untrusted CA was rejected.
- Installed app settings HTML and status JSON returned HTTP 200 in TestClient,
  without starting the framework or SDK IO and without exposing the token.
- Daemon HOME/config location matches the provisioned app configuration.
- `current-app-status` remained `null`; production runner remains stopped.

Next: the user clicks Start for `reachy_mini_reception_app` in the official UI.
Inspect native/daemon logs and m1max `service.stdout.log`/`service.stderr.log`;
confirm physical speech and AV progress, then official UI Stop and cleanup.
This installation does not enable full receptionist behavior or prove physical
playback, and connection editing/full operator UI remain later work.

### September 11 Official UI Probe Findings

User reported a first-launch error followed by the robot sleeping, then speech
on subsequent attempts with a cut/abrupt join between "clinic" and "how".
Inspection found one failed service session, followed by three AV probe runs
(two stopped early, one reached the 60-second bound). No further runs or code
changes were made during diagnosis.

First launch:

- The robot connected to the authenticated m1max service. In session
  `80e4721844de49188ca92919fd9f3a78`, m1max's SDK connection back to
  `ws://192.168.1.165:8000/ws/sdk` failed with OS error 65, `No route to host`.
- The service reported a runtime fault; the native app raised its explicit
  fault exception and exited with code 1. Robot journal timestamps are
  September 12 00:34:41-00:34:49 +01:00 (September 11 19:34:41-19:34:49 EDT).
- This identifies the failed connection, not why the OS rejected it. Transient
  routing or process-specific network permission remains unconfirmed; TLS/auth
  on the separate control connection was not the failing operation.
- The daemon releases the app slot on exit and schedules idle/sleep cleanup.
  The next Stop logged that the robot was already asleep. The daemon process
  did not itself exit; later status was running/ready with motors disabled.

Speech:

- This probe plays a fixed 4.224-second PCM WAV, with no live LLM or TTS.
- Our probe sequentially reads microphone and video before each 20 ms output
  chunk, then waits 5 ms. SDK reads can block for 20 ms each. Consequently the
  output cadence depends on input availability and can fall below real time.
- Logs from the first successful attempt show 0.84-0.90 seconds submitted per
  approximately one wall-clock second during several playback intervals.
- An offline fake-SDK experiment using the unchanged probe over two seconds
  submitted 2.00 seconds of audio with immediate video reads, versus 1.38
  seconds with a 20 ms video-read wait (median push gap 29.49 ms). This proves
  the scheduling flaw, not the physical cause of the exact word-boundary cut.
- Original greeting and copied service logs are under ignored local
  `artifacts/diagnosis/native-probe-20260911/`. Source-WAV listening comparison
  remains necessary before attributing that specific join to streaming.

Next proposed work: prevent blocking input reads from controlling output pacing,
verify cadence with realistic blocking-read tests, and improve bounded startup
connection handling/diagnostics. Keep the SDK transport and S2S unchanged.
No successful full reception test or clean physical audio acceptance is claimed.

### September 11 Probe Pacing Correction

User listened to the exact source greeting locally and reported no problem.
This rules out the source-WAV join as the explanation for the reported cut,
but does not by itself identify which playback stage caused it.

Changed only `reception_av_probe.py` in the isolated m1max candidate. Once both
inputs arrive, it submits the complete prerecorded WAV once, mirroring the
production `ReachyAudioSink` ownership of timing: SDK/GStreamer handles playback.
Subsequent blocking capture reads can no longer delay additional audio chunks.
No new output thread, SDK patch, S2S change, or native-wheel update was needed.
Stop still flushes queued audio and closes media. A stop/deadline reached during
a blocking capture read is now checked before publishing readiness/submitting.

Added focused tests for complete submission with a 20 ms capture delay, Stop
after submission, and Stop during a blocked read. Total focused suite: 50 passed;
Ruff and whitespace checks passed. Deployment SHA-256 matched local source:
`f8e28217506576ce8fd3882f8733df7422f987f33626dad8e23acb0a8a7bcd20`.

The native app was confirmed stopped before deployment. The candidate listener
was stopped with SIGINT (exit 0) and restarted in Interactive mode, waiting for
the user's official UI Start. No physical retry was initiated by the agent.
The original WAV and 60-second bound are unchanged. Listening acceptance and
the separate first-launch network error remain open. This prerecorded probe
does not validate live TTS production pacing.

### September 11 Pacing Retry: Acceptance Failed

User reported a delayed start (approximately 3-4 seconds) with only the first
sentence audible, then a faster start with the same cut between "clinic" and
"how". Do not treat the pacing change as an accepted audio fix.

The post-restart service log contains five SDK connections, not just two. It
lacks wall-clock/session correlation on probe messages, so the two listening
reports cannot be mapped definitively to particular connections. Evidence:

- `webrtcbin2` received no video through elapsed 3.076 seconds; the complete
  4.224-second WAV was submitted before the 4.100-second report. The probe gates
  speech on both microphone and camera input, explaining a variable startup
  delay of this kind independently of TTS.
- All five attempts logged one submission of 67,584 samples after the SDK's
  audio-send-chain-ready message. Submission is not confirmation of playback.
- `webrtcbin0` logged `streaming stopped, reason not-linked (-1)` in its
  GStreamer receive pipeline. Input counters then froze at 22 audio samples and
  three video frames, while the probe kept running until cleanup. This is a
  confirmed media failure, not proof of the cause of either reported cutoff.
- Logged player flushes occurred after the expected end of the submitted WAV;
  no mid-speech application flush is evident in these logs. Some signaling
  errors appeared after cleanup, which must not be confused with the earlier
  in-run `not-linked` error.

Snapshot: `artifacts/diagnosis/native-probe-20260911/service-post-pacing-retries.stderr.log`.
The native app was stopped (`current-app-status: null`) at inspection. No new
physical run, runtime change, SDK patch, or production change was made. Next
diagnosis needs timestamp/session-correlated transport evidence downstream of
submission; the existing log cannot localize where the audible samples were
lost. The known-good source WAV and lack of LLM/TTS in this probe narrow the
investigation to the playback/transport path, not speech generation.

### September 11 Final Comparisons and Pause

The user confirmed the stock-SDK isolated WAV playback was choppy, direct
daemon file playback was clean, and the existing frozen runner's scripted WAV
playback had no cutoff. The last comparison used
`official-wav-comparison-20260911-202032` and the same 4.224-second greeting.
The runner's supported post-submission wait was set to 7.3 seconds rather than
the preflight command's 3-second default; no production source/config was edited.
Its log is preserved locally under `artifacts/diagnosis/native-probe-20260911/`.

Important correction to earlier comparisons: production startup calls the
existing `audio._patch_bin_add_check()`. Despite its narrow name, that legacy
override replaces the complete SDK send-chain setup. It has no silence mixer
and places elements in a different containing pipeline than stock SDK 1.10.0.
The standalone probes and native candidate did not load this override. Thus
the earlier assertion that their downstream paths were identical was wrong.
The clean runner comparison identifies a working path, but does not isolate
which difference (send chain, warm-up, or environment) causes the cutoff.
Do not add/copy a new dependency patch based on this finding.

Sender captures preserved the full speech before encoding and after offline
Opus decode. Their bursty buffer timings are instrumented sender observations,
not receiver evidence or proof of loss. No robot decoded-audio capture was made.

The user stopped further implementation/testing and requested housekeeping
before reconsidering the design using official guidance and the Conversation
app. The temporary `com.reachy.reception.native-probe` service was stopped and
unloaded; port 8876 no longer had a listener. Native app status was null.
Production backend, robot daemon, installed app/dependencies, private config,
prototype source/tests, and diagnostic evidence were retained. Explicit approval
is still required before deleting the listed one-off test scripts/uploaded WAV.

### September 12 Controlled Audio A/B/A

User authorized diagnostic testing, not resuming native implementation or
deploying an SDK change. First stopped production run
`official-live-20260912-123927`; supervisor cleanup and artifact closure succeeded
without forced kill. Restored robot readiness for playback, leaving reception
policies/chat stopped.

One shared diagnostic harness used the frozen release interpreter, production
environment loading and `_base_env`, existing `wait_for_audio`, and
`ReachyAudioSink`. It submitted the same complete 4.224-second WAV once, then
waited 7.3 seconds before flushing/disconnecting. No TTS, perception, native
launcher, or capture callbacks ran. Each trial used a fresh SDK connection.
Only B imported the existing `audio._patch_bin_add_check()` override into its
test process; installed SDK files and production configuration were unchanged.

| Trial | Submission Time (EDT) | Send Chain | User Listening Result |
| --- | --- | --- | --- |
| A: `audio-ab-stock-20260912T192121647306Z` | 15:21:23 | Stock SDK | Same cutoff |
| B: `audio-ab-legacy-20260912T192201263477Z` | 15:22:02 | Existing legacy override | No issue |
| A repeat: `audio-ab-stock-20260912T192251270829Z` | 15:22:52 | Stock SDK | Cutoff returned |

All three exited 0 and disconnected. Verified identical WAV hash, interpreter,
SDK/GStreamer versions, captured GStreamer environment, and post-submission wait.
Audio warm-up lasted 0.530, 0.527, and 0.538 seconds respectively. A and its repeat
used the same send-method hash; B's method came from `reachy_mini_brain.audio`.
Each trial's setup/result/log is preserved locally under
`artifacts/diagnosis/native-probe-20260911/<trial-id>/`.
Machine result JSON retains `listening_result: pending`; this table records the
subsequent human reports without rewriting the original machine evidence.

An earlier harness invocation (`audio-ab-stock-20260912T192040177960Z`) failed
before SDK connection or playback because it queried the GStreamer version
before initialization. Moved only that query after SDK initialization before
running all three audible trials; it is not an audio-quality trial.

Interpretation: strong reproducible evidence that send-chain implementation
matters under these conditions. Production environment setup and receive-audio
warm-up did not remove the stock-path cutoff. This does not isolate silence
mixing, pipeline placement, queueing, or timing as the individual cause, nor
prove packet loss or receiver underruns. No permanent patch is approved.
Temporary A/B harness files remain for reproducibility and require confirmation
before deletion; the native probe listener remains unloaded.

### September 12 Service Compatibility Deployment

After the controlled A/B/A results, the user authorized reusing the existing
legacy override on the m1max service only. Added `--audio-send-chain stock|legacy`
to the service and provisioner; generic default remains `stock`. The existing
candidate launchd plist explicitly selects `legacy` and retains Interactive
scheduling. No production release/config, installed SDK, native wheel, or robot
configuration was changed.

The probe imports the existing `audio._patch_bin_add_check()` before constructing
the real SDK. It verifies the marker, rejects unvalidated SDK versions (only
1.10.0 accepted for legacy), and rejects stock requests after legacy was installed
in the same process. This is a process-wide compatibility choice, not a live
session toggle. Source code for the override was not duplicated or rewritten:
the isolated candidate stages the frozen release's unchanged `audio.py`,
`audio_pacing.py`, and `robot.py` modules alongside the updated service modules.

Offline verification: 57 focused control/settings/probe tests passed; Ruff and
whitespace checks passed. A deployed import-only check created no SDK connection
and confirmed send-method SHA-256
`81fb796e96548e5818e23adba42871128d655d2237e8e8e6a1e5a058196d8613`, matching B.
The listener restarted as PID 3084 on port 8876; native app status was null.
Official UI listening/Stop acceptance remains pending. Full receptionist service
integration is still out of scope for this bounded prerecorded-WAV probe.

Rollback requires native app Stop and a fresh service process with
`--audio-send-chain stock`. The old plist was retained on m1max as
`com.reachy.reception.native-probe.pre-legacy-20260912.plist` beside the active
probe plist. Do not edit installed SDK files or try to undo the class override
inside a running process. Temporary A/B scripts/config copies and all evidence
remain; no files were deleted during this update.

### September 12 First-Sound Lead-In Experiment

The user confirmed the official UI probe with `legacy` no longer cut off the
"clinic"/"how" transition, but reported that the first sound of "welcome"
appeared missing. Logs showed the legacy helper active and full WAV submission
after send-chain setup, within the first approximately one second after SDK
construction. No early flush or startup send-chain error was logged. This is
consistent with an output-startup issue but does not establish its cause.

User authorized a test-only silent lead-in comparison. Added
`--probe-lead-in-ms` (0-1000 ms, default 0) to service/provisioner. The candidate
listener explicitly uses 300 ms: 4,800 float32 zero samples prepended in memory
to the unchanged speech samples, submitted together once. Original WAV and
production configuration remain unchanged. Total queued duration is 4.524 s.
This is output priming, not merely a wall-clock sleep; it delays speech onset
and is not approved as a production default.

62 focused tests passed; Ruff/plist validation passed. Native app was stopped
before deploying and restarting only the waiting listener. The prior zero-prefix
plist is retained as `com.reachy.reception.native-probe.pre-lead-in-20260912.plist`.
Official UI listening result for the 300 ms prefix is pending. No automated
physical replay or file deletion was performed during this change.

### Deferred: Separate Reception Stop From Robot Sleep

After learning this is official behavior, the user chose to leave it unchanged
for now and continue the audio probe retry. Do not block current acceptance or
modify daemon behavior for this request without revisiting that decision.

Our native app and probe do not issue `goto_sleep` or daemon-stop requests.
The official app manager releases its managed app slot when the process exits.
The daemon's `_on_robot_slot_free` callback calls `request_idle_reset`, normally
using `IDLE_RESET_DEBOUNCE_S = 1.5`, and then `reset_to_sleep`. This applies to
normal exits as well as failures. The control UI's app Stop invokes
`POST /api/apps/stop-current-app`; that route does not expose a leave-awake flag.

Therefore removing sleep calls from our app cannot meet the requirement: no
such calls exist there. A supported daemon/API option or an explicitly reviewed
upstream change is needed to distinguish deliberate app Stop from fault cleanup.
Do not patch installed SDK/daemon internals, send delayed wake commands to race
the sleep reset, or keep an apparently stopped app alive solely to hold its slot.
No daemon/control-app code or runtime behavior was changed for this finding.

## September 12 Step 4: Shared Runtime Sessions

The user accepted the 300 ms probe lead-in listening result and reported clean
Stop during playback, media disconnect, and another Start. This closes the
attended probe checkpoint, not full receptionist acceptance. The lead-in remains
probe-only; no silence prefix was added to live TTS.

### Implementation

- `official_runtime.live_app.run_live_session` is the common entry for the
  existing CLI and the service. The CLI keeps its timed-run arguments, signal
  handlers, normal output drain, and external OPS supervisor. No CLI subprocess
  or OPS launch is used by the native path.
- `reception_service --mode reception` explicitly selects the new adapter.
  `mock` and `av-probe` remain separate. Listening creates no robot connection;
  authenticated native Start selects one server-owned configuration.
- `reception_runtime` runs one session in a dedicated thread with its own
  asyncio loop. SDK/model initialization and the existing processing loop stay
  off the control WebSocket loop. This is isolation within one process, not a
  new microservice or another model backend.
- Existing profile composition, date/tool instructions, approved `none` or
  `time-web` tool selection, S2S coordination, policy ticks, conversation latch,
  serial/broker vision paths, and artifact generation are reused. The backend,
  dependency pins, policy thresholds, and TTS settings are unchanged.
- Server configuration explicitly names the profile and its source format,
  tools, vision policy, backend endpoint, robot host, and artifact root. Native
  requests cannot supply paths, profile contents, credentials, or runtime flags.
  See the non-secret template in `native_app/reception-runtime.example.json`.
- Control `session_id` maps to artifact `run_id = native-<session_id>`. A session
  records this binding and uses existing manifests/audio/vision/events. Existing
  run manifests cannot be overwritten by repeating an old session ID. Media
  liveness uses input callbacks even when video recording is disabled.

### Stop And Ownership

Stop rejects new audio writes and queued policy events, cancels the runtime's
tasks and cues, closes its S2S connection/tool coordinator, clears the SDK player,
disconnects this SDK client, and closes artifacts. Shutdown fallback registrations
also cover partial startup. Blocking initialization is awaited before ownership
is released, so a late SDK connection cannot become an unowned running session.
The service handles SIGTERM through this lifecycle as well as native Stop.

No normal service cleanup calls `/api/media/release`, stops the daemon, sends
sleep, or stops the separate S2S service. Shared desktop preview must remain
available. Existing cue-rest commands are retained; official app-manager idle
behavior is outside this service.

The service checks for older CLI live processes before SDK initialization.
Updated CLI and service runtimes also share an advisory lock at
`~/.local/state/reachy-reception/runtime.lock` (override:
`RECEPTION_RUNTIME_LOCK_PATH`). The lock file is retained, not deleted; ownership
is the OS lock, not file existence. This coordinates one host/user only. Older
frozen CLIs do not honor it, and other computers are not covered: deployment must
still prevent simultaneous owners operationally.

Runtime health reuses startup/event-loop/audio/video thresholds from the existing
supervisor. The service reads the in-memory liveness snapshot; it does not add
another filesystem heartbeat writer. Control heartbeats remain a separate lease.
On runtime error or incomplete cleanup, the service latches `faulted`. The
reception runtime records eventual worker completion independently of its async
wrapper: wrapper cancellation alone does not prove release of robot resources.
A subsequent explicit Start can clear the latch only when the worker returned
without errors, with no recorded stop-callback failures, and the controller task ended.
Pending cleanup refuses Start with `cleanup_pending`; failed or unknown cleanup
requires operator review. Runtimes without this completion evidence (including
the AV probe) remain latched. There is no automatic reconnect or restart.
Review the service log and confirm ownership ended before using the CLI.

### Limits And Next Gate

An in-process worker cannot be forcibly killed safely. Native code holding the
GIL can also delay the control loop, and an SDK/native call can outlive the
cleanup budget. Fault latching prevents reuse in that service, but is not proof
of physical silence or a replacement for a process supervisor. Managed service
fault recovery, release environment, Interactive scheduling, and shutdown budget
verification belong to Step 5 and later live acceptance.

Offline tests exercise the actual runtime assembly with fake robot/backend I/O,
real profile/tool composition, and fake vision boundaries. They cover repeated
Start/Stop, interrupted SDK startup, backend startup failure, cleanup errors,
control responsiveness during blocking work, liveness faults, exclusive ownership,
and CLI option preservation. Existing runtime, detector, broker, and control tests
provide regression coverage. These tests do not run RF-DETR/DINO inference or
prove physical playback/Stop timing.

Verification on September 12: `.venv/bin/python -m pytest -q` completed with
470 passed, 1 skipped, 36 deselected. Hardware and optional LiveKit cases were
excluded by the repository's default marker filter. Ruff on the changed runtime,
service, and test modules and `git diff --check` passed. The new shared-runtime
test module contains 23 cases; no robot/provider endpoints were contacted by it.

No new dependencies, robot installation, deployment, service restart, profile
edit, backend restart, or physical test was performed for Step 4. Next is Step 5:
embedded UI/config/status and managed service packaging/rollback, reviewed before
deployment. The current production release and AV-probe listener remain unchanged.

## September 12 Step 5: UI And Candidate Packaging

Implemented locally after user approval. See the
[native-service runbook](reception-native-service-runbook.md) for preparation,
deployment gates, status semantics, diagnostics, retention and rollback.

- Embedded read-only operator status replaces the minimal JSON-style view. It
  shows selected configuration, readiness/terminal reason, elapsed time, input and
  processing health, recording settings and identifiers. Official Start/Stop remain
  the only run controls. One reviewed server-owned configuration is fixed per run;
  no live credential/tool/config editing or arbitrary configuration discovery.
- A bounded status snapshot is attached to the existing control heartbeat reply;
  there is no new media channel or model task. Stale control/browser connection
  state must not leave a green Ready/Receiving display. Health is sender/runtime
  evidence, not physical speaker confirmation.
- Added private lifecycle receipts and a candidate bundle generator. It freezes
  named runtime options, verifies wheel/source matching and records release hashes.
  It creates a separate Interactive launchd plist, never loads it, and refuses dirty
  source or an existing output directory. Environment/provider secrets stay in the
  existing private environment, not a newly generated `.env` or robot config.
- Native wheel 0.1.2 contains only the control client/UI. The service code remains
  in the m1max release. No app-store publishing or native inference dependencies.
- Report-only retention now includes candidate service logs/receipts under the
  selected artifact root. Deploying the matching scanner is still pending.

Actual m1max bundle generation requires a reviewed commit and exact candidate
venv/config/TLS paths. Dependency/import checks, listener installation and native
wheel/config update happen only in an approved deployment window. Step 6 is the
attended full receptionist acceptance; neither mock process tests nor the earlier
WAV probe substitute for it. No robot/m1max state was changed for Step 5.
