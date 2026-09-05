# S2S main migration and client agent integration

Date: 2026-09-05
Status: staging and live backend acceptance passed; managed production promoted
Audience: backend, receptionist-runtime, and operations maintainers

The app/backend/config promotion completed on 2026-09-05. Current production is
app `ce95a49` with backend `2e4449c`, private client-owned profile, and default
`time-web` tools. The follow-up app release adds the approved Sohee delivery instruction;
the backend and dependency pins are unchanged. See the [promotion record](s2s-production-promotion.md) for managed
acceptance and rollback evidence. The implementation history below retains the
original migration baseline and staging decisions.

## 1. Goal

Migrate the forked Hugging Face `speech-to-speech` backend from the current
`v0.2.10`-based production baseline to a pinned upstream `main` revision while
preserving the accepted modular voice path:

```text
Reachy microphone
  -> Silero VAD
  -> Parakeet TDT STT
  -> S2S Chat (canonical active-session history)
  -> Responses API / configured LLM
  -> Qwen3 TTS
  -> Reachy speaker
```

`Realtime` describes the transport and interaction lifecycle. This migration
does not require an end-to-end audio model. Direct audio input with `--stt none`
is outside the initial migration.

The target also supports:

- dynamically selected private clinic profiles;
- client-executed, allowlisted tools;
- deterministic fixed policy speech without an LLM call;
- an explicit extension point for long-term visitor memory;
- the existing artifact and diagnosis workflow;
- rollback to the accepted production backend.

## 2. Pinned revisions and rollback

- Upstream migration candidate: `huggingface/speech-to-speech`
  `e34312cf47cd0159ee82f0d34b02e72353b7752e`.
- Current validated fork migration candidate:
  `2e4449c345c305e4ee6b9761f86c1849bbf3cb08` (`speech-to-speech==0.2.12`).
  It adds the accepted structured lifecycle trace to the functional baseline
  validated at `db1aeb000cbe53d23e65f49cc76e8b378e01a6d4`.
- Current backend rollback point: fork branch `reachy/conversation-state` at
  `a963ca68b9aa3599b7ea5eeabb9505a68263fbff`.
- The migration branch must be based on the exact upstream SHA, not a moving
  `main` reference.
- Before promotion, record the active receptionist release, backend dependency
  lock, launch configuration, and model assets alongside the backend SHA.
- Keep the current branch and deployed runtime available until all migration
  acceptance gates pass. The migration does not delete or overwrite rollback
  artifacts.

### 2.1 Staging isolation

Staging remains separate from the managed production backend directory and port.

| Runtime | Directory | Port | Package | Service ownership |
| --- | --- | --- | --- | --- |
| Production | `/Users/leon/projects/speech_to_speech_backend_2e4449c_frozen` | `8765` | `0.2.12` / `2e4449c` | managed launchd service |
| Migration staging | `/Users/leon/projects/speech_to_speech_backend_migration` | `8766` | `0.2.12` / `2e4449c` | manual test process only |
| Previous production rollback | `/Users/leon/projects/speech_to_speech_backend` | inactive; restore on `8765` | `0.2.10` / `a963ca6` | preserved directory |

Promoted production uses `run_s2s_backend.sh` with `S2S_CLI_MODE=serve`; the
legacy `--mode realtime` path remains available for rollback. Migration staging
uses `setup_s2s_backend_staging.sh` and `run_s2s_backend_staging.sh`, which pin
the new SHA, use the `serve` CLI, disable Smart Turn, and intentionally do not
enable provider-managed Hermes conversation state.

Installing staging may create or update only its dedicated directory. It must
not change the active release pointer, production venv, launchd definitions,
production service, or port 8765. A second fully loaded model stack may still
compete for unified memory and compute, so check m1max resources before starting
staging alongside production.

### 2.2 Isolated staging smoke (2026-09-03)

The migration runtime was installed on m1max in the staging directory above
without changing the active receptionist release or the production backend.
Acceptance evidence:

- Python `3.12.13`, `speech-to-speech==0.2.12`, and fork commit `db1aeb0` were
  recorded in the staging runtime metadata;
- `uv pip check` passed for all 124 installed packages;
- Parakeet, the direct OpenRouter Responses backend, and Qwen3 TTS initialized,
  and the Realtime server listened only on `127.0.0.1:8766`;
- one greet and one goodbye `tts.create` request produced exact transcripts and
  nonempty audio, with first audio at 222 ms and 147 ms respectively;
- production remained on frozen receptionist release `7840866`, S2S `0.2.10`,
  and port `8765` throughout the smoke;
- the warmed staging process used approximately 5.7 GB RSS, so it was stopped
  after the smoke and must not remain running alongside production unattended.

This smoke proves isolated installation, startup, and deterministic TTS. It did
not by itself complete the text/audio turn, tool-loop, WAV replay, or live
acceptance gates in section 8.

### 2.3 Text-only profile and history check (2026-09-03)

The local product client connected through an SSH tunnel to staging port `8766`
and ran two `input_text` turns on one Realtime WebSocket. Each
`response.create` requested `output_modalities: ["text"]`, so VAD, STT, and TTS
did not participate.

- Turn 1 used the fictional Lakeside profile to answer its weekday hours.
- Turn 2 recalled the visitor name supplied in turn 1 and combined it with the
  profile's second-floor location fact.
- Both responses completed and emitted only text-output lifecycle events.
- End-to-end turn times were 1.074 s and 2.421 s.
- The report recorded profile ID, source IDs, character count, and hash without
  recording the composed instructions.

This passes the basic profile injection and backend-local `Chat` continuity
gate. Tool execution, cancellation/revision behavior, audio turns, and WAV
replay remain separate acceptance steps.

### 2.4 Reference-tool integration check (2026-09-03)

The first attempt exposed an ambiguous client test-tool contract rather than an
S2S defect: the model supplied a natural-language topic to
`reference_catalog`, while the client implemented that filter as one literal
substring. The model-facing catalog tool was simplified to accept only `{}` and
return the complete allowlisted on-demand catalog. `reference_read` still
requires an exact enum-constrained ID from that catalog.

The rerun passed against isolated staging:

- tool order was `reference_catalog`, then `reference_read`;
- both tool results were submitted and each requested one follow-up response;
- all three provider responses completed;
- the final answer correctly identified East Lot C and the parking permit from
  reception;
- elapsed time was 14.878 seconds, including downstream response processing;
- provider-request tracing confirmed that every follow-up contained the
  cumulative tool call/output pairs and both tool schemas.

This passes the sequential read-only tool-loop integration gate. It does not
test cancellation/revision, audio input, WAV replay, or live robot behavior.

For an isolated diagnostic run, set both variables when launching staging:

```bash
S2S_STAGING_LOG_LEVEL=debug \
S2S_STAGING_LOG_TRANSCRIPTS=1 \
bash scripts/m1max/run_s2s_backend_staging.sh
```

The migrated backend also has an opt-in structured lifecycle trace:

```bash
S2S_STAGING_EVENT_TRACE_DIR=/Users/leon/projects/reachy_mini_receptionist_deploy/artifacts/s2s-backend-trace \
bash scripts/m1max/run_s2s_backend_staging.sh
```

This writes owner-only JSONL files and exposes writer health under
`/v1/pool.event_trace`. Transcript bodies remain absent unless
`S2S_STAGING_LOG_TRANSCRIPTS=1` is also set; hashes and sizes are retained by
default so adjacent payload boundaries can still be compared.

`S2S_STAGING_LOG_TRANSCRIPTS` is off by default. When enabled, full provider
text and tool payloads are sensitive. The manual staging launcher writes its
ordinary logs only to its attached terminal; the structured trace is retained
only when `S2S_STAGING_EVENT_TRACE_DIR` is set. The retention report must cover
that directory before production enablement. Embedded audio and image data are
always omitted from provider-payload lineage.

### 2.5 First reviewed audio-turn replay (2026-09-03)

The product replay client streamed reviewed fixture turn 1 through one explicit
visitor conversation on the isolated migration backend. The replay used the
tracked fictional Lakeside profile and the same client-owned reference-tool
registry as the live runtime.

- Parakeet produced the exact reviewed transcript: `Hey, nice to meet you.`
- The model welcomed the visitor to Lakeside Family Clinic and asked how it
  could help.
- The selected response ended with status `completed`.
- Qwen3 TTS produced 118,272 PCM16 samples, or 7.392 seconds of audio.
- Transcript completion to first audio was 1.762 seconds; transcript completion
  to `response.done` was 4.375 seconds.
- Production remained available on port 8765 while staging used port 8766, and
  staging was stopped after the test.

Evidence is in
`artifacts/s2s-main-migration/audio-turn1-20260903-0001/`. This passes the first
audio-turn plumbing check. Multi-turn replay, semantic continuity across all 12
turns, cancellation/revision behavior under audio input, and live acceptance
remain separate gates.

### 2.6 Full 12-turn replay blocked by TTS output (2026-09-03)

Run `artifacts/s2s-main-migration/full-12turn-20260903-0001/` replayed all 12
reviewed WAVs in one fresh visitor session. STT, model text, state continuity,
and response lifecycles completed, but the run failed audio acceptance:

- all 12 final responses had status `completed`, nonempty audio, and no
  `hf.realtime.error` event;
- Mike was retained through the session, profile behavior was appropriate, and
  turn 3 reproduced the documented `Too starty.` STT limitation;
- turns 1 and 3 through 10 produced approximately 28.4-28.8 seconds of Qwen
  output for short replies;
- the backend independently logged those same Qwen generation durations, so
  the repeated ceiling was not introduced by the replay collector;
- turns 3 through 10 contained approximately 21-27 seconds of low-level
  trailing audio after the apparent speech ended, while turn 1 remained
  acoustically active through the generation limit;
- transcript-to-first-audio P50 was 1.388 seconds, but the invalid long outputs
  raised transcript-to-response-done P50 to 11.617 seconds.

The staging process was stopped and production remained available on port
8765. Do not promote the migration candidate until Qwen termination is isolated
and the 12-turn replay is repeated successfully. The current replay harness
rejects empty output but does not automatically classify excessive duration or
trailing low-level audio; this run was stopped by evidence review.

### 2.7 Accepted macOS MLX dependency baseline (2026-09-04)

The repeated Qwen output ceiling was isolated with the same fixed
`tts.create` text in 30-request runs. The request text was:
`Nice to meet you, too. Welcome to Lakeside Family Clinic. How can I help?`

| S2S source | macOS MLX stack | Exact transcripts | 360-token cap hits | Result |
| --- | --- | ---: | ---: | --- |
| Production `0.2.10` fork | `mlx-audio 0.4.2`, MLX `0.31.1` | 30/30 | 0/30 | varied 4.288-9.088 s outputs |
| Migration `0.2.12` fork | `mlx-audio 0.4.7`, MLX `0.32.0` | 30/30 | 30/30 | identical 28.768 s outputs |
| Migration `0.2.12` fork | `mlx-audio 0.4.2`, MLX `0.31.1` | 30/30 | 0/30 | varied 5.024-9.088 s outputs |

The migration fork therefore freezes the accepted Darwin dependency family:

```text
mlx==0.31.1
mlx-audio==0.4.2
mlx-lm==0.31.1
mlx-metal==0.31.1
```

`mlx-audio 0.4.2` also resolves `pyloudnorm 0.2.0` transitively. The fork's
`pyproject.toml` and macOS install smoke must agree on these versions. The
upstream repository ignores `uv.lock`, so the package declaration is the
versioned resolver contract. The m1max staging setup independently verifies the
installed versions and writes them into `runtime-info.json`.

This establishes the updated dependency family as the ownership boundary of
the regression; it does not isolate one package within that family. Do not
advance these pins as part of an upstream merge without rerunning the fixed-text
cap test, Parakeet/Qwen startup checks, the 12-turn WAV replay, and the selected
STT/TTS stress checks.

Evidence is under:

- `artifacts/s2s-main-migration/qwen-version-ab-20260903/frozen-0210-mlx042-r3/`;
- `artifacts/s2s-main-migration/qwen-version-ab-20260903/migration-0212-mlx047-r1/`;
- `artifacts/s2s-main-migration/qwen-version-ab-20260903/migration-0212-mlx042-r1/`.

The dependency blocker is resolved. Section 2.8 records the successful full
replay on this accepted stack.

### 2.8 Accepted full 12-turn replay (2026-09-04)

Run `artifacts/s2s-main-migration/full-12turn-20260904-0001/` replayed the same
reviewed `stable-v2` WAV fixture through one fresh visitor session on fork
commit `db1aeb0` and the accepted MLX dependency baseline from section 2.7.

- all 12 selected turns completed with matching assistant transcript, audio
  completion, `response.done`, and nonempty decoded PCM;
- the event artifact contains 12 final response lifecycles and no
  `hf.realtime.error` events;
- all 12 output WAVs were distinct, ranging from 2.144 to 18.208 seconds, with
  a median of 6.496 seconds and no 28.768-second generation-cap result;
- transcript-to-first-audio latency had a 1.557-second median and 3.136-second
  maximum;
- transcript-to-response-done latency had a 4.104-second median and
  8.275-second maximum;
- turn 3 reproduced the known `Too starty.` STT limitation;
- revised inputs on turns 2 and 9 cancelled stale generations and produced the
  complete final transcripts;
- the session retained Mike's name and the Lakeside profile through turn 12.

Production remained available on port 8765 throughout the replay. The isolated
staging process on port 8766 was stopped afterward. This passes the full
12-turn WAV replay gate. Live robot acceptance, Smart Turn evaluation, and
promotion remain pending.

### 2.9 Structured trace turn-1 validation (2026-09-04)

The reviewed turn-1 WAV was replayed through the isolated migration backend
after adding the explicit internal/public response mapping. The backend trace
contained one `public_response.created` and one
`public_response.completed` event. Both records carried the same pair:

```text
response_key=f07c834d35274b4bbd29d114cd21a4a8
response_id=resp_81d031fbd49c490ca99fcd2b766c9dc3
```

The client `response.created`, all 32 audio deltas, and `response.done` used
that same public response ID. The backend and client also recorded the same
session ID. Payload lineage was complete: the STT hash matched through LLM
enqueue, the assistant-text hash matched across LLM sentence output, output
processing, TTS enqueue, and Qwen input, and Qwen's 159,744 PCM bytes matched
the backend transport and client output.

The backend JSONL had 46 contiguous, unique sequences and event IDs. Captured
writer health reported an empty queue, zero dropped events, and zero write
errors. No error, cancellation, stale-discard, or tool event was expected or
observed in this single normal turn. Evidence is under
`artifacts/s2s-main-migration/event-trace-turn1-20260904-155442/`.

The replay report's negative `input_done_to_transcript_s` is not a backend
trace gap: `input_done_ts` means the end of streaming the complete 2.8-second
fixture, while VAD endpointed and transcribed its 1.78-second speech segment
before the fixture's trailing silence finished streaming.

## 3. Backend decisions

### 3.1 Preserve the current cascade

Start the migrated server with the same selected functional components:

- STT: `parakeet-tdt`;
- LLM backend: `responses-api`;
- production model: the configured GPT-5.6 Luna route;
- TTS: `qwen3`, Sohee, with the accepted m1max settings;
- macOS MLX dependencies: the frozen versions in section 2.7;
- transport: Realtime WebSocket;
- pipeline count: one unless separately evaluated.

Do not combine the migration with a provider, model, voice, transport, or
recording-policy change.

### 3.2 Baseline with Smart Turn disabled

Pass `--no_smart_turn` for initial parity tests. The current production backend
uses Silero-only endpointing, while upstream `main` enables Smart Turn v3.2 by
default. Smart Turn changes speculative processing, turn reopening, and output
commit timing. Disabling it initially isolates migration regressions.

After baseline acceptance, run Smart Turn as an independent A/B evaluation. It
may improve eager-speaker and mid-thought-pause handling, but it is not required
for migration acceptance.

### 3.3 Port deterministic `tts.create`

Retain the generic `tts.create` extension for greet, goodbye, and conversation
opener speech:

- validate nonblank authoritative text;
- bypass STT and the LLM;
- enqueue the text through the selected TTS backend;
- emit the normal response transcript, audio, completion, and metadata events;
- remain cancellable through the normal response generation mechanism;
- do not append policy speech to conversational Chat history.

### 3.4 Preserve Hermes routing as compatibility mode

Port the existing stateful Responses conversation routing and direct-provider
credential path behind disabled-by-default flags. It remains useful for rollback
and controlled comparison, but it is not the migrated default.

Default mode:

```text
S2S Chat owns canonical history
  -> each Responses request receives the canonical context snapshot
```

Compatibility mode:

```text
--responses_api_conversation
  -> Hermes/provider owns named conversation state
  -> S2S sends only the newest user message
```

Compatibility mode retains its known limitation: a cancelled speculative run
may already have changed provider-owned history. Provider conversation state is
not the long-term visitor-memory design.

Retain the associated options for conversation prefix, direct base URL, direct
model, and direct API key. Credentials remain environment-owned and never appear
in process arguments or artifacts.

### 3.5 Use upstream response-lifecycle fixes

The revised-turn assistant transcript fix from our fork is already upstream and
must not be duplicated. Preserve its regression test: after a superseded
generation, current-generation assistant text and audio must both survive, and
the client must receive `response.output_audio_transcript.done` plus a terminal
`response.done`.

Also validate the newer upstream `response.done.output`, transactional Chat
commit/rollback, output ordering, and cancellation behavior against our artifact
consumers.

## 4. Receptionist client agent integration

The S2S server owns VAD, STT, active-session Chat, LLM generation, response
lifecycle, and TTS. The receptionist client owns private profile selection and
all local tool execution.

### 4.1 Profile composition

At visitor-session creation, the client:

1. selects the configured profile;
2. loads version-controlled public profile material;
3. loads the private clinic overlay from a path outside Git;
4. validates required sections and size limits;
5. composes one deterministic instruction document;
6. sends it in `session.update` for the new WebSocket session.

The selected profile is fixed for that visitor session. Switching profiles
requires a new session so conversation state and tool authorization cannot cross
profile boundaries.

Operational logs may contain profile ID, source identifiers, byte/character
counts, and content hashes. They must not contain private profile text,
credentials, or unrestricted filesystem paths.

Frequently needed clinic facts and capabilities belong in the composed
instructions. Larger optional references remain available through tools.

#### Production profile review (2026-09-04)

The production migration reuses the approved Hermes content rather than
maintaining a second clinic instruction format:

```text
SOUL.md
  + generated HERMES.md (base operating context + prompt-delivered documents)
  + spoken-response instructions
  + optional tool usage guidance
  + runtime local-date context (America/New_York)
  -> session.instructions
```

`profile_context.compose_context_document` is the shared renderer used by the
Hermes sync script and `compose_hermes_agent_profile`. The latter accepts the
original source directory containing the **base** `HERMES.md`, the deployed
`SOUL.md`, and an explicit spoken-instruction file. Do not pass the generated
deployed `HERMES.md` as the base: it already contains the prompt documents.
Source order and heading formatting match the Hermes sync script. The complete
S2S instruction string is bounded to 20,000 characters and has content-free
hash/source provenance. No public fictional-profile fallback is used.

The preview applies `with_session_date` after optional tool guidance. This final
assembly step adds a clock-derived local date/weekday and timezone, includes it
in the final hash, and records `runtime:local_date` provenance. The base composer
and editable profile files stay date-independent. It is a labelled date snapshot;
`time_now` supplies exact time or an updated date when needed. See
[Reception Agent Tools](reception-agent-tools.md) for runtime refresh scope.

Local review command (does not contact a backend or change production):

```bash
PYTHONPATH=src .venv/bin/python scripts/compose_s2s_profile.py \
  --profile-id reachyclinic \
  --source-dir private/profiles/clinic_receptionist \
  --soul private/profiles/clinic_receptionist/personality.md \
  --spoken-instructions profiles/clinic_receptionist/session_instructions.txt \
  --output private/profiles/clinic_receptionist/S2S_SESSION_INSTRUCTIONS.preview.md
```

`--soul` accepts the original `personality.md` that Hermes publishes as `SOUL.md`.
Use that editable source after review changes; the deployed `reachyclinic-hermes`
snapshot is a comparison baseline, not the authoring copy.

The preview is created owner-only and existing files are never overwritten.
Private source files, deployed snapshots, and preview output remain Git-ignored.
The snapshot copies content only, not Hermes credentials, sessions, databases,
or plugins. Review and approve the text before wiring this composer into live
startup; the existing test composer and production runtime remain unchanged.

The first-pass tool candidates are now `time_now` and Firecrawl `web_search`,
implemented locally for testing. See [Reception Agent Tools](reception-agent-tools.md)
for the shared credential, result bounds, and explicit enablement. The reference
tools below remain integration-test infrastructure, not automatically enabled
production functionality. This composer registers no tools; `--tools time-web`
on the preview command only appends the corresponding usage instructions.

### 4.2 Tool contract and registry

Add a client-owned tool registry. Each registration contains:

- a stable tool name and description;
- a JSON Schema argument contract;
- an asynchronous execution callback;
- an authorization policy;
- a timeout;
- a maximum serialized result size;
- a follow-up-response policy.

Initial tools:

- `reference_catalog`: list references available to the selected profile;
- `reference_read`: read one allowlisted reference by catalog ID.

The tools must reuse the existing read-only reference-library behavior. They do
not expose arbitrary path reads, directory traversal, shell execution, or file
writes.

### 4.3 Tool execution context

Every callback receives a constrained application context:

```text
profile_id
visitor_session_id
optional visitor_id
reference_store
optional memory_store
cancellation signal
event sink
```

The executor resolves all resources through that context. Model-provided
arguments cannot select another profile, visitor namespace, or storage root.

### 4.4 Realtime tool coordinator

Adapt the upstream packaged-client coordinator to the Reachy handler's raw JSON
WebSocket and frame-stream interfaces. Do not use the packaged `sounddevice`
client as the robot audio transport.

For each tool call:

```text
response.function_call_arguments.done
  -> correlate response ID, output index, and call ID
  -> validate tool name and JSON arguments
  -> execute through the allowlisted registry
  -> serialize a bounded result
  -> send conversation.item.create(function_call_output)
  -> send one follow-up response.create after required results are delivered
```

First-pass execution is sequential. Results preserve protocol output order.
Tool failure returns a bounded structured error to the model rather than
terminating the media loop.

Cancellation and session isolation are mandatory:

- cancellation stops pending callbacks when possible;
- stale results are never submitted to a newer response;
- reconnecting or ending a visitor session closes the coordinator and cancels
  outstanding work;
- no result can cross a connection generation or visitor-session boundary;
- tool follow-up generation cannot race an active response.

## 5. Long-term memory extension point

### 5.1 Ownership boundary

- S2S `Chat` owns short-term context for the active visitor session.
- A future application-owned `MemoryStore` owns durable visitor memory.
- Provider-managed Responses conversations are compatibility behavior, not the
  durable memory source of truth.

Define the `MemoryStore` protocol and include it in the tool execution context,
but a production database is not required for initial migration acceptance.

### 5.2 Namespace and identity

Durable memory is scoped by at least `profile_id` and a verified `visitor_id`.
An anonymous visitor session does not automatically acquire or write durable
memory. A future identity system may map a returning visitor to an old namespace
without changing the S2S protocol or tool coordinator.

### 5.3 Read and write policy

Add read capability first, for example `memory_search`, after the identity and
retention policy is approved. Do not give the model unrestricted database or
filesystem access.

Any future memory write must pass deterministic application policy. Persist only
finalized, committed turns or explicitly approved facts. Never persist:

- speculative transcript revisions;
- cancelled or discarded assistant output;
- raw tool arguments/results by default;
- unidentified visitor conversations;
- secrets or unrestricted clinic records.

Durable writes may run asynchronously after turn commitment so they do not add
latency to response generation.

## 6. Observability

Preserve the current application-level event, audio, manifest, audio-review,
and Rerun artifact contracts. Adapt parsers only where upstream event shapes
changed.

The backend-side implementation is specified in
[`s2s-backend-event-trace.md`](s2s-backend-event-trace.md). That trace is the
source of truth for internal stage transitions and payload lineage; the
application artifacts remain the source of truth for client-visible Realtime
events and robot-sink delivery.

For response lifecycle, retain or add:

- connection generation, visitor session, turn, revision, response, item, and
  call identifiers;
- speech start/stop and STT partial/final timing;
- LLM request, first output, completion, failure, and cancellation timing;
- terminal response status, reason when supplied, usage, and output summary;
- TTS start, first audio, audio completion, and robot-sink timing;
- stale/discard decisions and their generation identifiers.

For tools, record:

- response ID, output index, call ID, and allowlisted tool name;
- validation, queue, execution, result-submission, and follow-up timestamps;
- timeout, cancellation, or bounded error category;
- serialized result size, but not result content by default.

Production operational logs remain content-free by default. Transcript-bearing
diagnosis is explicitly enabled only for controlled runs and written to the
existing protected artifact path.

## 7. Implementation sequence

1. Record rollback metadata and create a migration branch from the pinned
   upstream SHA.
2. Verify an unmodified pinned backend can install and expose the expected CLI
   and selected component options on a staging environment.
3. Port `tts.create` with focused backend tests.
4. Port disabled-by-default Hermes conversation/direct routing with focused
   compatibility tests.
5. Add isolated m1max staging setup and launch scripts for the new CLI,
   dependency lock, port 8766, and `--no_smart_turn` baseline. Keep production
   setup and launch defaults unchanged until promotion.
6. Preserve and adapt application artifacts for the newer Realtime event shape.
7. Implement profile composition at client session creation.
8. Implement the allowlisted tool registry and constrained execution context.
9. Adapt the upstream coordinator into `S2SRealtimeHandler` with cancellation,
   ordering, and session-isolation tests.
10. Define the disabled `MemoryStore` extension point; do not add production
    persistence yet.
11. Run offline and integration acceptance in order, stopping on the first
    unexplained failure.
12. Run a short human-gated live robot acceptance only after offline gates pass.
13. Evaluate Smart Turn independently and record the enable/disable decision.
14. Promote the tested backend SHA and retain the rollback tag/runtime.

### 7.1 Current implementation status

- Steps 1-5 are complete on the functional baseline
  `db1aeb000cbe53d23e65f49cc76e8b378e01a6d4`. The current candidate
  `2e4449c345c305e4ee6b9761f86c1849bbf3cb08` adds the validated structured
  backend lifecycle trace without changing that data path.
- Step 6 preserves all raw Realtime event types and adds content-free
  `response.done` output/usage summaries for artifacts.
- Steps 7-10 are implemented behind the opt-in client profile configuration.
- Offline client tests cover private overlays, reference confinement, bounded
  tool failures, sequential output ordering, follow-up generation, cancellation,
  and session teardown.
- Basic text-only profile and backend-local history integration passed on
  2026-09-03. The sequential reference-tool integration also passed on that
  date. Reviewed WAV turn 1 also passed through STT, the profiled model request,
  and TTS with a completed nonempty response. The first full 12-turn attempt
  exposed the newer MLX stack's repeated 28-second Qwen outputs. The migration
  fork now freezes the validated MLX versions from section 2.7, and the repeated
  full replay passed as recorded in section 2.8. Live acceptance, Smart Turn
  evaluation, and promotion remain pending.

The opt-in client profile is configured with:

```text
RECEPTION_AGENT_PROFILE_ID=reachyclinic
RECEPTION_AGENT_PROFILE_PRIVATE_DIR=/path/outside/git/to/reachyclinic
```

`RECEPTION_AGENT_PROFILE_PUBLIC_DIR` may override the tracked public base. The
private directory path is passed through the process environment and is not
recorded in runner state or runtime artifacts. The profile ID, logical source
IDs such as `private:clinic_facts.md`, character count, hash, and tool names are
recorded.

## 8. Acceptance gates

### Backend and protocol

- Backend starts with Parakeet, Responses API, Qwen3, and Smart Turn disabled.
- Existing Realtime WebSocket session setup remains compatible with the Reachy
  client.
- Text-only and audio turns complete with ordered transcript/audio lifecycle
  events.
- Cancellation, barge-in, transcript revision, reconnect, and session teardown
  pass focused tests.
- The historical revised-turn gap cannot recur: valid current-generation text
  and audio both reach the client.
- `tts.create` returns exact text and nonempty audio without an LLM request.
- The installed Darwin MLX family matches the frozen versions in section 2.7,
  and the fixed-text cap regression test completes without a limit hit.
- Optional Hermes mode passes its existing state-continuity tests while disabled
  mode remains unchanged.

### Profile and tools

- A test-only profile is selected and composed without placing private data in
  Git or ordinary logs.
- Injected facts are answered directly from session instructions.
- Optional references are discoverable and readable only through catalog IDs.
- Unknown tools, invalid arguments, path escapes, oversized results, timeouts,
  and tool exceptions are bounded and observable.
- Multiple calls execute sequentially and return results in protocol order.
- Cancellation and session replacement prevent stale tool results from being
  submitted.

### End-to-end

- Existing text-only scenarios pass.
- The accepted 12-turn WAV replay completes with nonempty response audio and no
  unexplained lifecycle errors.
- Latency and Qwen audio pacing are compared with the production baseline.
- Existing audio-review and Rerun artifacts render the migrated events.
- A short live run confirms microphone input, interruption, smooth speech,
  profile behavior, one reference tool call, and deterministic policy speech.

## 9. Stop conditions

Stop migration work and preserve evidence when any of these occurs:

- an upstream change cannot reproduce current STT, LLM, or TTS configuration;
- `tts.create` cannot be ported without bypassing normal response cancellation;
- valid revised-turn assistant text or audio is dropped;
- the new client coordinator can submit a stale or cross-session tool result;
- private profile/tool content enters Git or ordinary operational logs;
- dependency changes materially reduce Qwen throughput or destabilize Parakeet;
- an offline acceptance test fails for an unexplained reason.

Do not compensate for an upstream or provider bug with an unbounded monkey patch.
First isolate the failing ownership boundary and decide whether to configure,
adapt, report upstream, or retain the previous backend.
