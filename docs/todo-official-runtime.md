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
**Goal:** Quarantine the legacy stack without breaking the product path.
**Dependency map (verified — do not break these):**
- `official_runtime/` is self-contained **except** two keepers it imports from legacy:
  - `audio_pacing.py` — constants, used by 4 official modules. **KEEP.**
  - `audio.py` — `robot_io.py:53` lazily uses `_patch_bin_add_check`. **KEEP.**
- **Legacy-only, safe to retire** (14 modules): `reception.py`, `session.py`,
  `perception.py` (legacy — official has its own `official_runtime/perception.py`),
  `detector.py`, `approach.py`, `gesture.py`, `alert_engine.py`, `stt.py`, `stt_worker.py`(*),
  `tts.py`, `brain.py`, `replay.py` (already orphaned — no entry point),
  `review_audio.py`, `transcribe.py`.
  - (*) `stt_worker.py` is currently untracked and only used by legacy `session.py`; it
    retires with the daemon. Commit-then-remove, or just don't commit it under Phase 0 —
    decide explicitly.
  - **Tangle:** `audio.py` (kept) lazily imports `stt`/`tts` inside its `listen`/`speak`
    CLI functions. Deleting `stt.py`/`tts.py` breaks those legacy audio CLIs and
    `tests/test_e2e_audio.py` (shells out to `tts.synthesize`). Decide whether those
    manual audio CLIs stay before removing `stt`/`tts`.
- **Tests that die with legacy:** `tests/test_reception_manifest.py` (imports `reception`).
- **Entry points to remove when legacy goes** (`pyproject.toml`): `reception`,
  `review-audio`.
**Steps:**
- First pass: add deprecation headers / a `legacy` note; stop documenting them as current.
- Actual quarantine (move to `legacy/` or tag-then-delete) and any file deletion: **only
  after explicit user confirmation** — surface the exact file list and disposition choice
  (tag+delete vs `legacy/` subdir vs delete) at that point.
**Done when:** legacy is clearly marked deprecated and the deletion/quarantine plan is
written and waiting on a go/no-go; product path still imports and tests pass.

---

## Phase 2 — Build the loop-accelerating infrastructure (before experiments)

### 5. OPS management tools  `[ ]`
**Goal:** Remove the ops confusion seen during live tests with a small, clear command set.
**Steps:** clean commands (extend `scripts/m1max/live_ops.sh` or a sibling) for:
- backend status / start / stop
- daemon (runtime) start / stop / sleep / wake
- current robot + runtime status readout
- artifact sync (pull latest run from m1max) + **latest-run summary** — this last one
  should call the run-summary tool from #6, not reimplement it.
**Done when:** one documented command each; a fresh operator can bring up, check status,
and tear down without reading code.

### 6. Run-summary / diagnosis visibility (the keystone)  `[ ]`
**Goal:** Turn "I have to re-experience the robot" into "I read the run summary." Build a
compact per-run review tool on the **official-runtime artifact schema** (do **not** port
the legacy `review_audio.py` — it's welded to the dead daemon format; build fresh on the
cleaner `run_id`+`ts`+`response_id` rows under `artifacts/official-runtime-live/`).
**Steps:**
- Render, per response, the lifecycle:
  `user transcript -> thinking cue started -> response.created -> first audio -> audio done`,
  with per-stage latencies.
- **Markers alignment:** join `artifacts/markers-<run_id>.jsonl` (from `mark.py`, currently
  write-only with no consumer) onto the same timeline by wall-clock `ts`, so each piece of
  human feedback sits next to the events around it.
- Surface **missing-cue / suppression reasons** (e.g. `start_suppressed`) inline.
- Detect the run schema (official vs legacy) or just target official; one tool, one output
  (compact text/markdown summary + machine-readable JSON).
**Done when:** running it on a recorded run prints a readable timeline that a human can use
to diagnose UX without replaying the session; markers show up aligned.
**Note:** This is the consumer the marker tool was built for, and the long-deferred
"merged timeline" from `docs/data-harness.md` gap #8.

---

## Phase 3 — Iterate UX & backend with the fast loop

### 7. Antenna UX polish  `[ ]`
**Goal:** After #1 validates the cue logic, tune movement style/timing.
**Steps:** tune wave-chat thinking cue, greet/goodbye pulse, startup ready cue; keep
movement **non-overlapping with robot speech** (overlap reproduced choppiness in earlier
live tests — see `docs/live-test-log.md` 06-14/06-15).
**Done when:** movement reads as natural per live feedback, with no speech-overlap choppiness.
**Constraints:** live + user present — confirm first. Diagnose timing from #6's run summary,
not from guesses.

### 8. Backend context & model experiments  `[ ]`
**Goal:** Give the receptionist real clinic context, then decide model/wrapper.
**Reference:** `docs/custom-realtime-backend-research.md` is the active agentic-backend/context-memory
research plan for this item.
**Steps:**
- Add clinic-receptionist system context to the local S2S backend prompt/config.
- Comparison tracks (current stack: local STT/TTS + remote LLM via OpenRouter Responses API):
  - raw Responses API with different LLM models (model swaps)
  - Hermes / agentic wrapper with conversation memory/context management
- Test clinic context + model swaps **before** deciding whether Hermes is worth the added
  latency.
**Done when:** clinic context is live; a documented comparison (quality vs latency) supports
a model/wrapper decision.

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
