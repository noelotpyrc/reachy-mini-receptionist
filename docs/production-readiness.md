# Production Readiness

**Status:** approved for assisted clinic production; not yet approved for unattended production

**Current phase:** assisted production operation

**Updated:** 2026-09-05

This document is the promotion checklist for running the receptionist in the clinic for an extended
period without a developer onsite. It owns pass/block status. Detailed implementation and operating
instructions remain in the linked specifications and [runbook](runbook.md).

The approved S2S migration was promoted on 2026-09-05 after staging, live backend acceptance,
profile/tool review, and managed deployment checks. Further STT/LLM/TTS behavior changes require
reopening backend evaluation explicitly. See [production promotion](s2s-production-promotion.md)
for the exact deployed combination and evidence.

## Current Baseline

- Active production release: the immutable revision selected by
  `/Users/leon/.config/reachy-reception/active-release`, validated by `reception-prod` before every
  command. Current app: `ce95a49`. It uses Python `3.12.13`, Reachy SDK `1.10.0`, and the
  lock-enforced environment.
- The previous `37c7042` app and its saved production configuration are the immediate
  rollback for the Sohee delivery change; the backend revision is unchanged.
- The previous `7840866` frozen release and saved production configuration are the immediate
  rollback target for the Hermes route. The live-validated `7b2600e` remains an older fallback.
- The older `87d35ba` and `b7520a0` frozen releases remain available as additional fallbacks.
- Recovery and rollback: pre-removal source is tagged `legacy-daemon-last` at `260e2f2`; the previous
  clean release at `612ea43` and the dirty deployment checkout remain intact.
- S2S backend: `speech-to-speech==0.2.12`, fork SHA `2e4449c345c305e4ee6b9761f86c1849bbf3cb08`,
  listening only on `127.0.0.1:8765`, managed by launchd as `ProcessType=Interactive`.
  Backend pins: MLX `0.31.1`, mlx-audio `0.4.2`, mlx-lm / mlx-metal `0.31.1`.
- Agent context: client-owned private Hermes-source profile composed into session instructions.
  Tools default to `time-web`; explicit `none` disables them. The compatibility Hermes service
  on `127.0.0.1:8642` is preserved but is not used or required by the direct production route.
- Direct provider/model: OpenRouter with `openai/gpt-5.6-luna` as the configured direct model.
- Policy speech: fixed greet/goodbye text bypasses the LLM; audio synthesis is still sampled.
- Shared Qwen voice: Sohee with the approved moderately brisk instruction and the pinned
  mlx-audio temperature default of `0.9`. Volume and generation limits are unchanged.
- Visitor policy default: `door-v4-20260827`, using presence-overlap greet and direct
  person-to-door distance-crossing goodbye. `legacy` and door v1-v3 remain explicit rollback
  profiles.
- Operational surface: `ops_core` plus `reception-ops`; structured status and physical-action
  authorization are implemented.
- Diagnosis: raw/derived artifacts, audio review, offline Rerun review, door-policy review, and
  latest-run pointers are available.

## Promotion Gates

| Area | Gate | Status | Evidence / remaining work |
| --- | --- | --- | --- |
| Release | Immutable product revision, reproducible venv, documented rollback | **Pass / activated** | App `ce95a49` and backend `2e4449c` are frozen separately. `reception-prod` rejects mutable/dirty/mismatched releases. App `37c7042` plus saved configuration restores the prior voice delivery; `7840866` plus its configuration restores the Hermes route. |
| Backend | Reproducible pinned runtime and production smoke | **Pass / frozen** | Both dependency checks and 375 m1max tests passed. Managed private-profile text, time/web tools, fixed policy TTS, shutdown, restart, and trace health passed on 2026-09-05. |
| Robot lifecycle | Remote start, stop, sleep, and machine verification | **Pass for assisted use** | OPS start/stop lifecycle works and leaves the backend warm. Managed S2S restart now waits for complete launchd unload, process exit, and port closure before starting. Physical runner restart remains operator-authorized. |
| Visitor behavior | Greet, goodbye, and wave-chat accepted with real visitors | **Accepted for first production pass** | Door v4 and the current chat backend are frozen at the user-accepted behavior; further tuning is deferred rather than a launch blocker. |
| Long-run behavior | Multi-hour run with conversations and no wedged subsystems | **Pass for assisted use** | Recent two-hour and shorter recorded runs completed cleanly with advancing broker/media counters and finalized artifacts. Policy/chat quality is accepted separately for the first pass. |
| Startup | Bounded, observable transition to ready | **Pass / activated** | Frozen release `4c28a3e` reached ready in `112.301 s` on its first ordinary start and `9.827 s` on its second, with advancing audio/video, zero heartbeat-writer errors, and clean shutdown. |
| Session duration | First-class run-until-stopped mode | **Deferred to control app** | Assisted CLI shifts use a deliberate fixed duration, currently eight hours. The reception control app will start an unlimited session that ends through End Reception or Emergency Stop; this is not a blocker for assisted production. |
| Recording integrity | Audio/video/capture finalize and retain diagnosable timing | **Accepted limitation** | Fixed-`5 FPS` MKVs play faster than wall time, but frame order is intact and JSONL sidecars retain the timing source of truth used by replay/Rerun. Use MKVs only for qualitative review; map a reported player position to frame index and then sidecar `ts`. |
| Crash recording | Artifacts remain finalized or explicitly interrupted after runner failure | **Pass for assisted use** | Controlled media loss in `official-live-20260830-085651` finalized the enabled artifacts and preserved the terminal fault. If a hard kill prevents runner cleanup, the detached supervisor retains interrupted status and open artifact paths. A recorder sidecar remains a deferred enhancement for hard-kill finalization. |
| Privacy | Approved raw-data policy, access boundaries, and retention | **Pass / active** | Production records audio and derived vision diagnostics but defaults raw MKV video off. Audio/video and backend traces older than 30 days are reported daily on m1max for reviewed cleanup; no automatic deletion is allowed. Private profiles and credentials remain outside Git. |
| Monitoring | Backend, provider, runner, media flow, artifacts, disk, and robot health | **Pass / accepted** | Aggregate status checks OpenRouter authentication, S2S, Interactive scheduling, disk, retention, runner heartbeats, and media faults. Hermes is required only for its compatibility route. Structured backend trace health reports writer status, drops, and errors. Controlled media-loss acceptance remains valid. |
| Supervision | Services recover safely after machine/process or media failure | **Pass / fail-stop accepted** | Hermes and S2S launchd `KeepAlive` restart was verified on 2026-08-29. LaunchAgent replacement now waits for unload before bootstrap, closing the observed activation race. Controlled media loss stopped and cleaned up the physical runner without automatic restart on 2026-08-30. |
| Remote access | Authenticated, auditable, least-privilege control | **Blocked for non-technical users** | SSH/Tailscale plus OPS is acceptable for assisted production. No remote operations API or operator UI exists yet. |
| Emergency handling | Idempotent remote stop and documented local fallback | **Pass for assisted use** | `emergency-stop` exposes the existing bounded shutdown path, keeps backend services warm, and has documented timeout, verification, repeat, and local-m1max fallback behavior. Controlled media loss exercised the same bounded fail-stop and cleanup path. |

All gates required for assisted production pass, are accepted with a documented limitation, or are
explicitly deferred outside this pass. Run `official-live-20260901-170248` and the operator's audio
acceptance completed the assisted promotion review. The operator web UI, run-until-stopped mode,
chat endpointing robustness, and recorder-sidecar hard-kill enhancement remain follow-up work and
are not blockers for assisted operation.

## Latest Acceptance Evidence

### Sohee Delivery Follow-Up (2026-09-05)

App `ce95a49` adds optional instruction forwarding; the backend remains `2e4449c`.
The new frozen app's package inventory matches `37c7042` exactly. Launcher and OPS
regressions passed locally and on m1max (73 tests each), along with shell syntax and
targeted Ruff checks. Managed S2S restarted with Interactive scheduling. Greet,
goodbye, and news requests completed with nonempty audio and exact transcript events;
the pool returned to idle, and trace drops/write errors remained zero. These checks
do not prove physical playback or complete spoken wording.

The operator accepted the brisk news samples offline. Physical listening with this
setting remains for the next assisted clinic run, planned for September 6. Check
delivery in ordinary chat and a longer search answer, including its ending and the
robot's response to follow-up speech. This is not a repeat migration benchmark.

Known residuals: missed post-response speech is unresolved and explicitly deferred
([incident](robot-runtime-debugging.md#september-5-missed-speech-after-news-playback));
the news cutoff is consistent with the internal Qwen generation ceiling, which was
not changed ([incident](robot-runtime-debugging.md#september-5-news-tts-cutoff)).
The voice instruction is not recorded as a truncation fix. Neither a VAD change nor
a generation-limit patch is part of this release. Assisted production remains the
approved scope, not unattended production. See the
[voice deployment record](s2s-production-promotion.md#sohee-delivery-configuration-september-5)
for configuration and rollback evidence.

### Client-Owned Agent Promotion (2026-09-05)

The user accepted the migrated backend's two live runs and subsequent seven-case private
profile/tool regression. Managed promotion then passed from frozen app `37c7042` and backend
`2e4449c`: real clinic facts, one time call, one real Firecrawl call, exact greet/goodbye TTS,
complete shutdown and restart, direct-provider health, and trace retention reporting. Trace
drops and write errors were zero. The robot runner was not started during deployment checks.
The migrated real profile and tools have text acceptance; this promotion did not repeat physical
speaker or live visitor testing. Detailed evidence and rollback paths are in the
[promotion record](s2s-production-promotion.md#retry-2026-09-05).

### S2S launchd scheduling regression (2026-09-01)

The S2S service was initially installed with launchd `ProcessType=Background` on 2026-08-29. That
classification applies resource limits intended for non-user-requested work. It reduced the unchanged
Qwen3-TTS/MLX stack from its June foreground baseline of roughly `1.9-2.5x` realtime to
`0.7-0.84x`; long responses accumulated about `1.1-1.2 s` of modeled audio-supply deficit and could
sound choppy. Restarting the background-class service did not improve throughput.

A controlled test changed only the installed S2S process type to `Interactive` and repeated the same
30 exact-text generations with robot and vision workloads disabled. All 30 transcripts matched,
first-audio latency fell from roughly `485 ms` to `154-160 ms`, measured delivery rose to
`2.72-2.91x`, and every sample had zero modeled supply deficit. Qwen's backend RTF returned to
`2.3-2.6x`, consistent with the earlier foreground baseline. The S2S LaunchAgent therefore uses
`Interactive`; Hermes and maintenance jobs remain `Background`.

**Full-stack acceptance:** two-hour run `official-live-20260901-170248` exercised the production
robot, broker-v1 vision stack, Hermes conversation path, and Qwen TTS from frozen release `7b2600e`.
It completed normally with 12 completed responses and one canceled response. For the 12 completed
generations, backend Qwen RTF was `1.71-2.02` with P50 `1.83`; all 13 delivered audio streams stayed
above realtime. While audio was being generated, the retained policy-frame gaps had P95 `0.418 s`
and max `0.540 s`. Gesture processing completed all `80,578` published frames with zero drops or
failures; the latest-frame policy consumer completed `33,983` frames at `4.707 FPS`, and the policy
event sink dropped zero of `359,499` events. Artifacts closed, heartbeat writes had zero errors,
and bounded robot cleanup succeeded. The operator confirmed that playback was smooth. This closes
the full-stack scheduling acceptance for assisted production.

### Launchd activation race (2026-09-01)

Replacing a loaded LaunchAgent definition exposed a lifecycle race: `launchctl bootout` returned
before the old S2S job disappeared from `launchctl print`. The installer therefore observed the old
job as loaded, skipped bootstrap, and then attempted to kickstart a target that finished unloading
between those operations. The active release changed while S2S remained unloaded.

The installer now waits for the old target to disappear before replacing/bootstraping its plist,
and it does not immediately kickstart a newly bootstrapped `RunAtLoad` job. OPS backend restart also
waits for launchd unload, backend-process exit, and port closure, and stops without starting a new
job if that transition times out. Production status reads the installed S2S plist and reports
degraded unless `ProcessType` is exactly `Interactive`.

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

**Implementation status (2026-08-27):** OPS now removes GStreamer wheel-generated registry and
plugin paths before launching the supervisor, and the supervisor repeats that cleanup immediately
before launching the media child. Unrelated diagnostics such as `GST_DEBUG` remain available. This
ensures the selected child interpreter applies its `.pth` environment exactly once. Focused OPS,
supervisor, liveness, and runtime tests pass. A local venv with no registry cache successfully
initialized GStreamer `1.28.3` and created one normal registry file without manual prewarming.
Fresh-release m1max and robot-media acceptance passed on 2026-08-30 from frozen release `4c28a3e`.
No manual registry prewarm was used. The first ordinary start reached `ready` in `112.301 s`; the
second reached it in `9.827 s`. Both runs advanced audio/video heartbeats and shut down cleanly.

The accepted fix gives the live child a clean, deterministic GStreamer environment rather than
passing interpreter-mutated values through each launcher layer. OPS removes bundle-managed
GStreamer variables from the supervisor environment, and the supervisor launches the child from
the clean environment stored in its launch specification. The fresh and warm starts above satisfy
the acceptance criteria without manual cache preparation. The optional Python-plugin linkage
warning is nonblocking when that incompatible optional plugin directory is excluded from scanning.

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
initial `180 s` startup, `5 s` heartbeat-file, and `8 s` source/event-loop thresholds are
configurable. Four retained healthy m1max runs had maximum audio and video gaps of `0.484 s` and
`4.216 s`.

**Controlled media-loss acceptance (2026-08-30):** run `official-live-20260830-085651` was allowed
to reach `ready` with advancing audio/video, then robot media was deliberately released through the
daemon API at `1788094639.854`. Both source sequences stopped; the supervisor recorded
`media_liveness_fault` / `audio_stale`, and the runner closed its artifacts `8.843 s` after
injection. The terminal record and bounded cleanup completed after `12.586 s`, with no forced kill.
No runner or replacement session remained; robot media was released, motors were disabled, no move
was active, and Hermes/S2S remained healthy. Aggregate status retained the fault as
`degraded` / `stopped_faulted`, as designed.

**Startup-liveness correction (2026-08-30):** non-ready phases now receive the full startup grace
before the supervisor reports `startup_stalled`; the strict heartbeat-file threshold begins after
`ready`. This prevents synchronous native model initialization from bypassing startup grace. The
heartbeat writer also retries filesystem failures instead of terminating its thread, logs the first
and every tenth consecutive failure, and reports cumulative write errors in its next successful
snapshot. The default startup grace is `180 s`, based on a measured valid startup of `113.2 s`.
Live acceptance used frozen release `4c28a3e`: runs `official-live-20260830-084803` and
`official-live-20260830-085318` reached `ready` in `112.301 s` and `9.827 s`, respectively. Both
reported zero heartbeat-writer errors, advancing audio/video, closed artifacts, and successful
bounded robot cleanup.

**Normal-stop correction (2026-08-27):** when a timed input source ends, the live runtime now enters
`stopping` before its bounded output-drain and artifact-finalization period. The supervisor continues
to enforce stale-source thresholds while `ready`, but no longer misclassifies intentional microphone
closure during normal shutdown as `audio_stale`. Live run `official-live-20260830-135154` used a
30-second duration override: input ended normally, phase changed to `stopping`, the 20-second output
drain completed without a stale-source fault, and terminal status was `complete` / `completed` with
closed artifacts and successful robot cleanup.

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

Fail-stop acceptance now includes offline starvation tests and controlled live robot-media loss.
Status faulted within the configured bound, left no live runner, and finalized artifacts. Bounded
restart remains deferred and was not exercised; if later approved, it must create one healthy
replacement run with separate artifacts and no duplicate policy speech caused by stale state.

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

Frozen release `4c28a3e` completed release and startup acceptance on 2026-08-30:

- `uv lock --check` and `uv pip check` passed in Python `3.12.13` runtime and validation
  environments reproduced from `uv.lock`.
- Full default suite: `305 passed, 1 skipped, 36 deselected`; focused Ruff passed.
- Full Ruff retained the same 23 pre-existing findings as the preceding release and introduced no
  new finding.
- Two ordinary OPS starts reached `ready` with advancing microphone and camera heartbeats; both
  normal stops closed artifacts, released media, disabled motors, and left Hermes/S2S healthy.
- Controlled media-loss run `official-live-20260830-085651` faulted on `audio_stale`, closed
  artifacts, completed cleanup without a forced kill, retained a diagnosable terminal status, and
  did not create a replacement physical run.
- Timed run `official-live-20260830-135154` transitioned through `stopping`, completed its bounded
  output drain without a false media fault, closed artifacts, and returned aggregate status to
  `ok` with no remaining runner.

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
3. Use the fixed eight-hour production duration for the first assisted shifts. Explicit unlimited
   semantics are deferred to the reception control app, where a run continues until End Reception
   or Emergency Stop is invoked. Normal timed stop was accepted in
   `official-live-20260830-135154`. **Complete.**
4. Activate the frozen release through `reception-prod` and its private production configuration;
   verify both a fresh and warm ordinary startup. Accepted with release `4c28a3e` in
   `official-live-20260830-084803` and `official-live-20260830-085318`. **Complete.**
5. Validate aggregate Hermes/provider/service/disk/retention health on m1max. **Complete.**
6. Confirm launchd restarts Hermes and S2S without enabling automatic physical-runner restart.
   **Complete.**
7. Treat current visitor-policy and chat behavior as accepted for the first production pass.
   **Complete.**
8. Verify bounded media fail-stop and physical cleanup without automatic restart. Accepted in
   `official-live-20260830-085651`. **Complete.**
9. Run one assisted clinic shift with remote status checks and the tested emergency-stop procedure.
   Run `official-live-20260901-170248` completed normally after two hours. **Complete.**
10. Review evidence and explicitly promote or roll back. Audio playback was accepted by the operator;
    retained media, vision, TTS, and cleanup evidence passed. **Promoted for assisted production.**

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
