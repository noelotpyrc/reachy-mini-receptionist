# Temporary Audio-Review Fix Tracker

Date: 2026-07-01

Context: review of the reception audio-review app (`src/reachy_mini_brain/official_runtime/audio_review.py`)
after the backend speculative-turn investigation on run `official-live-20260625-133754`. This started as a
fix-proposal note; it now records the disposition of those fixes after implementation.

Why a tmp doc (doc-placement reasoning):
- `docs/conversation-audio-player.md` is the **spec** for this app and is the eventual home once a fix is
  accepted — but delivering a reviewed issue list to another agent is a different job (see the precedent
  `rerun-review-issues-20260626.md`, which delivered fix proposals for the Rerun renderer the same
  way).
- There is no audio-review review doc yet, so per that precedent this is a fresh `tmp-*-review-issues.md`.
- When a proposal is accepted, fold it into `docs/conversation-audio-player.md` (spec) and, where it touches
  span/marker anchoring, `docs/general-timeline-model.md`.

## Purpose of the app (keep this in mind for every fix)

The review app is for a **human** to eyeball + **listen to** live-test results and judge audio quality and
content — an AI agent cannot judge either. So every fix below is about making it easy for a person to (a)
see where something happened, (b) click to hear it, and (c) trust or distrust what the app claims. Fixes must
not present machine-derived guesses as ground truth.

## Current disposition

- **#1 implemented differently** — no inferred text is inserted into the Assistant text transcript lane.
  Gaps are shown through the `Transcript availability` lane.
- **#2 implemented** — optional recovered-text sidecar, repeatable m1max wrapper, and separate
  `STT recovered transcript` lane.
- **#3 deferred by product choice** — conversation-script panel with per-turn playback remains the main
  future UX improvement.
- **#4 implemented** — `response.created` is demoted to a sparse signal; turn windows/lifecycle use
  first audio, audio done, and `response.done` anchors.
- **#5 deferred** — run-on cluster annotation is optional diagnostic polish.

## Root-cause reference (why the holes exist — it is not an app bug)

The missing assistant text is **backend behavior**, verified in the installed HF `speech-to-speech` package on
m1max, not our code:
- The backend uses *speculative turns*. Run-on / re-segmented user speech bumps a turn's revision; the
  superseded generation's assistant text is dropped at the transcript-emit gate
  (`api/openai_realtime/handlers/response.py:on_assistant_text` → "Dropping stale assistant text" → `return []`).
- The **audio path is not gated** (`handlers/audio.py:encode_audio_chunk`), so the robot still speaks. Result:
  audio present, text absent, response `status=completed`.
- Evidence (run `official-live-20260625-133754`): 16 responses (`response.done`), but only **13**
  `response.output_audio_transcript.done` and **13** `handler.output` role=assistant. The 3 gap responses
  (`…8a580fb4`, `…68b56d52`, `…fb69ed37`) each preceded by 2–3 rapid `input_audio_transcription.completed`
  segments, each `status=completed` with `output_tokens` 14/19/21, each with a saved per-response WAV. Assistant
  text for them exists **nowhere** in the artifacts.

Implication: the app should stop treating "no assistant text" as "robot said nothing." It should treat a
robot-audio span with no text as a **known backend gap** and help the human recover/inspect it.

---

## 1. Assistant-text lane has silent holes — flag them (cheap, no STT)

Status: implemented differently. Renderer-only. No new dependency.

Issue: the `llm_response` "Assistant text transcript" lane (`audio_review.py:600`) is fed only by
`response.output_audio_transcript.done` (`:711-713`) and `handler.output` role=assistant (`:704-705`) — both
derive from the dropped backend path. For the 3 gap turns the lane is simply **blank**, even though the
`robot_audio` playback lane (`assistant.audio.started/done`, `:661-675`) and the output audio clearly show the
robot spoke.

Reasoning: a human scrubbing the timeline sees a robot-audio span with an empty text lane and has no way to
know whether it is a bug, a lost log, or the robot genuinely saying nothing. That ambiguity defeats the app's
purpose. `robot_audio` is reliable (16/16), so the app can detect and label the gap deterministically.

Resolution:
- Keep `Assistant text transcript` strictly backend-emitted. It stays blank when the backend did not log
  assistant text.
- Add a separate `Transcript availability` lane with one response span per robot utterance:
  `backend transcript logged` or `no backend transcript event`.
- If an STT recovery sidecar exists for a missing response, the availability detail says
  `stt_recovered_text=available` and the recovered text appears only in the recovered lane.

## 2. Recover the dropped text via STT of the per-response WAV

Status: implemented. Uses an offline STT pre-step + sidecar. This became the separate recovered transcript
lane, not a backfill of the backend transcript lane.

Issue: the assistant text was generated and spoken (`output_tokens` 14/19/21) but never emitted as an event,
so it is unrecoverable from the event artifacts. The per-response TTS WAVs, however, exist and are already in
the app payload (`response_audio`, built in `build_audio_review_app` from `review.audio_hints` for
`response-*` streams).

Reasoning: the robot's spoken audio *is* the missing text. Re-transcribing the per-response WAV recovers what
the robot said, which is what the human wants to read on the timeline. Doing it as a separate pre-step keeps
the review server dependency-light and matches the app's "replay = scrub, don't re-run" philosophy.

Resolution:
- `reception-audio-review --recover-missing-text` finds response WAVs with robot audio but no backend
  assistant transcript and writes `audio-review/<run_id>/recovered-text-<run_id>.jsonl`.
- `scripts/m1max/recover_audio_review_text.sh <run_id>` is the canonical m1max wrapper because it runs under
  the S2S backend Python environment where `speech_to_speech` Parakeet STT is installed.
- The review server auto-loads the default sidecar and renders rows in `STT recovered transcript`, directly
  under `Assistant text transcript`.
- Provenance is mandatory: recovered text is Cat-2/fallible and must never look like backend-emitted
  transcript.

## 3. Conversation-script panel with per-turn playback

Status: deferred. Frontend + payload. Highest remaining human-facing UX improvement once the timeline lanes
are stable. This is the "turn like a conversation script" ask.

Issue: the current turn list is user-centric — it shows `#index user-transcript` + input/output counts, no
assistant reply, and there is no per-turn play control. Per-response WAVs are now playable through the main
response dropdown, but they are not attached to user/assistant dialogue rows.

Reasoning: the fastest human loop for judging content quality is *read the dialogue, click to hear each side,
spot the mismatch* (e.g. robot said something different from the text, text missing, wrong answer). A flat
user-only turn list does not support that.

Proposed fix:
- Add an alternating **user ↔ assistant** dialogue panel, one row pair per turn.
- Assistant text = backend transcript where present, else recovered text (#2), else an explicit
  no-backend-transcript state — each carrying its provenance/trust tag.
- A **play button per row**: user rows seek the main timeline to the turn window (`review_start_ts` /
  `review_end_ts` already computed in `_turn_payloads`); assistant rows play that turn's per-response WAV
  directly.

## 4. "S2S response lifecycle" lane is misleadingly sparse

Status: implemented. Renderer-only.

Issue: the `llm` lane (`:599`) draws `response.created` (`:706-707`) and `response.done` (`:708-710`).
`response.created` is emitted only on the explicit-create path (policy speeches + speculative reopen) — **7 of
16** on this run — while `response.done` is 16/16. `_turn_payloads` also uses `response_created_ts` as a
fallback anchor (`:1020`).

Reasoning: a reviewer sees "response object created" on fewer than half the turns and can misread the other 9
as responses that never started. The reliable per-response signals are `response.done` /
first `response.output_audio.delta` / `assistant.audio.started` (all 16/16).

Resolution:
- `S2S response lifecycle` shows `response.created signal`, `first audio`, `response done`, and `audio done`.
- `response.created` is explicitly sparse and not a response-start anchor.
- Turn windows prefer `audio_done_ts`, `response_done_ts`, and `first_audio_ts` over `response_created_ts`.
- `docs/general-timeline-model.md` now states that `response.created` must not be used as the generic
  backend-processing start boundary.

## 5. (Optional) Annotate run-on clusters

Status: deferred. Nice-to-have diagnostic sugar.

Issue: run-on speech shows up as multiple `vad` "speech NN" spans + multiple `stt_final` finals close
together, but nothing connects that cluster to the turn whose text got dropped.

Reasoning: it explains the *why* at a glance — "these 2–3 segments collapsed into one turn and the backend
dropped the text." Helps a human correlate cause and symptom without reading raw events.

Proposed fix: when ≥2 VAD/STT-final segments precede a single response with missing text, bracket the cluster
with a `run-on → text dropped` badge.

---

## Shared note: provenance / trust labeling (applies to #1, #2, #3)

Per the `docs/data-harness.md` taxonomy, keep text provenance visually distinct:
- **Backend-emitted transcript** (`output_audio_transcript.done` / `handler.output`) — what the backend logged.
- **STT-recovered** — what we re-derived from the robot's own output audio (Cat-2, fallible).

For gaps, prefer a diagnostic state (`no backend transcript event`) over text that could be mistaken for what
the robot said.

The whole point of the app is human judgement of content/quality; conflating a machine-derived guess with a
logged fact would undermine that.

## Remaining build order

1. **#3** — conversation-script panel + per-turn playback. Deferred for now by product choice.
2. **#5** — optional run-on cluster annotation.

## Cross-references

- Spec / eventual home: `docs/conversation-audio-player.md`
- Timeline model (span/marker anchoring, `response.created` demotion): `docs/general-timeline-model.md`
- Data taxonomy (Cat-1/2/3, provenance): `docs/data-harness.md`
- Root-cause backend finding: HF `speech-to-speech` speculative-turn staleness drop (this session's diagnosis).
