# S2S main migration and client agent integration

Date: 2026-09-03
Status: backend migration validated; client integration implemented for offline acceptance
Audience: backend, receptionist-runtime, and operations maintainers

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
- Validated fork migration commit:
  `aaa7c75e1f16a6ccdcd902ea94af92e325ebd455` (`speech-to-speech==0.2.12`).
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

The migration must not reuse the managed production backend directory or port.

| Runtime | Directory | Port | Package | Service ownership |
| --- | --- | --- | --- | --- |
| Production | `/Users/leon/projects/speech_to_speech_backend` | `8765` | `0.2.10` / `a963ca6` | managed launchd service |
| Migration staging | `/Users/leon/projects/speech_to_speech_backend_migration` | `8766` | `0.2.12` / `aaa7c75` | manual test process only |

Production continues to use `setup_s2s_backend.sh`, `run_s2s_backend.sh`, and
the legacy `--mode realtime` CLI until promotion is approved. Migration staging
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

- Python `3.12.13`, `speech-to-speech==0.2.12`, and fork commit `aaa7c75` were
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

This smoke proves isolated installation, startup, and deterministic TTS. It does
not complete the text/audio turn, tool-loop, WAV replay, or live acceptance
gates in section 8.

## 3. Backend decisions

### 3.1 Preserve the current cascade

Start the migrated server with the same selected functional components:

- STT: `parakeet-tdt`;
- LLM backend: `responses-api`;
- production model: the configured GPT-5.6 Luna route;
- TTS: `qwen3`, Sohee, with the accepted m1max settings;
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

- Steps 1-5 are complete on fork commit
  `aaa7c75e1f16a6ccdcd902ea94af92e325ebd455`.
- Step 6 preserves all raw Realtime event types and adds content-free
  `response.done` output/usage summaries for artifacts.
- Steps 7-10 are implemented behind the opt-in client profile configuration.
- Offline client tests cover private overlays, reference confinement, bounded
  tool failures, sequential output ordering, follow-up generation, cancellation,
  and session teardown.
- Integration, WAV replay, live acceptance, Smart Turn evaluation, and promotion
  remain pending.

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
