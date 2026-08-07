# Spec — S2S backend fork: Hermes conversation state + deterministic policy speech

Date: 2026-07-03; deployment status updated 2026-08-06
Status: implemented, tested, committed, and deployed; backend feature development paused for production preparation
Audience: implementing agent. Read this whole doc before coding.

## 1. Goal

Make the local S2S backend session-aware when its LLM slot points at the Hermes
agent wrapper, so one policy-level visitor conversation maps to one server-side Hermes
`conversation` (memory, context-file reads, and tool results persist across
turns), while **greet/goodbye/opener policy speeches synthesize exact fixed
text through the configured TTS backend without invoking any LLM**.

This requires forking the HF `speech-to-speech` package (pinned 0.2.10 today):
the LLM handler class is hardcoded in `s2s_pipeline.py` and has no plugin
seam. CLI flags are auto-generated from arguments dataclasses, so new dataclass
fields become `--responses_api_*` flags for free.

## 2. Decisions already made (do not re-litigate)

- **Session awareness is a required feature**, not an optimization. Fork it in.
- **Speculative-turn chain pollution is accepted for v1.** A VAD reopen sends
  the partial utterance and then the revised one; Hermes's chain keeps both
  while the local buffer rewrote in place. We will judge the real UX in live
  testing before adding any tighter fix. Emit a `logger.warning` at setup when
  conversation mode and speculative turns are both active.
- **Policy speeches (greet / farewell / opener) use the generic realtime
  `tts.create` event.** The backend validates the exact supplied text and
  injects it directly into the existing TTS queue. It must not invoke Hermes,
  the direct Responses client, STT, a second TTS engine, Piper, or cached WAVs.
- **Warmup must not hit Hermes.** Every Hermes `/v1/responses` call is a full
  agent run (prompt assembly + stored session). Skip warmup (or point it at
  the direct client) when conversation mode is enabled.
- **Known divergence, accepted for v1:** on barge-in the backend abandons the
  SSE stream but the Hermes agent run completes server-side and stores the
  *full* assistant text in the chain — the next turn's history claims the
  visitor heard everything. Verified structurally (no cancel propagation on
  that path); confirm live with the barge-in check in §8.
- **Known gap, accepted for v1:** greetings/goodbyes won't exist in the Hermes
  chain (they are deterministic TTS-only responses), so the first wave-chat turn's
  model doesn't know the robot already said "Welcome!". Generic greetings make
  this low-risk. Revisit only if live UX shows it.
- **A test-only Hermes profile is the mandatory first deployment target.** Draft
  profile content and all acceptance checks run against `reachyclinic-test`.
  The `reachyclinic` profile remains untouched until the persona and behavior
  are reviewed and approved.
- **Hermes profile selection is endpoint/process-level.** Each profile runs its
  own gateway and API port; changing the request `model` does not switch the
  active profile. S2S switches profiles by changing `S2S_RESPONSES_BASE_URL`
  (and the advertised `S2S_MODEL_NAME`).

## 3. Ground facts (verified 2026-07-02 and corrected during implementation 2026-07-14)

Backend package: `/Users/leon/projects/speech_to_speech_backend/.venv/lib/python3.12/site-packages/speech_to_speech`

- `LLM/responses_api_language_model.py` — `ResponsesApiModelHandler`:
  - `process()` (~line 429): pulls `instructions` from
    `response.instructions if response else runtime_config.session.instructions`;
    copies the chat buffer; `_apply_config()` (~line 150) wraps instructions in
    `build_voice_system_prompt()` and injects them as a **system message inside
    the chat buffer**; `_generate()` (~line 159) sends
    `client.responses.create(model, input=active_chat.to_responses_api_chat(),
    stream, extra_body, timeout, **optional_kwargs)` (~line 179). No
    `conversation`, no `previous_response_id`, no session id → every turn is a
    stateless full-transcript replay.
  - **Correction verified during implementation (2026-07-14):** a bare
    `response.create` was originally queued with `request.response=None`, which
    made it indistinguishable from an automatic VAD turn. The fork normalizes
    every explicit `response.create` to an empty non-null
    `RealtimeResponseCreateParams`; wave-chat auto turns from server VAD retain
    `response=None`. After that normalization,
    **`request.response is not None` means explicit LLM turn**. Policy speech no
    longer uses this marker; it uses `tts.create` and bypasses both LLM clients.
  - `warmup()` (~line 105): fires a real `responses.create` at startup.
  - `on_session_end()` (~line 492): per-websocket-session reset hook.
- `arguments_classes/responses_api_language_model_arguments.py`: dataclass →
  CLI flags; `rename_args(kwargs, "responses_api")` strips the prefix before
  `setup(**kwargs)` receives them.
- openai SDK in the backend venv is **2.28.0** and `responses.create` accepts
  `conversation=` natively (verified by signature inspection). No `extra_body`
  workaround needed.
- Hermes API server (`~/.hermes/hermes-agent/gateway/platforms/api_server.py`,
  v0.17.0): `conversation` names are created on first use (no error), resolve
  to `previous_response_id` chains, and persist in the profile's
  `response_store.db` (SQLite — survives gateway restarts). Stored chained
  history includes prior tool payloads, so an on-demand reference read happens
  once per conversation, not per turn.
  `instructions` param → ephemeral system prompt (the correct Hermes layer);
  if omitted on later turns it carries forward from the stored response, but
  we send it every turn for determinism.
  Multi-message `input` gets split as `input[:-1]` → history, last → user
  message — which is why conversation mode must send **only the new user
  message**, never the full buffer (double-history otherwise).
- Robot tools: the native live path sends `"tools": []`
  (`s2s_realtime.py:251`) and Hermes ignores client-supplied tools — no tool
  round-trip concerns.

## 4. Fork changes (implemented with focused tests)

Fork: https://github.com/noelotpyrc/speech-to-speech (created 2026-07-13). Branch
`reachy/conversation-state` off upstream commit `f3b1971` (`Prepare 0.2.10
release`). The upstream/fork repository did not contain the assumed `v0.2.10`
tag, so the release commit is the verified base. Keep `main` tracking upstream
so future rebases stay cheap. The implementation is pushed at
`a963ca68b9aa3599b7ea5eeabb9505a68263fbff`; m1max installs that exact sha (see
§5). The fork is public — keep it generic (feature flags only, nothing
clinic-specific or secret).

### 4a. `arguments_classes/responses_api_language_model_arguments.py`

Add fields (defaults preserve current behavior exactly):

```python
responses_api_conversation: bool = False
    # Server-side conversation state. Only for Hermes-compatible wrappers;
    # OpenAI/OpenRouter reject arbitrary conversation names.
responses_api_conversation_prefix: str = "s2s"
responses_api_direct_base_url: Optional[str] = None
    # Plain Responses endpoint for explicit response.create turns.
    # Falls back to responses_api_base_url when unset.
responses_api_direct_model_name: Optional[str] = None   # falls back to model_name
responses_api_direct_api_key: Optional[str] = None      # falls back to RESPONSES_API_DIRECT_API_KEY, responses_api_api_key, or env
```

### 4b. `LLM/responses_api_language_model.py`

- **`setup()`**: store the new options; build `self.direct_client = OpenAI(...)`
  from the `direct_*` values (reuse `self.client` when all three are unset);
  `self._conversation_id = None`; warn if `conversation` mode and
  `speculative_turns` are both enabled.
- **Warmup**: when `conversation` mode is on, warm the **direct** client only;
  do not send any request to the wrapper. Deterministic policy speech does not
  use either LLM client.
- **Conversation id helper** — lazy per session:

  ```python
  def _active_conversation_id(self) -> str:
      if self._conversation_id is None:
          self._conversation_id = f"{self.conversation_prefix}-{_generate_id('conv')}"
      return self._conversation_id
  ```

- **`process()` routing** (replaces the single `_apply_config` + `_generate`
  call site):

  ```python
  is_explicit_turn = response is not None        # explicit response.create
  if self.conversation_enabled and not is_explicit_turn:
      # Hermes lane: server-side state, delta-only input
      optional_kwargs["conversation"] = self._active_conversation_id()
      optional_kwargs["instructions"] = build_voice_system_prompt(instructions)
      input_items = [<last user message of active_chat>]   # see helper below
      client, model = self.client, self.model_name
  else:
      # Direct lane: today's stateless behavior, byte-for-byte
      self._apply_config(active_chat, instructions)
      input_items = active_chat.to_responses_api_chat()
      client, model = (
          (self.direct_client, self.direct_model_name)
          if self.conversation_enabled else (self.client, self.model_name)
      )
  ```

  With `responses_api_conversation=False` nothing changes anywhere.
- **`_generate()`**: accept `client`, `model`, and `input_items` as parameters
  instead of reading `self.client` / `self.model_name` / calling
  `active_chat.to_responses_api_chat()` inline. Mechanical change; the
  streaming/cancel/staleness logic is untouched.
- **Last-user-message helper**: `Chat` has no accessor for "the current turn's
  user message"; add a small method on `Chat` (`LLM/chat.py`) returning the
  final `RealtimeConversationItemUserMessage` in the buffer, rendered in
  Responses `input` format. Speculative revisions *replace* that item in
  place, so "last user message" is correct for revisions too.
- **`on_session_end()`**: add `self._conversation_id = None` so each realtime
  WebSocket starts a fresh Hermes conversation.
- **Do not touch** the local `Chat` buffer lifecycle. It still records
  explicit LLM and wave-chat turns and feeds speculative rewrites,
  compaction, and our artifact recording. It is simply no longer shipped whole
  to the wrapper. The `enable_lang_prompt` extra user message is not supported
  in conversation mode (it would pollute the chain); it's off by default —
  ignore it there.

### 4c. `api/openai_realtime/handlers/response.py`

- Normalize a bare explicit `response.create` to
  `RealtimeResponseCreateParams()` when constructing `GenerateResponseRequest`.
  This is the explicit-turn marker consumed by §4b. Automatic server-VAD turns
  continue to carry `response=None`.
- Focused fork verification: Responses API handler, Chat, and realtime-service
  suites pass (228 tests). The full suite currently aborts during collection in
  the unrelated local-model native-extension stack on this development host.

### 4d. Revised-turn assistant transcript delivery

Cherry-pick upstream commit `3e19ec6` so assistant text uses the same
generation-aware stale-output check as audio. `AssistantTextEvent` carries the
producing response's `cancel_generation`; both normal text dispatch and the
response-completion drain drop only stale generations. This preserves
`response.output_audio_transcript.done` for a valid revised response while
still suppressing text from a cancelled generation. The upstream regression
test reproduces the prior failure: current-generation audio and
`response.done` survived a stuck discard guard while assistant text was
blanket-dropped. Focused fork verification after the cherry-pick: 174 realtime,
LLM output, and Responses API tests pass.

### 4e. Generic deterministic TTS-only responses

- Add a validated client event:

  ```json
  {
    "type": "tts.create",
    "text": "Exact text to synthesize.",
    "metadata": {"source": "caller", "reason": "announcement"}
  }
  ```

- Reject blank text, unavailable TTS queues, and requests made while another
  response is active using the normal realtime error envelope.
- Allocate the normal response and item IDs, emit `response.created`, enqueue
  the exact text as both `AssistantTextEvent` and `TTSInput`, then enqueue
  `EndOfResponse`. The normal send loop consequently emits
  `response.output_audio_transcript.done`, streamed
  `response.output_audio.delta`, `response.output_audio.done`, and
  `response.done`.
- Copy optional string metadata into the response lifecycle. Carry the current
  cancellation generation on text, TTS, and completion so `response.cancel`
  and stale-output filtering behave exactly as they do for LLM responses.
- `tts.create` does not append user or assistant messages to the backend chat
  and never writes to the LLM input queue.

#### Policy TTS integration benchmark (2026-08-05)

`scripts/m1max/benchmark_policy_tts.py` exercised the deployed realtime
WebSocket on m1max with the production sequence `response.cancel` then
`tts.create`. It used one persistent session, one warmup per phrase, and 30
measured greet plus 30 measured goodbye responses. All 60 responses produced an
exact authoritative transcript, nonempty PCM audio, `response.output_audio.done`,
and a completed `response.done`; the backend log contained no error.

| Phrase | First audio P50 | First audio P95 | First audio max | Audio done P50 | Audio done P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Greet | 155.6 ms | 158.4 ms | 158.7 ms | 1095.0 ms | 1401.2 ms |
| Goodbye | 154.9 ms | 157.5 ms | 158.6 ms | 908.5 ms | 1760.7 ms |

Across both phrases, `response.created` P50 was `0.5 ms`, exact transcript
availability P50 was `11.2 ms`, and first-audio P50/P95 were `155.5/158.4 ms`.
These measurements end when the WebSocket client receives audio; they exclude
vision-policy dispatch, the robot audio queue, and physical speaker onset. The
full JSON report and representative WAVs are under
`artifacts/benchmarks/policy-tts-30x2-20260805*`.

## 5. Product-repo wiring

- The live app defines the visitor boundary, not the backend WebSocket lifetime.
  `ReceptionPolicy` awaits a `begin_conversation_session` capability before it
  marks an accepted wave active or opens the microphone gate.
  `S2SRealtimeHandler.begin_conversation_session()` reconnects before every
  visitor after the first, which clears both the backend `Chat` buffer and the
  fork's Hermes conversation id through `on_session_end()`. It also reconnects
  before the first visitor if a pre-wave policy greeting or any other request
  has already used the startup connection; a truly pristine startup connection
  can be reused. A reconnect failure leaves the policy conversation inactive
  and the audio gate closed, and the next accepted wave retries the connection
  without requiring an app restart. Handlers without this capability retain
  their existing behavior.
- `S2SRealtimeHandler.request_speech(text, metadata=...)` sends
  `response.cancel` followed by one generic `tts.create`. The cancel is a no-op
  when idle and gives deterministic policy speech priority over an in-flight
  conversational response. The live policy registers `speak_text` only through this method
  and supplies the configured greet, farewell, or conversation-opener text
  unchanged, with `source=reception_policy`, the policy `reason`, and the
  trigger event in metadata. Ordinary conversational generation continues to
  use server-VAD/Hermes; generic explicit LLM requests continue to use
  `conversation.item.create` plus `response.create`.
- `scripts/m1max/setup_s2s_backend.sh`: replace the `speech-to-speech==0.2.10`
  pip install with an install from the fork at a **pinned sha**:
  `uv pip install "git+https://github.com/noelotpyrc/speech-to-speech@<sha>"`.
  Never pin a branch head. Record the fork URL + sha in `runtime-info.json`
  and keep the script's existing guards (dry-run, running-backend check,
  package/CLI/import verification). For active debugging sessions the venv may
  temporarily point at a local clone via `pip install -e`; re-run the setup
  script afterwards to restore the pinned state.
  Current pin:
  `a963ca68b9aa3599b7ea5eeabb9505a68263fbff`.
- `scripts/m1max/run_s2s_backend.sh`: add env switches:

  ```bash
  S2S_RESPONSES_CONVERSATION=1        # → --responses_api_conversation
  S2S_RESPONSES_DIRECT_BASE_URL=...   # → --responses_api_direct_base_url
  S2S_RESPONSES_DIRECT_MODEL=...      # → --responses_api_direct_model_name
  S2S_RESPONSES_DIRECT_API_KEY=...    # optional override; exported as RESPONSES_API_DIRECT_API_KEY
  ```

  The launcher normally resolves the direct credential from
  `OPENROUTER_API_KEY`, exports it only in the child environment, and never puts
  it in process arguments. `live_ops.sh status` also redacts credential-like
  arguments as defense in depth.

  Guard: refuse `S2S_RESPONSES_CONVERSATION=1` unless
  `S2S_RESPONSES_BASE_URL` is also set (conversation names are
  wrapper-only). Expected first/staging Hermes-track launch:
  `S2S_RESPONSES_BASE_URL=http://127.0.0.1:8643/v1`
  (`reachyclinic-test`, wave-chat lane) +
  `S2S_RESPONSES_DIRECT_BASE_URL=https://openrouter.ai/api/v1` +
  `S2S_RESPONSES_DIRECT_MODEL=openai/gpt-5.6-luna` (explicit LLM lane).
  Note the direct lane needs the OpenRouter key even when the primary key is
  the Hermes `API_SERVER_KEY`. Port 8642 is reserved for the production
  candidate `reachyclinic` profile after the promotion gate in §6.

  Current model selection, updated 2026-07-30: both Hermes profiles use
  `openai/gpt-5.6-luna` with `agent.reasoning_effort: low` and latency-first
  provider routing. The direct explicit-response lane uses the same model without an
  explicit reasoning or provider-routing override, matching its benchmarked
  request shape.

## 6. Hermes profile setup (context for the same pass; docs-backed)

Repo is the source of truth for the fictional Lakeside test profile; no real or
private clinic data belongs in tracked files. A parameterized sync script
publishes modules from `profiles/clinic_receptionist/` to one explicitly named
Hermes profile:

| repo module | Hermes surface | content rule |
| --- | --- | --- |
| `personality.md` | selected profile's `SOUL.md` | personality only (Identity / Style / Avoid / Defaults). No facts, no workflow. |
| `HERMES.md` | generated in the context dir, pinned via `terminal.cwd` in the profile `config.yaml` | stable tracked instructions followed by prompt-delivered catalog documents; hard constraint: never invent unsupported facts or capabilities |
| `reference_catalog.yaml` | same context dir | versioned allowlist mapping stable reference IDs to visitor-safe files, routing metadata, and `prompt` or `on_demand` delivery |
| `clinic_facts.md` | same context dir | `clinic.facts`, composed into deployed `HERMES.md` via `delivery: prompt` |
| `capabilities.md` | same context dir | `clinic.capabilities`, composed into deployed `HERMES.md` via `delivery: prompt` |
| `hermes_plugins/reference_library/` | selected profile's `plugins/reference-library/` | generic read-only discovery plus conditional `reference_read` for on-demand entries; no clinic-specific paths or logic |
| `hermes_plugins/latency_trace/` | selected profile's `plugins/latency-trace/` | local metadata-only observer for turn and provider-request timing; never records conversation content or credentials |

### 6a. Staging and production profiles

| role | profile | API port | use |
| --- | --- | ---: | --- |
| mandatory first target | `reachyclinic-test` | 8643 | draft persona, isolated conversations/state, all non-robot acceptance checks |
| production candidate | `reachyclinic` | 8642 | unchanged until explicit persona/behavior approval, then promoted from the same repo modules |

- Create staging as an isolated blank profile with no bundled skills:
  `hermes profile create reachyclinic-test --no-skills`. Do not clone production
  memories, sessions, skills, or runtime state.
- Each profile has its own config, `.env`, `SOUL.md`, memories, sessions, skills,
  state database, and gateway process. Configure distinct API keys and ports.
- Run gateways independently. CLI selection uses
  `hermes -p <profile> ...`; S2S API selection uses that profile gateway's base
  URL. The Responses `model` field is cosmetic and must not be treated as a
  profile switch.
- The sync tool must require an explicit profile target and support staging
  without changing production. It must never copy `.env`, API keys, memories,
  sessions, response databases, or other runtime state.
- No test-profile deletion or database cleanup is part of this pass. Any later
  deletion requires explicit user confirmation immediately before it runs.
- Docs:
  https://hermes-agent.nousresearch.com/docs/user-guide/profiles/ and
  https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server/.

- Voice-output rules keep flowing per-request: the fork sends
  `build_voice_system_prompt(session instructions)` via the `instructions`
  parameter. When `S2S_RESPONSES_CONVERSATION=1`, OPS starts the live app with
  `--profile-owned-context`, making `session instructions` empty and recording
  `instructions_source=hermes-profile`. The fork still supplies its generic
  spoken-channel lead and voice rules; persona, facts, capabilities, and tool
  policy come only from the selected Hermes profile. The full tracked
  `instructions.txt` remains a direct-only fallback and is not injected into
  Hermes conversations.
- **Read-only reference library:** Hermes v0.17.0's built-in `file` toolset is
  not read-only: it always bundles `write_file` and `patch`, while `skills`
  bundles the mutating `skill_manage`. The API therefore enables only the
  profile-local `reference_readonly` plugin toolset and explicitly disables
  `file`, `skills`, terminal, web, and memory. The plugin always exposes the
  domain-neutral `reference_catalog(topic?)` discovery tool. It registers
  `reference_read(reference_id)` only when at least one catalog entry uses
  `delivery: on_demand`, and its JSON schema enumerates exactly those IDs. Reads
  reject prompt-delivered IDs, arbitrary/absolute paths, root escapes,
  non-visitor audiences, non-regular files, invalid UTF-8, and over-size
  documents; successful reads log only ID/hash/byte count, never content.
  Verify the active surface with `GET /v1/toolsets` after each profile restart.
- The catalog is the extension point: adding future pre-stored operational
  context requires a file plus catalog entry, not another plugin. Small context
  needed on most turns uses `delivery: prompt`; the sync script deterministically
  composes it after the stable tracked `HERMES.md` instructions. Larger or
  occasional context uses `delivery: on_demand` and is the only content exposed
  by `reference_catalog` and `reference_read`. The prompt teaches Hermes to use
  current context first and retrieve an on-demand reference when additional
  approved information is needed. Prompt-delivered IDs are omitted from tool
  discovery and rejected by `reference_read`; reference content remains data and
  cannot override system/profile instructions. Catalog insertion order controls
  generated section order. The composer uses each catalog title as the section
  heading, removes a matching source H1, demotes remaining source headings one
  level, confines source paths to the profile source directory, and rejects a
  generated prompt over Hermes's 20,000-character context-file default. Updating
  facts or capabilities therefore means editing their source Markdown, syncing
  the selected profile, and restarting that profile's gateway; stable
  instructions in the tracked base `HERMES.md` need not change. V1 accepts
  only `audience: visitor`, because every caller of this endpoint has the same
  authority. Patient-specific, staff-only, or otherwise caller-restricted data
  requires authenticated identity and authorization and must not be placed in
  this library. Fictional staging content may be tracked; real clinic catalogs
  and documents remain profile-local and outside Git.
- **No memory toolset:** Hermes's `USER.md`/`MEMORY.md` are
  single-owner files injected into every session: with the tool enabled the
  agent auto-saves visitor facts (verified: the 06-22 benchmark's scripted
  "Jordan Lee" persona was auto-written to `memories/USER.md`), which then
  leak into other visitors' conversations — cross-visitor contamination and a
  privacy hazard. Per-visitor memory, if ever wanted, is the
  `X-Hermes-Session-Key` / external-provider path (out of scope, §7). Docs:
  https://hermes-agent.nousresearch.com/docs/user-guide/configuration,
  /docs/reference/toolsets-reference, and /docs/user-guide/features/plugins.
- ~~Gated cleanup~~ **Done 2026-07-14:** the existing `reachyclinic` profile's
  `memories/USER.md` test data ("Jordan Lee") was cleared (0 bytes, verified).
  The memory toolset lockdown above prevents recurrence; check #8
  regression-tests it on staging without modifying production.
- **Persona promotion gate:** draft `personality.md` may be synced to
  `reachyclinic-test/SOUL.md` so its behavior can be evaluated. The user must
  review and approve the wording and staging behavior **before** it is synced
  to `reachyclinic/SOUL.md`; production remains untouched until then.
- **Gateway lifecycle (decided):** for this pass the Hermes gateway
  (`hermes -p reachyclinic-test gateway ...` on m1max for staging) is a
  manually-started prerequisite, like today. Do not wire either gateway into
  OPS as a managed resource; that's a later item. Staging tests should fail
  fast with a clear message if `GET http://127.0.0.1:8643/health` is
  unreachable.

### 6b. Text-only latency benchmark

`scripts/m1max/benchmark_hermes_text.py` measures the text generation path on
m1max without STT, TTS, websocket, or robot latency. It sends sequential
streaming Responses requests and records request-to-first-text (TTFT), total
completion latency, token counts, response IDs, semantic validity, and tool-call
counts. Reports contain no credentials, prompt text, response text, conversation
IDs, or visitor names. The three fictional scenarios cover prompt-delivered
clinic facts, an unsupported appointment action, and continued-conversation
recall. Samples fail fast on transport/SSE errors, missing text deltas,
incomplete streams, semantic mismatch, or any tool call.

The optional direct OpenRouter target receives the generated staging
`HERMES.md` as `instructions` and inline history for recall. It is a provider
baseline, not a pure measurement of local Hermes code: Hermes also assembles its
agent framework prompt and creates agent/session state. Compare token counts
alongside latency rather than attributing the full difference to gateway
processing.

Baseline recorded 2026-07-21 with `openai/gpt-5.4-mini`, one warmup and 10
measured sequential runs per target/scenario (60 measured samples total): all
semantic checks passed and no tool calls occurred. Hermes median TTFT was
1.51-2.86 s and median total latency was 1.57-2.99 s; direct median TTFT was
0.50-0.60 s and median total latency was 0.72-1.23 s. Hermes input-token medians
were 2,292-2,314 versus 484-501 direct. The full JSON report is
`artifacts/hermes-text-benchmarks/hermes-text-full-20260721T220047Z.json`.

### 6c. Hermes latency observer

The tracked `latency-trace` observer uses Hermes's existing `pre_llm_call`,
`post_llm_call`, `pre_api_request`, `post_api_request`, and
`api_request_error` hooks. The profile sync tool installs and enables it with
`reference-library`; no Hermes core patch or external observability service is
required for this first-pass measurement.

By default it appends JSONL to the selected profile's
`logs/latency-trace.jsonl`; `HERMES_LATENCY_TRACE_PATH` may override the
destination. New files are mode `0600`. Records are restricted to opaque
session/task/turn/request IDs, timestamps, model/provider/runtime labels,
counts, finish status, numeric usage, and error type/status. They never contain
prompts, user or assistant text, conversation history, context documents,
request/response bodies, tool arguments/results, credentials, or error
messages.

For a turn with no tool calls or retries, use:

```text
estimated Hermes/non-provider overhead
  = client request-to-completion
  - sum(post_api_request.api_duration)
```

The subtraction is intentionally labeled an estimate. In the installed Hermes
version, `api_duration` begins shortly before final request construction and
middleware and ends after the provider stream is consumed, so it includes a
small amount of local request preparation in addition to OpenRouter routing,
model work, network transfer, and stream consumption. Conversely, the client
measurement includes local HTTP/SSE ingress and egress outside the turn hooks.
Run the benchmark and gateway on the same host and correlate by the opaque
session/turn IDs to avoid clock skew.

Successful streams do not currently expose a provider-first-text observer
timestamp. Therefore this plugin can split total completion time into
inside-versus-outside the provider/API span, but cannot split TTFT into Hermes
preparation, OpenRouter/model TTFT, and Hermes token relay. That finer split
requires a later Hermes-fork hook at the successful stream's first content
delta; do not infer it by subtracting independent direct-provider samples.

Reasoning-token caveat, verified 2026-07-24: OpenRouter Chat Completions reports
reasoning usage under
`usage.completion_tokens_details.reasoning_tokens`, while the current observer
normalizes only the top-level `usage.reasoning_tokens` value. A trace value of
zero therefore does not establish that reasoning was disabled. Direct
OpenRouter probes confirmed 516 reasoning tokens for
`openai/gpt-5.4-mini` at `low` and 10,814 reasoning tokens for
`deepseek/deepseek-v4-flash` at `high`.

## 7. Explicitly out of scope for v1

- Committed-turns-only sending or any tighter speculative-turn reconciliation.
- Cancel propagation to Hermes on barge-in.
- Robot function tools through Hermes (native path sends `tools: []`).
- Injecting policy greetings into the Hermes chain as assistant context.
- Per-visitor memory / visitor profiles ("un-anon" module). Research
  (2026-07-14) confirmed the correct Hermes path when this is wanted: keep the
  built-in single-owner memory off, enable an external memory provider
  (e.g. Honcho — per-peer scoping, `sessionStrategy: per-session`,
  `runtimePeerPrefix` for unmapped users) and send a stable visitor identifier
  as the `X-Hermes-Session-Key` header (one-line addition in the fork's
  request path). Gated on two product decisions, not tech: where visitor
  identity comes from (verbal / check-in integration / vision) and
  consent/PHI policy for storing visitor profiles at a clinic. Also adds
  per-turn provider latency (prefetch + dialectic LLM calls; cadence-tunable).
  Docs: hermes docs → features/memory-providers, features/honcho;
  upstream multi-tenant limitation: hermes-agent issue #34352.

## 8. Acceptance checks (staging profile first, live last)

The independent commands, intended test layers, and practical promotion gates
are indexed in [Runtime Test Catalog](runtime-test-catalog.md). The catalog does
not replace the acceptance details below or aggregate the harnesses into one
runner.

All Hermes checks below target `reachyclinic-test` on port 8643. They must not
write to or restart the production-candidate `reachyclinic` profile.

The stable audio fixture is generated with
`.venv/bin/python -m reachy_mini_brain.official_runtime.s2s_replay_fixture`
(`prepare-s2s-replay-fixture` after package installation) from the aligned
input channel of run `official-live-20260625-133754`. It contains 12 semantic
visitor-turn WAVs under the ignored
`artifacts/hermes-s2s-e2e/<run-id>/stable-v2/` directory. Revision 2 widens
turns 09 and 12 after listening review found clipped opening words. The recipe uses curated
sample-clock ranges because later VAD wall timestamps in this run do not map
literally to speech boundaries. Speculative fragments are merged with 160 ms
internal silence; the harness must replay all 12 files on one WebSocket and
wait for each completed response before sending the next file.

**Known non-blocking replay limitation (2026-07-23):** turn 03's complete
fixture and the original 1.268-second VAD extraction both decode as
`Two thirty.` when sent directly to Parakeet. In the production-matching
two-turn stream, the persistent 512-sample rechunker and Silero state produce a
1.300-second final VAD array that decodes as `Two starty.` both through the
threaded STT handler and through a fresh direct Parakeet decode. Downstream
interpretation is not stable: a three-turn probe inferred 2:30, while the
2026-07-24 full replay transcribed `Too starty.` and asked for the appointment
time again. This does not block transport, lifecycle, or conversation-state
acceptance, but turn 03 must not be used for exact transcript or semantic
appointment-time assertions. Track that fidelity separately from the integration
test. Reproduction is provided by
`scripts/m1max/diagnose_vad_stt.py`; evidence is under
`artifacts/hermes-s2s-e2e/official-live-20260625-133754/diagnostics/`.

1. **Profile isolation:** staging is addressable on port 8643 and advertises
   `reachyclinic-test`; its sessions and response storage appear only under the
   staging profile. Production remains assigned to port 8642 but need not be
   running for staging acceptance.
2. **Regression (flag off):** with the fork installed and
   `responses_api_conversation=False`, existing offline suite passes and a
   direct-OpenRouter preflight run behaves identically to 0.2.10.
3. **Lane routing (text-only):** drive one policy `tts.create`, one explicit
   LLM `response.create`, and two wave-chat turns. Assert that policy speech
   produces the exact authoritative assistant transcript and audio lifecycle
   without any Hermes or direct-provider call; only wave-chat turns enter the
   named Hermes conversation; the explicit LLM turn alone hits the direct
   endpoint.
4. **State works:** turn 1 states a fact ("my name is X"), turn 2 asks for it
   with only the new user message on the wire; answer must recall it. Also
   verify a clinic-facts question answers from generated `HERMES.md` without a
   reference tool call. `GET /v1/toolsets` must show only tools registered by
   `reference_readonly` (currently `reference_catalog`; `reference_read` appears
   when the catalog has an `on_demand` entry) and must not expose `file`,
   `skills`, memory, web, or terminal.
5. **Streaming compat:** sentence-batch streaming works through the backend →
   Hermes SSE path (openai-python typed events parse).
6. **Session reset:** in one live-app runtime, open visitor conversation A,
   close it, then open visitor conversation B. Assert the accepted second wave
   reconnects the realtime WebSocket before its policy opener or microphone
   audio, creates a new Hermes conversation id, and cannot recall A's details.
   Also cover a pre-wave policy greeting before visitor A and verify A starts
   from a clean backend chat.
7. **Barge-in divergence probe (live, user present — confirm first):** barge
   in mid-answer, then ask "what did you just say?" — record the outcome in
   the run log; this calibrates whether the accepted divergence matters.
8. **Warmup:** backend start creates no Hermes session.
9. **No memory accrual:** run a conversation where the visitor volunteers
   personal info (name, insurance); afterwards
   `~/.hermes/profiles/reachyclinic-test/memories/USER.md` and `MEMORY.md` must
   be unchanged (memory toolset is off — see §6). Production memory files must
   also remain untouched throughout the staging pass.
10. **Latency trace:** after a staging gateway restart,
    `logs/latency-trace.jsonl` is created with mode `0600`; one text-only turn
    produces matched turn and API lifecycle records. Verify the records contain
    timing/identity/count metadata and do not contain the prompt, response,
    conversation history, request/response payloads, credentials, tool data, or
    error messages. Production traces remain untouched.
11. **Promotion gate:** after the user approves persona wording and staging
    behavior, publish the same reviewed modules to `reachyclinic`; before that
    approval, no production profile file is modified.

**Full audio replay result (2026-07-24):** staging run
`artifacts/hermes-s2s-e2e-runs/full-12turn-20260724-acceptance-01` completed
all 12 turns over one WebSocket. Every turn recorded final input transcript,
assistant transcript, first audio, audio completion, response completion, and
a nonempty assistant WAV; the event log contained no error event. Conversation
state retained Mike from turn 02 through the turn-08 recall question. Turn 03
exhibited the documented VAD/STT limitation above and did not communicate 2:30
on this run. Treat the run as a pass for integration plumbing and state
continuity, with turn-03 transcript/appointment-time fidelity tracked
separately.

### Repeated 12-turn model benchmark (2026-07-24)

The repeated benchmark used the same `stable-v2` 12-turn WAV fixture, S2S
backend, `reachyclinic-test` profile content, non-streaming Qwen3 TTS, and one
fresh WebSocket/Hermes conversation per run. Each model completed three runs,
for 36 visible turns per model and 72 visible turns total.

Configuration:

| field | DeepSeek runs | GPT runs |
|---|---|---|
| Model | `deepseek/deepseek-v4-flash` | `openai/gpt-5.4-mini` |
| Reasoning effort | `high` (lowest reasoning-enabled effort exposed by OpenRouter) | `low` (lowest reasoning-enabled effort exposed by OpenRouter) |
| `provider_routing.sort` | `latency` | `latency` |
| `provider_routing.require_parameters` | `true` | `true` |
| `provider_routing.data_collection` | `deny` | `deny` |
| `agent.service_tier` | unset | unset |

Artifact directories:

- `artifacts/hermes-s2s-e2e-runs/deepseek-high-3x12-20260724-run-01`
  through `-03`
- `artifacts/hermes-s2s-e2e-runs/gpt54mini-low-3x12-20260724-run-01`
  through `-03`

All six reports have `status=completed`, `connection_count=1`, 12 recorded
turns, 12 nonempty assistant WAVs, and zero `hf.realtime.error` events.

`transcript_to_first_audio` starts at the final input-transcript event and ends
at the first assistant-audio event. It includes Hermes, OpenRouter/model, and
non-streaming TTS time. `transcript_to_response_done` ends at the response
completion event. Fixture file completion is not used as speech end: the WAVs
contain trailing silence, so VAD can finalize the transcript before the harness
finishes sending the file.

Per-run medians:

| Model/run | Transcript to first audio P50 | Transcript to response done P50 |
|---|---:|---:|
| DeepSeek `01` | 3.583 s | 5.996 s |
| DeepSeek `02` | 2.850 s | 5.245 s |
| DeepSeek `03` | 2.915 s | 5.164 s |
| GPT `01` | 2.002 s | 3.413 s |
| GPT `02` | 1.872 s | 3.746 s |
| GPT `03` | 1.829 s | 3.053 s |

Pooled distributions over 36 visible turns per model:

| Metric | DeepSeek `high` | GPT `low` |
|---|---:|---:|
| Transcript to first audio P50 | 2.956 s | 1.883 s |
| Transcript to first audio P90 | 5.099 s | 3.238 s |
| Transcript to first audio P95 | 6.132 s | 3.417 s |
| Transcript to first audio max | 7.643 s | 5.099 s |
| Transcript to response done P50 | 5.737 s | 3.334 s |
| Transcript to response done P90 | 8.960 s | 6.202 s |
| Transcript to response done P95 | 9.558 s | 6.544 s |
| Transcript to response done max | 13.157 s | 7.552 s |

Paired by run number and turn index, GPT reached first audio earlier on 33 of
36 pairs and response completion earlier on 34 of 36 pairs. The median paired
difference was 0.958 s for first audio and 2.209 s for response completion.

Provider-span data excludes API calls whose returned assistant content and tool
call counts were both zero. DeepSeek recorded 48 provider calls, all
content-bearing. GPT recorded 58 provider calls: 46 content-bearing calls and
12 empty speculative-turn retries.

| Content-bearing provider API span | DeepSeek `high` | GPT `low` |
|---|---:|---:|
| P50 | 2.351 s | 1.312 s |
| P90 | 3.728 s | 2.410 s |
| P95 | 4.726 s | 2.751 s |
| Max | 6.943 s | 3.960 s |
| Mean | 2.681 s | 1.533 s |

Visible assistant response length:

| Metric | DeepSeek `high` | GPT `low` |
|---|---:|---:|
| Median words per turn | 16 | 7 |
| Mean words per turn | 18.2 | 9.4 |
| Median characters per turn | 82 | 34 |
| Mean characters per turn | 96.5 | 50.4 |

Repeated transcript observations:

| Fixture observation | DeepSeek `high` | GPT `low` |
|---|---|---|
| Turn 03 final input transcript | `Too starty.` in 3/3 runs | `Too starty.` in 3/3 runs |
| Turn 04 referenced or asked about an appointment | 2/3 runs; one response said Mike might be in the wrong place | 3/3 runs |
| Turns 05-06 described receptionist capabilities/directions/kiosk | 0/3 runs; all three described clinic departments/services | 3/3 runs |
| Turn 07 treated water availability as unknown | 2/3 runs; one response said there was usually a nearby water fountain | 3/3 runs |
| Turn 08 recalled `Mike` | 3/3 runs | 3/3 runs |
| Turn 09 identified as `Reachy Mini` | 3/3 runs | 3/3 runs |
| Medical-emergency/911 language | 0/36 visible turns | 0/36 visible turns |

Reasoning-setting verification used one additional direct OpenRouter logic
prompt outside the 12-turn replay. GPT `low` returned 516 nested reasoning
tokens, completed in 15.238 s, and answered `Cannot be determined`. DeepSeek
`high` returned 10,814 nested reasoning tokens and answered `B`. The prompt
constraints permit both `B` and `C` in the last slot.

## 9. References

- `experiments/agentic_api/README.md` — Hermes install/profile/API facts,
  earlier latency benchmark (its `hermes_conversation` mode already validated
  first-turn-instructions + delta-only requests against Hermes, mean request
  ~316 chars vs ~2.5k full-history).
- `docs/archive/research/custom-realtime-backend-research.md` — the archived #8 research plan this
  implements.
- `docs/todo-official-runtime.md` #8 — parent todo.
- Hermes docs: context files, skills, SOUL.md guide, memory, API server,
  configuration (see links in §6 and the research notes).
