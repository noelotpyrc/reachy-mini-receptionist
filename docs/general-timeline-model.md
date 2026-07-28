# General timeline — modeling spec

**Status:** settled L1 model (2026-06-25), audio-review anchoring clarifications added
(2026-07-01). **Renderer-agnostic** — this is the single source of truth for *what* the
conversation/general timeline contains; renderers (Rerun, an in-house trace/Gantt view, a text
summary) consume it. It also serves as the **event allowlist** used to filter the recorded firehose
down to what the timeline shows.

Grounded against the official-runtime artifacts; representative runs are listed in Grounding status.

## Modeling principle

**Each timeline element is bounded by the events of the actor that owns it.** Never substitute
one actor's event for another's for convenience. Two element kinds:
- **Span** — has duration; defined by a *start* event and an *end* event from the same actor.
- **Marker** — an instant; a single event.

Backend internals (VAD/STT/LLM/TTS) are **collapsed** into one backend span and do **not** appear
on the general timeline. Their detailed breakdown lives in the audio-review viewer spec
(`docs/conversation-audio-player.md`).

## Actors (6)

| # | Actor | Runs on | Role |
|---|---|---|---|
| 1 | **Policy engine** | m1max | director — decides what happens |
| 2 | **S2S backend** | m1max | audio processing (robot's raw mic → speech/response) |
| 3 | **Perception** | m1max | video detection (robot's raw video → detections) |
| 4 | **Robot (output)** | robot | physical output — speaker + antennas |
| 5 | **Human/operator** | — | feedback (markers) |
| 6 | **Session/runtime** | m1max | setup / lifecycle (preamble, optional) |

The robot's raw **mic** feeds actor 2 and raw **camera** feeds actor 3; those streams are data
inputs, not event-emitting actors (the mic-hot period is the policy envelope, below).

## Spans (have duration)

| Owner | Span | Start event | End event |
|---|---|---|---|
| Policy engine | conversation envelope *(= mic-hot/gate-open period)* | `policy.conversation_opened` | `policy.conversation_closed` |
| S2S backend | backend-processing *(one per user turn)* | `hf.realtime.input_audio_buffer.speech_started` | `hf.realtime.response.output_audio.done` |
| Robot · speaker | robot-speaking | `assistant.audio.started` | `assistant.audio.done` |
| Robot · antennas | thinking-cue | `runtime.antenna_cue` (cue=`thinking`, event_phase=`started`) | `runtime.antenna_cue` (cue=`thinking`, event_phase=`stopped`) |
| Robot · antennas | reaction-pulse *(greet/goodbye/wave)* | `runtime.antenna_cue` (cue=`policy_pulse`, phase=`high`) | `runtime.antenna_cue` (cue=`policy_pulse`, phase=`rest`) |
| Robot · antennas | startup-ready-cue | `runtime.ready_cue` (cue=`ready`, phase=`high`) | `runtime.ready_cue` (cue=`ready`, phase=`rest`) |

## Markers (instants)

| Owner | Markers |
|---|---|
| Policy engine | `policy.greet`, `policy.farewell`, `policy.wave_received`, close-reason on `policy.conversation_closed`; cue decisions `policy.conversation_cue.thinking_started/stopped`; `policy.antenna_pulse` (pulse decision); `policy.speech_requested`; suppressions `policy.conversation_cue.start_suppressed`, `policy.greet_suppressed`/`farewell_suppressed`, `cooldown_skip` |
| S2S backend | *(none — internals collapsed)* |
| Perception | detection triggers — **extensible** (more detection types planned); currently e.g. `vision.wave` (opens conversation), `vision.approach`, `vision.depart`. Frame-level detail (boxes/scores) is the physical/Rerun layer, not here |
| Human/operator | `markers-<run_id>.jsonl` (from `scripts/m1max/mark.py`) |
| Session/runtime *(preamble, optional)* | `robot_control_ready`, `robot_sdk_connected`, audio/video warmup, `software_pipeline_initialized`, `first_mic_frame_captured`/`…forwarded`, `audio_gate_opened`/`closed` |

Do **not** use `hf.realtime.response.created` as the general backend-processing start anchor. It is
a sparse explicit-create signal in current HF S2S artifacts, not a reliable "response started" event
for every user turn. For policy-text-only responses that have no user `speech_started`, keep the
policy request as a policy marker and the physical playback as `robot/speaker`; do not invent a
backend-processing span unless a same-actor backend start/end pair is recorded. The audio-review
viewer may still show `response.created` as a labeled sparse signal beside reliable per-response
anchors such as first audio, `response.done`, and `assistant.audio.done`.

## Entity layout (folders × sub-entities)

The timeline is organized as **`<actor>/<sub-entity>`**. Top folder = actor (fixed). Sub-entity
granularity follows one rule:

> **A sub-entity = a distinct emitting unit / behavior you'd watch on its own.** Spans always split
> (each renders as its own lane); markers group by their code emitter — one log per emitter — with a
> finer split only where a concern is genuinely scanned in isolation.

**Encoding:** spans → a **0/1 state-scalar** at the sub-entity path (step plot reads as a bar),
plus a matching `START`/`END` `TextLog` detail at the same timestamp; markers → **`TextLog`** at
the path.

| Actor folder | Sub-entities |
|---|---|
| `policy/` | `greet` · `farewell` · `wave_conversation` · `conversation_cue` (· `engine`, optional) — grouped by behavior, below |
| `backend/` | `processing` (span) |
| `robot/` | `speaker` (span) · `antennas/thinking` (span) · `antennas/pulse` (span) · `antennas/ready_cue` (span) |
| `perception/` | **one per detection type** — `wave` · `approach` · `depart` · `<future>` (**option 2**: the actor expected to grow most, each detection its own lane) |
| `human/` | `feedback` (log) |
| `session/` | `milestones` (log) |

### Policy sub-entities — grouped by behavior (not by the current class layout)

The code emits all reception reactions from **one** `ReceptionPolicy` today (only
`ConversationCuePolicy` is separate). The model groups them by the **behavior they implement** —
the policies they *should* be — via a renderer-side type→behavior map:

| Sub-entity | Trigger → behavior | Events (direct + routed) |
|---|---|---|
| `policy/greet` | approach → greeting | `greet`, `greet_suppressed`, `cooldown_skip`(action=greet), `speech_requested`/`antenna_pulse`(reason=approach) |
| `policy/farewell` | depart → goodbye | `farewell`, `farewell_suppressed`, `cooldown_skip`(action=farewell), `speech_requested`/`antenna_pulse`(reason=depart) |
| `policy/wave_conversation` | wave → session → close | the **conversation envelope span** + `wave_received`, `conversation_opened`/`closed`, `conversation_already_active`, `cooldown_skip`(action=conversation_open), `speech_requested`(reason=wave) |
| `policy/conversation_cue` | *(already its own policy)* | `thinking_started`/`stopped`, `start_suppressed` |

**Routing rule:** direct events go to their behavior by type; shared mechanism events
(`speech_requested`, `antenna_pulse`, `cooldown_skip`) route by their `reason`/`action` field. The
conversation lane is named `wave_conversation` (trigger-prefixed) so a future approach- or
button-triggered conversation is a separate lane. The conversation **envelope span** lives under
`policy/wave_conversation` (it's that behavior's span).

## Structure within one turn (overlap is real and intended)

```
policy.conversation_opened ───────────────────────────────────── policy.conversation_closed   (envelope)
  per turn:
   speech_started ───────────────────────── response.output_audio.done            backend-processing
        (speech_stopped · transcript)
          antenna_cue.started ── antenna_cue.stopped                               thinking-cue (antennas)
                            assistant.audio.started ──────── assistant.audio.done  robot-speaking (speaker)
```

- The cue sits inside the backend span; robot-speaking starts inside it (first audio precedes
  backend-send-done) and ends after it (playback tail). Cue→speaking handoff is at
  `assistant.audio.started`.
- Policy decisions/suppressions sit next to the spans they cause: "cue decided → antenna span" vs
  "cue *suppressed* → no antenna span." Everything rides on the policy envelope.

## Using the model as a filter (the curation step)

The recorded artifacts are a firehose (per-frame audio, every `hf.*` internal, all milestones).
**This model is the allowlist:** a renderer keeps only the events named above and drops the rest.
Pipeline:
1. Read all lanes (`events`, `realtime`, `policies`) + markers + audio sidecars.
2. **Keep only** the span-boundary events and marker events listed here; discard everything else.
3. Reconstruct spans by pairing each actor's start/end (per turn); collect markers.
4. Hand the spans+markers to the renderer.

This is what turns the cluttered "wall of ticks" into the 5-span + marker view.

## Renderer notes — two renderers consume this model

- **Rerun → physical/vision layer + L1 spans** (spec: `docs/rerun-integration.md`). Rerun has no
  native span archetype — it renders ticks (`TextLog`) or plots (`Scalars`) per entity; encode each
  span as a **0/1 state scalar** (`1` while active → step plot reads as a bar) with same-timestamp
  boundary details, markers as `TextLog`.
  (Verify against the pinned `rerun-sdk==0.33.1` in case an interval archetype now exists.) Rerun's
  native strength is the multimodal physical/vision data (camera, motion, detections).
- **In-house conversation/audio player → conversation/audio layer** (spec:
  `docs/conversation-audio-player.md`). The **Rerun-like** scrubbable player for *listening*: input +
  output audio (playable — which Rerun can't do) + backend timeline + markers. It uses this model's
  anchors, then deliberately uncollapses backend internals into audio-review-specific semantic lanes.
- **Text/JSON summary + slice queries** consume the same spans/markers for the agent-facing view.

## Grounding status

Grounded against `official-live-20260623-142850` for the L1 physical timeline and
`official-live-20260625-133754` for the audio-review gap investigation.

Verified present in representative runs: `speech_started`, `response.output_audio.done`,
`assistant.audio.started`/`done`, `runtime.antenna_cue` cue=thinking started/stopped,
cue=policy_pulse high/rest pairs, `runtime.ready_cue` high/rest pair,
`policy.conversation_cue.start_suppressed`, policy envelope (`conversation_opened/closed`),
perception triggers, human markers, and session milestones.

`response.created` is intentionally demoted. On `official-live-20260625-133754`, robot response
audio and `response.done` were present for 16 responses, while `response.created` appeared on only 7.
Missing `response.created` therefore means "no explicit create signal logged here," not "no response
started."

Assistant text can also be sparse for backend reasons: the same 2026-06-25 run had 16 robot-audio
responses but only 13 backend assistant transcript events. The general timeline does not infer text
from audio. The audio-review viewer exposes this as transcript availability plus optional
STT-recovered sidecar text.

## Open items

- **Policy refactor to match this model** (tracked in `todo-official-runtime.md`): greet / farewell /
  wave_conversation are one `ReceptionPolicy` today, so the behavior grouping is done via a
  renderer-side type→behavior map. Splitting `ReceptionPolicy` into per-behavior policies (each
  emitting its own `source`) would make the grouping native and delete the map. The model and the
  `.rrd` output are unchanged by this — only the renderer's behavior-derivation changes. Keep that
  derivation in **one `event → behavior` adapter** that is **vintage-compatible** (use native
  `source` when present, else fall back to the legacy type→behavior map) so old runs still render
  and the refactor touches only that adapter, not the entity-path / span-pairing / `.rrd` code.
