# Agentic Backend Research: Hermes / Context Memory

Status: active next-feature research, updated 2026-06-23.

This document supersedes the earlier LiveKit-first backend-replacement framing. The accepted live path
is now:

```text
official_runtime.live_app
  -> m1max local speech-to-speech backend
       STT: Parakeet TDT
       LLM slot: OpenRouter-compatible Responses API
       TTS: Qwen3 / Sohee
  -> Reachy Mini speaker/mic/video/movement
```

The next feature is not replacing the realtime audio stack. The next feature is improving the **LLM
slot** with clinic context, memory, and agentic session handling.

LiveKit backend-replacement work is no longer the current direction. The old LiveKit implementation
plan is archived at `docs/archive/research/plan-livekit-backend.md`.

## Question

Can we put an agentic API between the local S2S backend and the remote LLM provider so the receptionist
gets better clinic context, session memory, and future tool policy without rewriting the robot audio
runtime?

Target comparison:

```text
current baseline:
  local S2S backend LLM slot
    -> OpenRouter-compatible /v1/responses
    -> remote LLM model

agentic experiment:
  local S2S backend LLM slot
    -> local agentic wrapper /v1/responses
    -> Hermes-agent-style session/memory/context layer
    -> remote LLM model
```

## Current Decision

Use the current official-runtime + m1max S2S stack as the product baseline. Test an agentic wrapper
only at the text LLM boundary.

Do not change these in the first pass:

- Robot mic/speaker/video path.
- Reception policies.
- Audio gate and wave-chat flow.
- STT model.
- TTS model/voice.
- Output playback handler.

The wrapper must be swappable with the existing direct OpenRouter Responses call, so we can A/B latency
and quality without changing the live robot runtime.

## Why This Is The Right Boundary Now

The validated stack already solved the urgent live UX issue better than the legacy daemon:

- Official-runtime stream loop and robot speaker handling are accepted for basic live testing.
- Local S2S backend gives us controllable STT/TTS and a backend process we can keep warm on m1max.
- The remaining product issue is receptionist intelligence: clinic facts, stable tone, memory, and
  eventual tools.

Replacing the entire audio backend now would add risk in the part that just started working. Replacing
or wrapping the LLM slot is narrower and easier to benchmark offline.

## Agentic Backend Goals

First-pass goals:

- Add real clinic context without stuffing every turn with ad hoc prompt text.
- Preserve short-term conversation memory across a wave-chat session.
- Support clear session reset boundaries when a visitor leaves or the audio gate closes.
- Compare latency and answer quality against direct OpenRouter Responses API.
- Log enough metadata to diagnose what context/memory was used for each response.

Later goals:

- Cross-session memory only after we have a policy for what is safe/useful to remember.
- Tool use for clinic FAQs, staff lookup, hours, appointment routing, and escalation.
- Better guardrails for receptionist tone and uncertainty.

Out of scope for this research pass:

- LiveKit voice backend replacement.
- New STT/TTS provider selection.
- Robot movement changes.
- Multimedia backend replacement.

## Required API Shape

The local S2S backend currently expects a Responses-API-like LLM path. The cleanest experiment is a
local wrapper that exposes:

```text
POST /v1/responses
```

and forwards internally to Hermes / an agentic runtime / a remote LLM provider.

Useful secondary endpoints for offline benchmarking:

```text
POST /v1/chat/completions
POST /v1/completions
```

These are not equally important for live use. `/v1/responses` is the live integration target; chat and
completions are useful for controlled text-only latency/quality comparisons.

## Session Model

Proposed session mapping:

| Robot concept | Agentic backend concept |
| --- | --- |
| One live run | Parent run/session namespace |
| One wave-opened conversation | One agent conversation session |
| Goodbye, idle timeout, or max duration close | End or reset conversation session |
| Scripted greet/goodbye policy speech | Stateless or short-lived request; should not pollute visitor memory |

Open decision: whether greet/goodbye fixed policy text should use the same agentic endpoint. For
preflight, keeping it through the S2S LLM slot is useful because it detects backend lag. For memory,
fixed policy speech should not create long-term visitor context unless explicitly tagged.

## Clinic Context Handling

Baseline direct API:

- Send the same clinic receptionist instructions as system/developer context every request.
- Optionally include local conversation history in the request for fair comparison.

Agentic wrapper:

- Load `profiles/clinic_receptionist/instructions.txt` as the default receptionist profile.
- Initialize the agent session with the profile when the conversation starts.
- Let the agent runtime own short-term message history inside that session.
- Log a profile hash/version with each request so a live run can prove which instructions were used.

Do not rely on model self-report to prove context. Artifacts should record:

- wrapper/session id
- profile path and content hash
- model/provider name
- request start/end timestamps
- user input text
- assistant output text
- whether memory/context was used
- error/fallback fields

## Benchmark Matrix

Use text-only tests first. No robot, no STT, no TTS.

Input:

- 1-2 scripted clinic conversation scenes.
- 20-30 total user turns.
- Include memory probes, clinic fact questions, language/tone edge cases, and interruption-like follow
  ups.

Compare:

| Label | API path | History/context mode | Why |
| --- | --- | --- | --- |
| `raw_responses_stateless` | OpenRouter `/v1/responses` | system/profile each turn only | Current simplest baseline |
| `raw_responses_with_history` | OpenRouter `/v1/responses` | system/profile + local message history | Fair direct-API baseline |
| `raw_chat_with_history` | OpenRouter `/v1/chat/completions` | message list | Redundant but useful sanity check |
| `raw_completions_prompted` | OpenRouter `/v1/completions` | prompt-assembled context | Latency/compatibility sanity check |
| `agentic_responses_session` | local wrapper `/v1/responses` | agent-owned session memory | Main candidate |
| `agentic_chat_with_history` | local wrapper `/v1/chat/completions` | message list or wrapper session | Compatibility check |

Measure:

- first token / first text latency where available
- total text latency
- response length
- instruction following
- clinic fact accuracy
- memory correctness
- whether output is concise and speakable
- failure/error rate

Expected result:

- Direct API should be faster.
- Agentic API may add latency.
- Agentic API is only worth adopting if it clearly improves context/memory/tool-readiness enough to
  justify that latency.

## Integration Plan

### Phase 1 — Text-Only Benchmark

Build or update the benchmark harness under `experiments/agentic_api/`.

Done when:

- Same scripted turns run against raw direct API and agentic API.
- Results include per-turn latency and output text.
- The output table makes latency/quality tradeoffs visible.

### Phase 2 — Local Wrapper Prototype

Run a local agentic service that exposes `/v1/responses`.

Done when:

- `curl` or the benchmark harness can call the wrapper without the robot.
- Wrapper logs session id, profile hash, provider/model, timings, and errors.
- It can run in stateless and conversation-session modes.

### Phase 3 — S2S Backend Wiring

Point the local S2S backend LLM slot at the wrapper instead of OpenRouter.

Expected shape:

```text
S2S_PROVIDER=openrouter-like
responses_api_base_url=http://127.0.0.1:<port>/v1
model_name=<wrapper-routed-model>
```

Exact flags depend on the installed `speech-to-speech` backend, so verify against
`scripts/m1max/run_s2s_backend.sh` and the backend help before changing live ops.

Done when:

- Scripted policy preflight works through the wrapper.
- Artifacts show which wrapper session/profile/model was used.
- Direct OpenRouter fallback remains one command/config change away.

### Phase 4 — Live A/B

Only after text-only benchmark and preflight pass:

- Run direct OpenRouter baseline.
- Run agentic wrapper.
- Use the same preflight/live-test discipline as current ops.
- Evaluate latency and receptionist quality from artifacts plus physical feedback.

## Risks

- **Latency:** agentic session/memory may add seconds. Measure before live adoption.
- **Memory drift:** persistent context can make the robot answer stale visitor state. Start with
  per-conversation memory, not cross-session memory.
- **Prompt opacity:** if wrapper hides the actual prompt/context, diagnosis gets worse. Require
  profile hash and memory/context metadata.
- **Over-answering:** agentic harnesses may produce verbose responses. Add style constraints and score
  response length.
- **Failure fallback:** live runs need a simple way to return to direct OpenRouter if the wrapper wedges.
- **Privacy:** do not store long-term visitor memory until we decide retention/safety rules.

## LiveKit Status

LiveKit is not part of the current implementation direction.

Why:

- It replaces or wraps realtime voice mechanics, while the current voice path is now good enough for
  basic live tests.
- It introduces another media/session layer before we have exhausted the simpler LLM-slot improvement.
- The first replay comparison underperformed with the then-current model choices.

Keep the archived LiveKit plan for future reference:

- `docs/archive/research/plan-livekit-backend.md`

Revisit LiveKit only if we decide to replace the entire voice backend after the agentic LLM-slot work
and m1max S2S path fail to meet product needs.

## Sources / References

- Hermes Agent GitHub README:
  https://github.com/nousresearch/hermes-agent/blob/main/README.md
- Hermes architecture:
  https://hermes-agent.nousresearch.com/docs/developer-guide/architecture
- Hermes programmatic integration:
  https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration
- Hermes agent loop internals:
  https://hermes-agent.nousresearch.com/docs/developer-guide/agent-loop
- Hermes adding tools:
  https://hermes-agent.nousresearch.com/docs/developer-guide/adding-tools
- Archived LiveKit backend plan:
  `docs/archive/research/plan-livekit-backend.md`
