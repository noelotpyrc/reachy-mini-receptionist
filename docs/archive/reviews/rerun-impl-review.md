# Review — Rerun review implementation (#6 v1)

**Status:** superseded implementation review. Its v1 findings were addressed and exercised with the
real Rerun SDK; use [`rerun-integration.md`](../../rerun-integration.md) and
[`rerun-vision-tracking-plan.md`](../../rerun-vision-tracking-plan.md) for current behavior.

**Reviewed:** `src/reachy_mini_brain/official_runtime/rerun_review.py`,
`tests/test_rerun_review.py`, the `diagnosis = ["rerun-sdk"]` extra + `reception-rerun-review`
entry point (`pyproject.toml`).
**Against:** `docs/rerun-integration.md` (settled v1: read-only renderer over existing
artifacts, multi-lane read, offline-derived latency, suppression, audio RMS + WAV-path hints,
optional `rerun-sdk`).
**Method:** full read + offline tests (`3 passed`) + probed for a real `rerun-sdk`. No robot.

## Verdict

**Faithful, well-built v1 — but the Rerun rendering itself is unvalidated.** The parser/derivation
layer matches the settled design point-for-point (read-only, multi-lane, derive-don't-persist
latency, suppression, audio RMS + listen hints, `rerun-sdk` optional) and is well tested. The
**text/JSON path is trustworthy and usable today**. The **`.rrd`/viewer path has only ever run
against a fake `rerun` module** — no real `rerun-sdk` exists in any environment here — so the actual
viewer output is unproven, and there's a concrete version-API bug lurking in the scalar lane. Treat
#6 as "offline review works; Rerun rendering needs one validation spike before trust."

## Fix status — 2026-06-23

- R1 fixed: scalar logging now prefers real `rr.Scalars` and falls back to older `rr.Scalar`.
- G1 fixed: `diagnosis` pins the validated SDK version, `rerun-sdk==0.33.1`.
- G2 validated on synced run `official-live-20260623-142850`: parser produced 18 transcript turns,
  15 paired response turns, 46 suppression rows, and 20 audio hints; real SDK wrote a 1.2 MB `.rrd`.
- Additional fix from real-data validation: turn pairing now uses the transcript-to-next-transcript
  window so a late response is not incorrectly attached to earlier completed transcripts.

## 🟢 What's done well (matches the v1 design)

- **Read-only over existing artifacts; no runtime change, no robot.** `load_run_review`
  (`rerun_review.py:159`) reads manifest + lanes + markers + audio sidecars. Works on historical
  runs — exactly the v1 scope (no Stage-0 instrumentation gate).
- **Multi-lane read, classify by `type` prefix** — reads `events`/`realtime`/`policies` + markers,
  sorts by `ts` (`:167-171`); `_entity_for_row` (`:653`) routes by prefix. This is the Q3 decision
  (read both lanes, don't reroute `hf.*`). `_normalized_type` (`:579`) strips `hf.realtime.`/
  `realtime.` so transcript/response detection works regardless of which lane they landed in —
  handles the GA mis-laning **at read time**, no runtime fix needed.
- **Latency derived offline** — `_derive_turns` (`:492`) builds transcript → thinking →
  `response.created` → first-audio (`assistant.audio.started`) → `audio.done`, with per-stage
  deltas. Exactly the Q4 decision (derive, don't persist) and the v1 per-turn chain.
- **Suppression / missed-cue surfaced** (`_is_suppression`, `:606`) — the Q6 must-add, present.
- **Audio RMS scalars + listen hints** — `audio_hints` carry WAV path + `sample_start:sample_end`
  + duration (`AudioHint` `:76`, `_summarize_audio_hint` `:682`). This is precisely the Q6
  "RMS visual + WAV-path-and-offset for human listening" workaround for Rerun's no-playback gap.
- **`rerun-sdk` is genuinely optional** — lazy import *inside* `render_review_to_rerun` (`:247`)
  raising `RerunUnavailableError`; the text/JSON path never imports it; `diagnosis` extra in
  `pyproject`. Matches Q7 — ops/parsing never depend on Rerun.
- **Markers read one level up** (`run_root.parent / markers-<id>.jsonl`, `:343`) — correct schema
  detail.
- **m1max-absolute manifest paths resolve when read locally** — `_resolve_path` (`:544`) tries
  multiple candidates; the test simulates `/Users/leon/...` paths in the manifest. Important for
  reviewing synced runs.
- **Text + JSON output** (`format_text_review` + `--json-output`) — the "compact text + JSON"
  the TODO asked for.
- **Tests** cover parser/derivation (exact latencies, suppression, audio hints, all four lanes),
  CLI JSON, and the render path via a monkeypatched fake `rerun`. `3 passed`.

## 🔴 Should fix before trusting the Rerun output

**R1 — Rendering is unvalidated against a real `rerun-sdk`, and the scalar lane likely breaks on
current versions.** `rerun-sdk` isn't installed anywhere here, so the render path has only run
against a `SimpleNamespace` fake. Concretely: `_rr_log_scalar` (`:731`) tries only `rr.Scalar`
(singular); **modern `rerun-sdk` (0.18+) uses `rr.Scalars` (plural)** and may not expose `Scalar`,
in which case the code falls back to `rr.log(entity, raw_float)` — which likely fails or silently
drops every scalar (i.e. **the latency and audio-RMS lanes — two of the four v1 deliverables**).
The fake test uses `Scalar`, so it cannot catch this. Action: install the `diagnosis` extra, render
one real run, and fix the scalar archetype to match the installed API (mirror `_rr_set_time`'s
try/fallback pattern, which *does* handle `set_time` vs `set_time_seconds`). This is the audio/scalar
+ headless spike the design doc flagged as "settle before the bigger increments" — still open.

## 🟡 Gaps

**G1 — `rerun-sdk` is unpinned** (`diagnosis = ["rerun-sdk"]`, no version). Given the
`Scalar`→`Scalars` and `set_time` API churn, an unpinned dep will break unpredictably across
installs. Pin a tested version (or a floor) once R1's spike picks one.

**G2 — Transcript/response detection is unvalidated on a real run.** `TRANSCRIPT_KINDS` (`:21`)
anticipates `conversation.item.input_audio_transcription.completed` (after prefix-normalization) and
friends, but no real official-runtime run with `hf.*` conversation events was available to confirm
the actual emitted strings or where `response_id` sits. If the real names differ, turns/latency come
up **silently empty** (no error). Action: run it on one real synced run (e.g.
`official-live-20260623-142850` from m1max) and confirm `turns`/`latency` populate. This is the
"validate on real data" step the whole #6 analysis rested on.

**G3 — Latency uses recorder write-time `ts`, not backend `event_ts`/`created_ts`.** Several rows
carry `event_ts` (true emit time); `_delta` (`:647`) subtracts `ts` (write time). Usually
negligible, and the doc *settled on wall-clock `ts` for v1*, so this is aligned — but note it as a
precision caveat and an easy future refinement for the lag-localization use case.

## Minor

- **Turn boundary heuristic can mis-assign on overlap/barge-in** — `_derive_turns` binds the *first*
  matching row after the transcript ts; only `first_audio` is response-id matched, `thinking`/
  `audio_done` are not (`:497-509`). Fine for sequential turns; a barge-in-heavy run could bind a
  later turn's `audio_done` to the wrong transcript.
- **No video / detections / waveform lanes** — correctly **deferred** per v1 scope (not a gap).
- `format_text_review` is text-only; the TODO said "text/markdown" — text is fine for v1.

## Recommendation

Accept as the #6 v1 foundation — the offline review (text/JSON, latency, suppression, audio hints) is
faithful and usable **today** with no `rerun-sdk`. Before trusting the **Rerun viewer** output, do the
one spike the design already flagged: install + **pin** `rerun-sdk` (G1), render one **real** synced
run (G2), and **fix the `Scalar`/`Scalars` archetype** so latency + RMS actually plot (R1). Until
then, lean on the text/JSON handoff and treat the `.rrd`/`--spawn` path as unproven.
