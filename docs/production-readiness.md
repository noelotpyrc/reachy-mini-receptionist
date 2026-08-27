# Production Readiness

**Status:** not yet approved for unattended clinic production

**Current phase:** assisted production preparation

**Updated:** 2026-08-25

This document is the promotion checklist for running the receptionist in the clinic for an extended
period without a developer onsite. It owns pass/block status. Detailed implementation and operating
instructions remain in the linked specifications and [runbook](runbook.md).

Backend feature development is paused at the current deployed configuration. Production work may
make reliability, security, lifecycle, and observability fixes, but should not change the selected
STT/LLM/TTS behavior without reopening backend evaluation explicitly.

## Current Baseline

- Prepared m1max reliability release: clean checkout at `3449f8e` in
  `/Users/leon/projects/reachy_mini_receptionist_release_3449f8e_frozen`. Lock-enforced runtime and
  validation environments, full pytest, and changed-file Ruff passed. Media-liveness live
  acceptance is pending because the robot API was unreachable during deployment.
- Previously live-validated assisted release: `749ee18` in
  `/Users/leon/projects/reachy_mini_receptionist_release_749ee18_frozen` remains the rollback target.
- Recovery and rollback: pre-removal source is tagged `legacy-daemon-last` at `260e2f2`; the previous
  clean release at `612ea43` and the dirty deployment checkout remain intact.
- S2S backend: `speech-to-speech==0.2.10`, fork SHA `a963ca68b9aa3599b7ea5eeabb9505a68263fbff`,
  listening only on `127.0.0.1:8765`.
- Agent wrapper: production Hermes profile on `127.0.0.1:8642`; clinic context remains outside Git.
- Direct provider/model: OpenRouter with `openai/gpt-5.6-luna` as the configured direct model.
- Policy speech: fixed greet/goodbye text uses deterministic TTS and does not invoke an LLM.
- Visitor policy candidate: `door-v1-20260805`, combining semantic door movement with person-door
  interaction.
- Operational surface: `ops_core` plus `reception-ops`; structured status and physical-action
  authorization are implemented.
- Diagnosis: raw/derived artifacts, audio review, offline Rerun review, door-policy review, and
  latest-run pointers are available.

## Promotion Gates

| Area | Gate | Status | Evidence / remaining work |
| --- | --- | --- | --- |
| Release | Immutable product revision, reproducible venv, documented rollback | **Pass for assisted use** | Candidate `3449f8e` uses Python `3.12.13` and lock-enforced non-editable runtime/validation venvs; `749ee18` remains the live-validated rollback. Replace commit-named/manual activation with a stable production release mechanism before non-technical operation. |
| Backend | Reproducible pinned runtime and production smoke | **Pass / frozen** | Backend setup script, runtime metadata, Hermes text/integration tests, and deterministic policy-TTS benchmark completed. Add wrapper/provider checks to aggregate health. |
| Robot lifecycle | Remote start, stop, sleep, and machine verification | **Pass for assisted use** | OPS start/stop lifecycle works and leaves the backend warm. Physical runner restart must remain operator-authorized. |
| Visitor behavior | Greet, goodbye, and wave-chat accepted with real visitors | **Blocked** | Door policy passed captured offline evaluation. A controlled live door-entry, conversation, and exit sequence is still required. |
| Long-run behavior | Multi-hour run with conversations and no wedged subsystems | **Blocked** | `official-live-20260806-114813` ran for about 96 minutes but observed no people or conversations, so it tested idle stability only. |
| Startup | Bounded, observable transition to ready | **Blocked** | Fresh release startup took roughly two minutes while DINO/RF-DETR and media initialized. The 2026-08-25 frozen release also exposed a GStreamer registry/bootstrap defect that can crash media initialization in a new uv environment. Expose progress, define a timeout/fault state, and replace the registry prewarm workaround described below. |
| Session duration | First-class run-until-stopped mode | **Blocked** | Current operation uses a very large numeric duration as an indefinite-run workaround. Implement explicit unlimited semantics. |
| Recording integrity | Audio/video/capture finalize and retain diagnosable timing | **Accepted limitation** | Fixed-`5 FPS` MKVs play faster than wall time, but frame order is intact and JSONL sidecars retain the timing source of truth used by replay/Rerun. Use MKVs only for qualitative review; map a reported player position to frame index and then sidecar `ts`. |
| Crash recording | Artifacts remain finalized or explicitly interrupted after runner failure | **Partial / acceptance pending** | The detached session supervisor now records whether the runner-owned manifest closed cleanly or remained interrupted, including open artifact paths. A recorder sidecar is still required if hard-kill finalization rather than explicit interruption is required. |
| Privacy | Approved raw-data policy, access boundaries, and retention | **Decision required** | Continuous audio plus video is about `0.4 GB/hour` in the latest run and may contain sensitive clinic information. Decide production defaults and retention before unattended recording. |
| Monitoring | Backend, Hermes, provider, runner, media flow, artifacts, disk, and robot health | **Partial / acceptance pending** | Source-level audio/video ages, event-loop age, and retained terminal faults are implemented locally. Validate thresholds and fail-stop on m1max. Wrapper/provider health and disk headroom remain missing. |
| Supervision | Services recover safely after machine/process or media failure | **Partial / acceptance pending** | A detached session supervisor now performs fail-stop, bounded robot cleanup, terminal recording, and active-state retirement while leaving the backend warm. Controlled live fault acceptance remains; automatic physical restart stays disabled. Backend and wrapper still need managed service definitions. |
| Remote access | Authenticated, auditable, least-privilege control | **Blocked for non-technical users** | SSH/Tailscale plus OPS is acceptable for assisted production. No remote operations API or operator UI exists yet. |
| Emergency handling | Idempotent remote stop and documented local fallback | **Partial** | `stop-session` and `shutdown` exist. Define an operator-visible emergency stop, timeout behavior, and recovery instructions. |

## Latest Long-Run Evidence

### GStreamer uv startup incident (2026-08-25)

Fresh `.release-venv` environments built by `uv sync --frozen` could pass package validation and
still crash when the official runtime initialized SDK media. The failing run
`official-live-20260825-124253` stopped in `phase=starting` with return code `255`; its log showed
the external GStreamer plugin loader failing and an in-process segmentation fault while scanning
`gstreamer_python/lib/gstreamer-1.0/libgstpython.dylib`.

This is not evidence that uv selected inconsistent dependency versions. The failed release and the
previously working release used the same uv-managed CPython `3.12.13`, GStreamer `1.28.3`,
`gstreamer-python 1.28.3`, Reachy Mini SDK `1.8.0`, package inventory, and relevant binary hashes.
The defect is in runtime bootstrap and binary loading:

- `libgstpython.dylib` links to `@rpath/libpython3.12.dylib`, which is not present in the expected
  locations in the uv standalone-Python environment. The external scanner therefore cannot load
  that optional plugin.
- The installed GStreamer `.pth` bootstrap prepends its `GST_REGISTRY_1_0` value at every Python
  interpreter startup.
- A physical run starts three nested interpreters: OPS CLI, detached supervisor, and live runtime.
  The inherited registry filename is therefore expanded once per layer. Because
  `GST_REGISTRY_1_0` is one filename rather than a path-list variable, each depth addresses a
  different colon-containing cache filename.
- An older release happened to have the required registry cache from prior starts. A fresh release
  did not, so its live process rescanned the incompatible plugin and could crash before media became
  ready.

The temporary recovery was to initialize GStreamer once with the exact three-layer registry value
before starting the session. After that prewarm, `official-live-20260825-124601` reached `ready`,
with advancing microphone and camera heartbeats. This workaround is release-path-specific and must
not be treated as a production fix.

The permanent fix must give the live child a clean, deterministic GStreamer environment rather than
passing interpreter-mutated values through each launcher layer. In particular, OPS should remove
bundle-managed GStreamer variables from the supervisor environment, and the supervisor should
launch the child from the clean environment stored in its launch specification rather than from
its own post-`.pth` `os.environ`. Acceptance requires a newly built frozen environment with no
pre-existing registry: one normal OPS start must reach `ready`, audio/video sequences must advance,
and a second start must do the same without manual cache preparation. The plugin linkage warning
must either be removed by a compatible package/runtime layout or explicitly proven harmless after
the optional Python-plugin directory is excluded from scanning.

Run `official-live-20260807-110807` exposed a media-liveness failure that process-only health cannot
detect:

- At `2026-08-07 14:51:36 EDT`, the robot WebRTC signaling connection was reset by the remote peer.
  Audio input, camera frames, vision capture, DINO input, gesture recognition, and door-policy
  evaluation all stopped.
- DINO itself was healthy at the boundary (`3905 / 3905`, zero dropped); the runner PID, robot
  daemon, motors, and local backend remained alive.
- OPS continued returning `ok` because it checked the runner PID but not source-frame age.
- m1max recorded transient Wi-Fi transmit failures/retries at the same second, but retained evidence
  cannot distinguish a network-initiated failure from a robot-side signaling reset. Treat the
  immediate cause as terminal WebRTC-session loss and keep the deeper initiator unresolved.

### Media-liveness recovery requirement

**Implementation status (2026-08-07):** the required fail-stop path is implemented locally. The
live child publishes microphone, camera, and event-loop heartbeats independent of recording. A
detached supervisor monitors those signals, terminates a stale child, inspects artifact closure,
performs bounded robot cleanup, retains terminal status, and retires the active state pointer. The
initial `120 s` startup, `5 s` heartbeat-file, and `8 s` source/event-loop thresholds are
configurable. Four retained healthy m1max runs had maximum audio and video gaps of `0.484 s` and
`4.216 s`; a controlled live interruption remains required.

The runtime must track monotonic last-seen timestamps independently for expected audio input and
video input after `software_pipeline_initialized`. Normal frame gaps and startup are not faults;
source age beyond a validated, configurable threshold is. Loss of either source marks the run
degraded, and loss of both marks the WebRTC session terminal. OPS status must surface source age,
the fault reason, and the selected recovery action rather than inferring health from PID existence.

Recovery has two policy modes:

1. **Fail-stop (required default):** emit a structured media-liveness fault, stop the runner through
   the normal lifecycle, finalize or explicitly mark all artifacts interrupted, release media,
   sleep the robot, and leave backend services warm. The fault remains visible through latest-run
   status.
2. **Bounded restart (optional, explicit):** only when the shift was started with an approved
   unattended-recovery policy, stop the failed session completely and start a new run ID with a
   parent/recovery link. Limit attempts and use backoff. Any repeated liveness failure must fall
   back to fail-stop; parallel runners and in-place partial media reconstruction are forbidden.

Acceptance requires offline starvation tests plus one controlled live WebRTC interruption. Status
must fault within the configured bound; fail-stop must leave no live runner and finalize artifacts;
restart mode must create one healthy replacement run with separate artifacts and no duplicate
policy speech caused by stale state.

Run `official-live-20260806-114813` used the clean release with raw audio, video, vision capture,
Grounding DINO door observations, and Rerun streaming disabled.

- Audio and video warmups passed; the runtime reached `software_pipeline_initialized`.
- Door semantic updates detected exactly one door in `4397 / 4397` samples over the analyzed
  50-minute window. The state remained `STABLE` after two startup `UNKNOWN` frames and emitted no
  false policy trigger.
- No person was observed during that analyzed window, so person-door interaction and visitor policy
  behavior were not exercised.
- Graceful OPS shutdown stopped the runner, slept the robot, disabled motors, released media, and
  finalized the artifacts.
- The input loop reported about `5740 s`, but `ffprobe` reports approximately `3311 s` for the MKV.
  This is the accepted fixed-FPS playback limitation: the MKV remains useful for qualitative review,
  while its sidecar timestamps are required for timing and cross-artifact alignment.

## Latest Release Evidence

The non-live release build at `749ee18` completed on 2026-08-06:

- `uv lock --check` passed; runtime installation used `uv sync --frozen --no-editable`.
- Runtime and validation environments use Python `3.12.13`; uv is `0.11.19`.
- `uv pip check` passed in both environments.
- Full default suite: `225 passed, 1 skipped, 36 deselected`.
- Runtime and OPS CLI help smoke checks passed.
- The runtime package versions match `uv.lock`, including `aiortc 1.14.0`, `av 16.1.0`,
  `openai 2.44.0`, OpenCV `4.13.0.92`, and `torch 2.12.1`.
- Full Ruff is not yet clean: 24 pre-existing findings were recorded and no automatic changes were
  applied. This is tracked as cleanup debt rather than evidence of dependency drift.
- `.release-manifest.json` and `.release-packages.txt` record non-secret release provenance locally
  in the ignored release root.
- The first non-frozen build at `/Users/leon/projects/reachy_mini_receptionist_release_749ee18`
  resolved newer transitive packages and is not an accepted release. It remains untouched pending
  explicit cleanup approval.

## Assisted Production Sequence

Complete these in order. Stop and diagnose when a gate fails.

1. Correct active documentation and establish this checklist as the promotion source of truth.
   **Complete.**
2. Accept and document the fixed-FPS MKV limitation; require sidecar timestamps for timing-sensitive
   diagnosis. **Complete.**
3. Implement explicit unlimited session duration and startup progress/fault reporting.
4. Define the production configuration outside Git: release, visitor profile, recording mode,
   retention, provider, voice, and rollback target.
5. Add authenticated health checks for Hermes and the external provider plus media/artifact growth
   checks for an active run. Media-liveness fault and fail-stop are implemented locally; deploy,
   baseline thresholds, and complete controlled live acceptance.
6. Add service supervision for non-physical persistent services. Keep automatic physical runner
   restart disabled unless the bounded-restart policy above receives a separate safety approval.
7. Run one controlled visitor acceptance: door entry, greet, wave-chat, ordinary questions,
   goodbye, and door exit, with artifacts retained for review.
8. Run one assisted clinic shift with remote status checks and a tested emergency stop procedure.
9. Review evidence and explicitly promote or roll back.

## Remote Operations Roadmap

Remote operation is staged so a web app is not a prerequisite for the first assisted production
run.

1. **Assisted production:** use the existing OPS library/CLI over authenticated SSH/Tailscale.
2. **Operations service:** wrap `ops_core` directly with a local authenticated API, asynchronous
   jobs, one operation lock, idempotency keys, progress events, and an audit log. Do not expose an
   arbitrary command endpoint.
3. **Remote CLI:** add `status`, `start-shift`, `end-shift`, `emergency-stop`, and `mark` commands
   against the API; retain the local OPS CLI as the break-glass path.
4. **Operator web app:** expose a narrow Start Shift / End Shift / Emergency Stop workflow with
   Ready, Starting, Live, Stopping, and Fault states. Keep backend/model/profile controls
   administrator-only.

The robot daemon, Hermes port, and S2S backend port must remain private. A future operations service
should bind locally and be exposed only through an authenticated private-network or identity-aware
proxy.

## Explicit Non-Goals For This Pass

- No backend model, prompt, memory, STT, or TTS experiments.
- No public Internet exposure of robot or backend services.
- No automatic physical runner restart unless the bounded media-recovery policy is explicitly
  reviewed and approved; fail-stop remains mandatory and the default.
- No further deletion of code, documents, artifacts, profiles, or database entries without separate
  confirmation. The separately approved Batch C legacy-daemon removal is complete and recoverable
  from `legacy-daemon-last`.
