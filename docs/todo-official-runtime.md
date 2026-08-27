# TODO — post-pivot to the official-runtime architecture

Execution checklist for the official-runtime path (ported conversation app + local
m1max S2S backend) that replaces the legacy reception daemon. Work the items **in order**;
each has a clear *Done when*. Check boxes as you go and leave a one-line note + date.

This is a re-arrangement of the prior 8-item list, reordered so the **loop-accelerating
infrastructure (ops + diagnosis tooling) lands before the experiment-heavy work** — every
UX/model iteration after that is faster with it. Source review: `docs/data-harness.md`,
`docs/live-test-log.md`, and the dependency map in this doc's Phase 1.

## Ground rules (read first)

- **Evidence-based diagnosis, no guessing.** For any physical/audio/video/robot behavior,
  a proposed root cause must be stated as: `claim -> evidence (run_id, artifact path,
  ts-range) -> confidence -> test that confirms/refutes`. If the supporting artifact does
  **not** exist, the only valid next step is *instrument/reproduce first* — never a code fix.
- **Reproduce offline before spending live time.** Anything diagnosable from recorded
  artifacts (Cat-1 raw audio/video, events/policy/realtime JSONL) must be reproduced
  off-robot before requesting a live test. Live time + the user's presence is the scarce
  resource.
- **Confirm before irreversible/unreplayable actions.** Deletions, log/artifact overwrites,
  destructive git ops, and any live test needing a human present all require explicit
  user confirmation.
- **Commit cadence.** Commit + push directly to `main` in small logical commits (single dev,
  no feature branches).

---

## Phase 0 — Validate & secure the current state

### 1. Confirm the recent fixes via live test  `[x]`
**Goal:** Validate the thinking-cue fix (and the startup wave / greet-goodbye rough edge)
on the real robot before building on top of it.
**Steps:**
- Sync to m1max, run via `scripts/m1max/live_ops.sh clean-run` (now enables
  `--capture-vision` by default, so per-frame people/tracks/events land without video).
- Use `scripts/m1max/mark.py` from a second pane to time-stamp UX reactions live.
- Verify: thinking cues **start** after each recognized user turn; **stop** when robot
  speech starts. If movement is still missing, read `start_suppressed` reasons in the
  policy/realtime JSONL.
- Re-check the startup first-wave / greet+goodbye sequence with capture JSONL enabled
  (the 06-22 run couldn't prove the cause without it).
**Done when:** cues fire/stop correctly across several turns *per live feedback*, and the
startup wave/greet-goodbye either behaves or has a capture-backed diagnosis (not a guess).
**Constraints:** **needs the user present to talk/interact — confirm before running.**

### 2. Secure the pivot (commit the uncommitted official-runtime work)  `[x]`
**Goal:** The entire `official_runtime/` subpackage + its tests + `audio_pacing.py` +
`stt_worker.py` + the new docs/scripts are currently **untracked**. The accepted product
architecture lives only in the working tree — get it into history before any deletion.
**Steps:**
- Commit `src/reachy_mini_brain/official_runtime/`, `tests/test_official_runtime.py`,
  `tests/test_audio_pacing.py`, `src/reachy_mini_brain/audio_pacing.py`,
  `src/reachy_mini_brain/stt_worker.py`, the new `docs/*`, and `scripts/m1max/*` in small
  logical commits (e.g. runtime / tests / scripts / docs).
- Run the offline suite first: `.venv/bin/python -m pytest tests/test_official_runtime.py
  tests/test_audio_pacing.py -v` — should pass before committing.
**Done when:** `git status` shows no untracked official-runtime code and the accepted runtime is in a
checkpoint commit. Push separately when the remote sync policy is confirmed for this cleanup pass.
**Note:** A baseline commit of the current state *before* the live test in #1 is also
reasonable (protects the work + gives a clean revert point); ordering here honors the
"confirm the fix first" preference but the commit is independent of the live test.

---

## Phase 1 — Make official-runtime canonical, deprecate legacy

### 3. Accept official-runtime as the primary path  `[x]`
**Goal:** Make the new path the documented default; mark legacy as no longer primary.
**Steps:**
- `docs/runbook.md` already leads with the official-runtime flow — finish the sweep:
  update `README.md` / `docs/robot-guide.md` so `live_ops.sh` + local S2S backend +
  `official-runtime-live` are the documented path, and add a clear "legacy daemon is
  deprecated" banner pointing here.
- Keep the legacy daemon runnable for now (do **not** delete in this step).
**Done when:** a new reader is pointed at the official-runtime path by default; legacy is
labelled deprecated everywhere it's still mentioned as current.

### 4. Houseclean repo structure (deprecate now, delete behind confirmation)  `[x]`
**Status:** completed on 2026-08-06. The final legacy-daemon source snapshot is tagged
`legacy-daemon-last`.
**Result:**
- `official_runtime/` is self-contained **except** two keepers it imports from legacy:
  - `audio_pacing.py` — constants, used by 4 official modules. **KEEP.**
  - `audio.py` — `robot_io.py:53` lazily uses `_patch_bin_add_check`. **KEEP.**
- Removed the 12 legacy daemon/harness modules listed in `docs/legacy-cleanup-plan.md`, their
  manifest test, the `reception` and `review-audio` entrypoints, and the legacy-only `brain` extra.
- Kept `stt.py`, `tts.py`, and `audio.py` for the manual audio CLI and current shared helpers.
- Historical experiments and stale tests remain a separate cleanup decision.

---

## Phase 2 — Build the loop-accelerating infrastructure (before experiments)

### 5. OPS management tools  `[x]`
**Goal:** Remove the ops confusion seen during live tests with a small, clear command set, shaped
so it can later grow into an operator app without building that app now.

**Design — see `docs/ops-design.md`** (settled). In brief: organize around **3 resources × phases**
— resources **Backend OPS** / **Robot OPS** / **Runner OPS** (each owns lifecycle + status; Backend
& Robot persist across runs, Runner is per-run and owns live-ops), composed by thin **pre-run** /
**post-run** workflows. Status is a cross-cutting structured read; the safe-action read-only-vs-
physical flag lives in the API as authorization vs machine verification vs human quality gate.
**Architecture decided:** Python **library + dev CLI** replaces `live_ops.sh` as the source of truth;
app/service deferred, action layer kept transport-agnostic for a future service.

**Done when:** each resource has start/stop/status and each phase is one documented command; a fresh
operator can pre-run → (live) → post-run and read aggregate status without reading code;
OPS writes/reads a latest-run pointer for #6.

**Accepted:** first pass is built and accepted. See `docs/archive/reviews/ops-test-todos.md` for
the completed offline, m1max, robot, and human-gated checks.

### 6. Run-summary / diagnosis visibility (the keystone)  `[x]`
**Goal:** Turn "I have to re-experience the robot" into "I scrub the run timeline." Read-only over
existing official-runtime artifacts — **no robot, no re-recording, no re-execution**; works on
historical runs. (Do **not** port the legacy `review_audio.py`.)

**The design is settled — implement to these specs, don't re-derive them:**
- **`docs/general-timeline-model.md`** — the **model + event allowlist**: 6 actors × spans/markers,
  behavior-grouped policy, `span→0/1 state-scalar` / `marker→TextLog` encoding, the
  `event→behavior` adapter. This is the contract both renderers consume.
- **`docs/rerun-integration.md`** — the **Rerun renderer**: physical/vision layer + the L1 spans as
  state-scalars under `<actor>/<sub-entity>` folders.
- **`docs/conversation-audio-player.md`** — the **in-house conversation/audio player**: aligned input +
  output audio playback, semantic backend lanes, per-response WAV playback, transcript availability,
  and optional STT-recovered transcript sidecars.

**Build order completed for v1:**
1. **Shared parser / allowlist** — read a run's lanes + markers + audio sidecars; keep only the
   model's allowlisted events; reconstruct spans (pair each actor's start/end per turn through the
   `event→behavior` adapter) and collect markers. One reusable layer feeding both renderers.
2. **Rerun renderer** — `<actor>/<sub-entity>` entities; spans → state-scalars, markers → TextLog;
   physical/vision (audio RMS, camera, detections) under their own folders.
3. **Conversation/audio player** — the in-house listening tool (the merged "L2/L3").

**Exposure.** First pass is accepted with the dedicated diagnosis CLIs/tools rather than a unified
operator command. A future `reception-ops review <run_id>` can route to the right renderer, but that
is convenience polish rather than a blocker for #6 v1. Keep the renderers as libraries with lazy
diagnosis-only imports.

**Prototype disposition.** The original `rerun_review.py` flat-firehose prototype was reworked for
v1: the accepted renderer follows the model's actor/sub-entity layout, encodes spans as state-scalars,
and keeps behavior derivation in one vintage-compatible adapter (native `source` ∪ legacy
type→behavior map — see the model doc's policy-refactor note). Its parser/derivation remains the
reusable asset for future renderer work.

**Done when:** on a recorded run you get (a) a Rerun timeline matching the model's actor-folder
layout, and (b) the in-house player where a human can *listen* to input + output audio aligned with
the backend timeline + markers — diagnosing UX without re-running the session.

**Accepted:** first pass is built and accepted. Rerun covers the physical/vision layer and L1 spans;
the audio-review app covers aligned listening, semantic backend lanes, per-response playback,
transcript availability, and STT-recovered sidecars. See
`docs/archive/reviews/audio-review-issues-20260701.md` for the completed audio-review fix tracker.

**Video alignment follow-up:** new `--record-video` runs should use the recorder's per-frame
timestamp sidecar; historical runs may be aligned from `capture/*.jsonl` for the overlapping prefix.
Longer term, if the Reachy SDK exposes true camera/sensor timestamps, carry those through the camera
provider and recorder instead of the current runner-observed timestamp taken after
`get_latest_frame()` returns. **Checked 2026-06-26 — not available:** neither SDK 1.8.1 nor latest
upstream `main` exposes a per-frame timestamp; `media_manager.get_frame()` / `camera_*.read()` return
pixels-only and drop the GStreamer `buf.pts`. It stays the SDK's own unimplemented TODO
(`reachy_mini/utils/rerun.py:166-169`). The only way to get true per-frame `pts` before the SDK adds
it is a **custom media manager** subclass (the SDK supports one — `examples/custom_media_manager.py`)
that pulls `buf.pts` off the appsink — more work than the capture-`ts`/sidecar fix and it lives in our
code, so prefer the sidecar path unless frame-exact alignment becomes essential.

**Video alignment follow-up validation:** record one short new run with `--record-video` after the
sidecar fix, then compare video sidecar timestamps, capture timestamps, and policy/perception event
timestamps. If an overall video lag remains, classify it with evidence before changing code: likely
camera/media buffering, policy/event processing delay, or Rerun playback/render behavior.

**Note:** this is the consumer the marker tool was built for; the long-deferred "merged timeline"
(`data-harness.md` gap #8). Prereq: pinned `rerun-sdk==0.33.1` as the optional `diagnosis` extra,
not a core runtime/ops dep.

**Follow-up — policy refactor to match the timeline model.** The general-timeline model
(`docs/general-timeline-model.md`) groups policy events by *behavior* (`greet` / `farewell` /
`wave_conversation` / `conversation_cue`), but the code emits all reception reactions from one
`ReceptionPolicy` (only `ConversationCuePolicy` is split out). v1 renders this via a renderer-side
type→behavior map. Refactoring `ReceptionPolicy` into per-behavior policies — each emitting its own
`source` — makes the grouping native and deletes the map. Low priority; do after #6 v1.

**Follow-up — audio-review UX polish.** Deferred and not blocking #6 v1:
- Conversation-script panel with per-turn user/assistant rows and row-level playback.
- Optional run-on cluster annotation for speculative-turn transcript drops.
- Additional listening controls such as A/B overlap, loop selected range, and keyboard shortcuts.

### 6a. Media liveness and terminal WebRTC recovery  `[~]`
**Status:** fail-stop implementation, offline tests, healthy-run threshold baselining, and frozen
m1max deployment at `3449f8e` are complete. The 2026-08-27 normal-stop correction transitions
liveness to `stopping` before output drain so timed shutdown cannot be mislabeled `audio_stale`.
A controlled live interruption and normal timed-stop acceptance remain production-blocking.

**Goal:** A live PID must not be reported healthy after audio/video input has stopped. A terminal
WebRTC disruption must either complete a safe fail-stop or, under a separately approved unattended
policy, perform one bounded full-session recovery sequence.

**Evidence:** At `2026-08-07 14:51:36 EDT`, WebRTC signaling was reset by the remote peer. The last
video/capture frame was `1786128695.679` and the last audio frame was `1786128696.144`. DINO had
completed `3905 / 3905` frames with zero drops, but all media-driven processing stopped while OPS
continued to report `ok` from PID existence. See `live-test-log.md` and
`production-readiness.md`.

**Implementation sequence:**
1. **Implemented:** record source-level monotonic timestamps and sequences for expected microphone
   samples and camera frames independently of recording, plus an asyncio event-loop pulse.
2. **Partially complete:** configurable startup and stale-source thresholds are implemented. The
   current `120 s` startup, `5 s` heartbeat-file, and `8 s` source/event-loop defaults are based on
   four retained healthy m1max runs. Their maximum observed audio and video gaps were `0.484 s` and
   `4.216 s`, respectively; controlled live interruption acceptance remains required.
3. **Implemented:** expose heartbeat/source ages through runner and aggregate status; a stale source
   changes active status to `faulting` before process teardown.
4. **Implemented:** a detached per-run supervisor owns the live child, graceful stop/hard-stop
   escalation, artifact close/interruption inspection, bounded robot cleanup, active-state
   retirement, and retained terminal status. The backend remains warm.
5. **Deferred by design:** keep bounded restart behind an explicit recovery policy. A restart must stop the old session,
   use a new run ID linked to the failed run, cap attempts with backoff, and fail-stop on recurrence.
6. **Offline complete:** tests cover startup grace, required/optional source starvation, event-loop
   starvation, artifact close/interruption classification, bounded cleanup, and OPS fault status.
   Run one controlled live WebRTC interruption test after user confirmation.

**Done when:** status cannot remain `ok` beyond the configured liveness bound; fail-stop leaves no
runner/media ownership leak and preserves diagnosable artifacts; any approved restart creates
exactly one healthy replacement and never loops indefinitely.

---

## Phase 3 — Iterate UX & backend with the fast loop

### 7a. Stabilize vision-triggered greet/goodbye policy  `[ ]`
**Status:** implementation and captured offline evaluation complete; controlled visitor live
acceptance pending.

**Goal:** Promote the door-ordered visitor policy only after real entry, conversation, and exit
behavior is accepted without contradictory greet/goodbye speech.

**Current implementation:** `door-v1-20260805` uses continuous Grounding DINO door observations,
RF-DETR person observations, and ordered door-motion/person-interaction evidence. Fixed policy text
uses deterministic TTS. See [`vision-visitor-state-proposal.md`](vision-visitor-state-proposal.md).

**Evidence (2026-07-25 live run `official-live-20260725-111932`):**
- Marker 2 (`11:21:01`, "unwanted goodbye/greet"): track 5 emitted `approach` at
  `ts=1785003657.610` after its detected area jumped to `0.579`, then emitted `depart`
  at `ts=1785003658.964` after the box settled near `0.30`. The person remained
  continuously detected; this was not a departure.
- Marker 3 (`11:24:50`, "unwanted goodbye/greet after waving"): track 7 emitted
  `approach` at `ts=1785003882.447` with area `0.447`, then `depart` at
  `ts=1785003884.035` with area `0.251`. The person remained present near area
  `0.28-0.30` and produced the accepted `Open_Palm` wave at `ts=1785003888.872`.
- The direct policy lane then correctly spoke each event it was given. Hermes and the
  wave-chat conversation were not the source of these two unwanted sequences.

**Implemented result:** logical track handoff, observed/retained state separation, door observation,
door-person interaction metrics, ordered greet/goodbye candidates, live/offline Rerun diagnosis,
and a versioned rollback profile are covered by focused tests and accepted captured clips.

**Remaining steps:**
- Revisit wave-detection reliability; no replacement is accepted yet. A replay-only implementation
  of the Reachy Mini Rock Paper Scissors temporal hand-center algorithm was evaluated against the
  two known close-wave recordings without changing the live `Open_Palm` default:
  - `official-live-20260807-110807`, frames `120-190`: `31 / 71` frames had a hand observation,
    but no two-second window combined the required two direction changes with `0.08` normalized
    displacement; both temporal and static detection emitted zero waves;
  - `official-live-20260825-145234`, frames `4800-4920`: `36 / 121` frames had a hand observation;
    frame `4853` reached two direction changes and `0.0743` displacement, below the algorithm's
    `0.08` threshold; both temporal and static detection emitted zero waves;
  - MediaPipe `VIDEO` mode reduced useful trajectory evidence on both windows. Lowering the
    displacement threshold would recover only the second sample and is not accepted from this
    two-positive set. The temporal path remains available for offline comparison only.
  - **Offline implementation complete:** the versioned frame-broker runtime described in
    [`vision-frame-broker-architecture.md`](vision-frame-broker-architecture.md) provides one
    canonical 15 FPS stream to recording and MediaPipe while RF-DETR/DINO select frame-identified
    lower-rate subsets. Fan-out, overflow, startup ordering, source provenance, policy-event
    serialization, shutdown, manifest counters, and the OPS selector have deterministic tests.
    `serial-v1` remains the rollback default. Next, deploy to m1max, benchmark zero-drop 15 FPS
    recording/MediaPipe with RF-DETR and DINO active, then run controlled wave acceptance.
- **Offline complete:** the two-layer close-person patch rejects contaminated door geometry and
  makes oversized or frame-clipped person interactions policy-ineligible without hiding presence.
  It removes the false greet at frame `155` and false goodbye at frame `195` in
  `official-live-20260807-110807` while preserving four accepted events across the two real-door
  replay clips. The patch is versioned as `door-v2-20260809`, with `door-v1-20260805` retained for
  rollback. Controlled live acceptance remains required before promotion.
- Run one controlled door entry, greet, wave-chat, and door exit with a person onsite.
- Confirm trigger order, policy-speech latency, no duplicate greeting/farewell, and normal wave-chat.
- Review the retained video, person/door observations, policy events, audio, and transcripts.
- Promote `door-v2-20260809`, restore `door-v1-20260805`, or restore `legacy`; do not retune
  thresholds from an unlabelled run.

**Done when:** the two false sequences above produce no farewell, genuine walk-away
still produces one farewell, and a live walk-in-and-wave produces one coherent opener
without stacked greet/goodbye policy speech.

### 7b. Antenna UX polish  `[ ]`
**Goal:** After #1 validates the cue logic, tune movement style/timing.
**Steps:** tune wave-chat thinking cue, greet/goodbye pulse, startup ready cue; keep
movement **non-overlapping with robot speech** (overlap reproduced choppiness in earlier
live tests — see `docs/live-test-log.md` 06-14/06-15).
**Done when:** movement reads as natural per live feedback, with no speech-overlap choppiness.
**Constraints:** live + user present — confirm first. Diagnose timing from #6's run summary,
not from guesses.

### 8. Backend context & model experiments  `[ ]`
**Status:** paused on 2026-08-06 for production preparation. The deployed Hermes/profile-owned
context path, GPT-5.6 Luna direct fallback, session mapping, read-only reference tools, latency
tracing, and deterministic policy speech are the frozen baseline. Do not start new backend feature
or model experiments unless this item is explicitly resumed.

**Goal:** Give the receptionist real clinic context, then decide model/wrapper.
**Reference:** `docs/archive/research/custom-realtime-backend-research.md` preserves the completed
agentic-backend/context-memory research baseline for this paused item.
**Steps:**
- Add clinic-receptionist system context to the local S2S backend prompt/config.
- Comparison tracks (current stack: local STT/TTS + remote LLM via OpenRouter Responses API):
  - raw Responses API with different LLM models (model swaps)
  - Hermes / agentic wrapper with conversation memory/context management
- Test clinic context + model swaps **before** deciding whether Hermes is worth the added
  latency.
**2026-07-01 context/config pass:** the live app now sends the default clinic profile as realtime
`session.update` instructions, artifacts record the instruction source/hash/chars, and
`scripts/m1max/run_s2s_backend.sh` can point the Responses slot at either OpenRouter or an
OpenAI-compatible wrapper via `S2S_RESPONSES_BASE_URL`. No live robot test is required for this pass.
**2026-07-31 ownership correction:** when conversation mode is enabled, OPS now selects
`--profile-owned-context`; the application prompt is empty and auditable, S2S supplies generic voice
rules, and Hermes is the sole owner of persona, clinic facts, capabilities, and tool policy. The full
tracked profile prompt remains available only for direct-only fallback testing.
**Done when:** clinic context is live; a documented comparison (quality vs latency) supports
a model/wrapper decision.

### 9. S2S backend runtime reproducibility  `[x]`
**Goal:** Keep `/Users/leon/projects/speech_to_speech_backend` as the generic external backend
runtime folder, but make it reproducible from the product/controller repo instead of relying on
manual venv state.
**Decision:** The backend folder is not a product repo. It owns only `.venv`, logs, and runtime
state. The product/controller repo owns lifecycle, launch flags, and setup docs/scripts.
**Steps:**
- Add a setup/update script in this repo: `scripts/m1max/setup_s2s_backend.sh`.
- Pin and install Hugging Face `speech-to-speech==0.2.10` into
  `/Users/leon/projects/speech_to_speech_backend/.venv`.
- Ensure the script is idempotent and refuses to delete logs/runtime artifacts unless explicitly
  confirmed.
- Keep runtime launch through `scripts/m1max/run_s2s_backend.sh` and OPS `backend start/status`.
**Done when:** a fresh m1max can recreate or update the backend runtime folder from this repo's
documented commands, and live OPS still uses the same `ws://127.0.0.1:8765/v1/realtime` contract.
**Accepted:** setup/update script is implemented with dry-run, running-backend guard, package/CLI/import
verification, and `runtime-info.json` output. It does not delete logs/model caches/runtime artifacts.

### 10. Recorder sidecar process  `[ ]`
**Production relevance:** tracked as a promotion gate in
[`production-readiness.md`](production-readiness.md). The 2026-08-06 long run also exposed a large
input-loop versus MKV-duration discrepancy that must be diagnosed before continuous video is trusted.

**Goal:** Decouple artifact persistence from the live runner so audio/video/capture artifacts can
finalize even if the robot runner crashes or must be killed.
**Why now:** The current recorder writes frames incrementally, but the live runner still owns the
WAV/video file handles and manifest close. If OPS kills the runner before its finalizer completes,
artifacts can be readable but left `open` with an unfinalized video container.
**Steps:**
- Define a local recorder IPC contract for runtime events, audio frames, video frames, and close /
  heartbeat messages.
- Move long-lived WAV/video writers and manifest finalization into a recorder process owned by OPS.
- Add bounded queues/backpressure rules so recording cannot stall audio capture/playback.
- Make OPS stop order explicit: request runner stop, request recorder flush/close, then hard-kill
  only after both grace windows expire.
- Preserve the existing artifact layout so current replay/review tools still work.
**Done when:** killing or crashing the live runner during a raw-video run still leaves the manifest
closed and the video/audio files finalized, or marks exactly which stream was interrupted with a
machine-checkable reason.

---

## Status log
- 2026-06-22 — doc created; reordered from the prior 8-item list. Phases 0–3 pending.
- 2026-06-23 — #1 validated on live run `official-live-20260623-142850`; user feedback: pass, no
  issue. Full preflight also passed on `official-policy-preflight-20260623-142721`.
- 2026-06-23 — #2 secured locally in commit `bbcd9de` (`Accept official runtime live path`).
- 2026-06-23 — #3 documentation sweep: `README.md`, `docs/robot-guide.md`, `docs/runbook.md`, and
  `docs/archive/legacy/plan-reception.md` now point to official-runtime as current and label legacy
  daemon material as fallback/historical. Push was not attempted in this cleanup pass.
- 2026-06-23 — #4 non-destructive pass complete: legacy modules have module-level status notes, package
  metadata labels legacy entry points, and `docs/legacy-cleanup-plan.md` lists the exact future
  delete/quarantine candidates. No files were deleted or moved.
- 2026-06-25 — runbook updated for the accepted Python OPS CLI + native `s2s-local` live path, and
  S2S backend runtime ownership documented as a managed external folder.
- 2026-06-26 — Rerun renderer first pass accepted for timeline/spans/markers/suppression/audio-RMS
  and video-frame review. Remaining #6 work moves to the separate audio/listening UI.
- 2026-07-01 — #6 accepted as first-pass complete: audio-review app now has aligned playback,
  semantic lanes, transcript availability, per-response playback, STT-recovered sidecars, and m1max
  recovery wrapper. Deferred audio-review polish tracked under #6 follow-up; next active item is #7.
- 2026-07-01 — #7 requires live/user-present validation, so offline work advanced to #9. #9 complete:
  `scripts/m1max/setup_s2s_backend.sh` now recreates/updates the managed backend runtime venv and
  preserves the existing `run_s2s_backend.sh` / OPS backend contract.
- 2026-07-01 — #8 context/config pass complete without live testing: default clinic instructions are
  auditable in artifacts, and the S2S launcher now has explicit direct-model and wrapper endpoint
  switches. The text/preflight comparison is still the acceptance gate for choosing Hermes or staying
  direct OpenRouter.
- 2026-07-25 — live run `official-live-20260725-111932` accepted for wave-chat behavior and
  diagnosed two unwanted greet/goodbye sequences as vision-policy false positives caused by raw
  person-box peak/drop handling. Added #7a as the next improvement item; reproduce from the recorded
  capture before changing the tracker or spending another live run.
- 2026-08-06 — #7a implementation and captured offline acceptance complete with the versioned
  `door-v1-20260805` policy. Long run `official-live-20260806-114813` established idle door-detection
  stability but observed no people, so controlled visitor live acceptance remains open.
- 2026-08-06 — backend feature work paused at the deployed Hermes/GPT-5.6-Luna baseline. Production
  readiness, operations hardening, recording integrity, privacy/retention, and remote control are now
  the priority; see `production-readiness.md`.
