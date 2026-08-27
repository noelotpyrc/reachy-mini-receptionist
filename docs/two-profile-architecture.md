# Design — Two-profile Hermes architecture: owner assistant and patient receptionist

Date: 2026-07-31
Status: proposal, first pass. Nothing implemented; no live-path change is required by this document.
Audience: implementing agent and reviewer. Read [`hermes-s2s-fork-spec.md`](hermes-s2s-fork-spec.md) first.

## 1. Goal

Run two agents against one clinic:

1. An **owner-facing assistant** for Genie, handling clinic admin and management.
2. The existing **patient-facing receptionist** on the robot, handling lightweight
   arrival logging and waiting-room conversation.

The assistant may read what the receptionist produced. The receptionist may not read
anything the assistant holds, and nothing one visitor says may reach another visitor's
session unless a human approved it as a clinic fact.

## 2. Decisions made in this pass

- **Two Hermes profiles, not one.** Toolsets and memory files are profile-global, so a
  single profile cannot expose write tools to Genie while denying them to the robot.
- **The owner assistant stays on Hermes.** Profile tooling, the sync script, the
  `reference_readonly` plugin pattern, and the latency-trace observer already exist. A
  second harness would rebuild them for no gain.
- **The owner assistant runs a local model** on m1max, with an OpenRouter fallback in
  profile config. It is not latency-critical.
- **The receptionist is unchanged.** Memory toolset off, `reference_readonly` only,
  OpenRouter model, per §6 of the fork spec.
- **The shared store is structured-only.** Numbers, timestamps, and closed enums. No
  transcripts, no names, no free text.
- **The store is derived from Hermes's own artifacts**, not from robot runtime logs.
- **First-pass check-in means logging that someone arrived**, not identifying them.
- **Learning is batch and human-gated.** No autonomous write from a visitor conversation
  into anything injected into a later session.

## 3. Ground facts (verified 2026-07-31 against Hermes docs)

- Hermes supports local and self-hosted backends through OpenAI-compatible endpoints:
  `model.provider: custom`, `model.base_url`, `model.api_key`. llama.cpp, Ollama, LM
  Studio, and vLLM are named. Provider config is per profile.
- Hermes supports MCP servers as a tool source; each configured server generates an
  `mcp-<server>` toolset at runtime.
- A `cronjob` toolset schedules recurring tasks. A `safe` toolset is a read-only bundle.
  The `file` toolset includes write and patch (consistent with fork spec §6).
- From the fork spec, unchanged and load-bearing here:
  - Profile selection is endpoint/process-level; each profile has its own config, `.env`,
    `SOUL.md`, memories, sessions, skills, state database, and gateway process (§2, §6a).
  - Hermes's `USER.md`/`MEMORY.md` are single-owner files injected into every session.
    With the memory toolset on, the agent auto-wrote a scripted visitor persona, which
    would then surface in other visitors' conversations (§6).
  - Greet, goodbye, and opener speech uses `tts.create` and invokes no LLM. Greetings do
    not exist in the Hermes chain (§2).
  - Conversation chains persist in the profile's `response_store.db` (SQLite) and survive
    gateway restarts (§3).
  - `logs/latency-trace.jsonl` records opaque session/task/turn/request IDs, timestamps,
    model/provider labels, counts, finish status, numeric usage, and error type/status. It
    never records prompts, conversation text, payloads, credentials, or tool data. New
    files are mode `0600` (§6c).

## 4. Architecture

### 4a. Profiles

| | `reachyclinic` (robot) | `acugenie-owner` (new) |
| --- | --- | --- |
| API port | 8642 | 8644 |
| Backend | OpenRouter, `openai/gpt-5.6-luna` | local, `model.provider: custom` |
| Memory toolset | off | **on** — the single owner is Genie |
| Tools | `reference_readonly` only | `file`, `cronjob`, MCP servers, store reader |
| Latency budget | under 2 s TTFT | none |
| Interface | robot realtime WebSocket | Hermes CLI / API, text |

The memory asymmetry is deliberate. The feature that is a contamination hazard on the
robot is the correct feature on the assistant, because the assistant has exactly one
owner.

Port 8643 remains `reachyclinic-test` (staging).

### 4b. Direction of flow

```
 Genie (text)                            Patient (voice)
      |                                        |
 acugenie-owner :8644                   reachyclinic :8642
      |                                        |
      |   read-only                            |   writes nothing
      +---------->  session store  <-----------+
                          |
                 Genie approves a fact
                          |
                 reference_catalog --> receptionist prompt
```

Two structural rules carry the asymmetry. Prompt instructions do not.

1. **The receptionist LLM writes nothing.** It keeps `reference_readonly` and no write
   tools. The store is produced by an offline job, not by the agent.
2. **The owner profile reads the store through a read-only plugin**, following the
   `reference_readonly` pattern. Not the `file` toolset, which bundles write and patch.

The receptionist has no read path into the owner profile at all.

### 4c. Store schema

The privacy property comes from the schema. Every field is a number, a timestamp, or a
member of a closed enum, so the record structurally cannot carry PHI.

```
conversation_id   opaque id
ts_started        timestamp
duration_s        number
turns             number
errors            number
model             label
asked             enum[]   see below
unanswered        enum[]   same enum: what it deferred to Genie
```

Enum members: `hours`, `parking`, `restroom`, `water`, `services`, `booking`,
`practitioner`, `wait_time`, `health`, `smalltalk`, `other`.

`health` is a count of health-topic turns. It never carries the topic.

### 4d. Derivation from Hermes artifacts

Two sources, neither of them robot runtime logs.

**Skeleton — `logs/latency-trace.jsonl`.** Group turn records by session id. First record
gives `ts_started`, last minus first gives `duration_s`, the count gives `turns`, and
`api_request_error` records give `errors`. All of this is already content-free by design.

**Enums — batch classification over `response_store.db`.** A scheduled job reads the
week's stored turns, classifies each with the local model, and emits **only enum
members**. The classifier's output schema is the privacy boundary: it cannot emit
free-form text even if the model produces some. Transcript text is read and never copied.

Run both on Sunday or Monday. The clinic is closed, so the local model does not compete
with Parakeet and Qwen3 TTS for the box while a patient is in the room.

The `response_store.db` read depends on Hermes v0.17.0 internal schema. That is acceptable
for a batch job: a version upgrade that breaks it degrades the week's digest to skeleton
only. Nothing in the live path depends on it.

### 4e. Known blind spot: this is a conversation log, not an arrival log

Greet, goodbye, and opener speech never invoke Hermes. A visitor who arrives, is greeted,
says nothing, and leaves therefore produces **no Hermes record of any kind**.

The store counts conversations. It cannot count arrivals, and the missing row is the one
with the most product value: the visitor the robot failed to draw in. Engagement rate
requires the vision/wave policy as a source and is out of scope here.

Name the metric `conversations` in every digest and dashboard so this is not rediscovered
later as a data bug.

### 4f. Review loop

A `cronjob` in the owner profile produces a weekly digest for Genie: conversation count,
median turns, error count, most frequent `asked` categories, and most frequent
`unanswered` categories.

Genie decides whether a repeated `unanswered` category becomes a clinic fact. If so, the
text is added to `clinic_facts.md` or a new catalog document, the profile is synced, and
the gateway is restarted — the extension point already defined in fork spec §6. Nothing
reaches a future visitor without that human step.

## 5. Privacy model

- No visitor identity is collected, so no consent flow is required for the store itself.
- The store holds counts and enums only, and is the durable record.
- Conversation text lives in `response_store.db`, which is Hermes's own artifact and
  already exists today.
- **Open issue, independent of this design:** `response_store.db` persists conversation
  chains indefinitely. m1max is therefore already retaining full patient conversation text
  with no retention policy. This predates this document and should be raised with Genie
  before a digest job makes it look like a deliberate pipeline. A retention window — 30
  days is a reasonable starting proposal — would leave counts durable and text short-lived.

## 6. Out of scope for the first pass

- MCP tool integrations for the owner assistant (calendar, email, Jane). Genie has not
  been consulted on what she needs; picking integrations now would be guessing.
- Patient identification, and therefore per-patient memory across visits. The path is
  already researched in fork spec §7 (external memory provider plus
  `X-Hermes-Session-Key`) and stays gated on identity and consent.
- Engagement rate and any arrival source outside Hermes.
- A local model for the receptionist. Fork spec §6b measured Hermes adding 1.5–2.9 s
  median TTFT with a cloud model; a local model sharing the box with STT and TTS moves the
  wrong way. This is where patient speech actually leaves the building, so it is worth
  revisiting, but not first.
- Any change to the live robot path.

## 7. Verification required before building

1. **Session id alignment.** Does the latency-trace session id map one-to-one to a single
   visitor conversation, or does Hermes issue session ids that do not align with the
   fork's `s2s-conv-<id>` reset boundary? If they do not align, conversation counts are
   wrong and the join to `response_store.db` is unreliable. This is the first thing to
   check and it blocks §4d. Not yet run: m1max was unreachable at drafting time.
2. **`response_store.db` schema.** Confirm the tables and columns that expose per-turn
   text and their conversation linkage on the installed v0.17.0.
3. **Local backend on m1max.** Confirm a `model.provider: custom` profile completes a
   tool-using turn against the chosen local server, and measure how it behaves while the
   robot stack is running, to size the closed-days constraint.

## 8. References

- [`hermes-s2s-fork-spec.md`](hermes-s2s-fork-spec.md) — deployed session integration,
  profile rules, memory lockdown, latency-trace observer, and the out-of-scope per-visitor
  memory research this document inherits.
- [`data-harness.md`](data-harness.md) — existing artifact taxonomy.
- Hermes docs: user-guide/configuration, reference/toolsets-reference,
  user-guide/profiles, user-guide/features/api-server.
