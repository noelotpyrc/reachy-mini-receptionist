# Agentic API Exploration

This folder is isolated exploration work for using an agentic LLM API behind the accepted
official-runtime + local S2S backend path.

The current production direction remains:

```text
Reachy Mini official-runtime live app
  -> m1max local S2S backend
  -> local STT: parakeet-tdt
  -> remote LLM: direct responses-api via OpenRouter
  -> local TTS: qwen3 / Sohee
```

This exploration asks whether the LLM step should become:

```text
local S2S backend
  -> local agentic API wrapper
  -> Hermes Agent API server
  -> remote LLM provider
```

## Research Notes

Hermes Agent already exposes the shape we want for the first experiment:

- It has an OpenAI-compatible HTTP API server.
- It supports `POST /v1/responses`.
- Its Responses endpoint supports server-side state through `previous_response_id`.
- It also supports named conversations with a `conversation` parameter.
- It can be called by generic OpenAI-compatible clients at `http://localhost:8642/v1`.
- Its core `AIAgent` owns prompt assembly, provider selection, tool dispatch, retries, fallback,
  compression, session persistence, and memory flushing.

Sources:

- Hermes GitHub README: https://github.com/NousResearch/hermes-agent
- Programmatic integration: https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration
- API server: https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server
- Agent loop internals: https://hermes-agent.nousresearch.com/docs/developer-guide/agent-loop
- Provider runtime resolution: https://hermes-agent.nousresearch.com/docs/developer-guide/provider-runtime

## Integration Hypothesis

Do not put Hermes in the audio path. Keep STT and TTS local on m1max. Use Hermes as the text brain
behind the existing local S2S backend's LLM slot.

First useful wrapper target:

```text
POST /v1/responses-compatible request
  model: hermes-agent
  input: transcript text
  instructions: clinic receptionist profile
  conversation: reachy-session-<run_id>

Hermes API server
  loads profile / memory / context
  calls configured remote model provider
  returns final assistant text in Responses format
```

This can be compared directly with the current bare Responses API path:

```text
POST https://openrouter.ai/api/v1/responses
  model: openai/gpt-5.4-mini
  input: transcript text
  instructions: clinic receptionist profile
```

## What We Need To Learn

Primary comparison:

- Hermes wrapper total text-response latency vs raw Responses API total text-response latency.
- Whether Hermes adds too much overhead for wave-chat UX.
- Whether Hermes improves context retention and clinic-fact behavior enough to justify that overhead.

Secondary comparison:

- Can Hermes keep conversation state with `conversation` or `previous_response_id` without us managing
  the full transcript?
- Can we log which model/provider/profile/memory context was used on every turn?
- Can we disable risky tools and keep only memory/context behavior for a clinic receptionist?

## Candidate Setup On m1max

Status, 2026-06-22: Hermes is installed on m1max under `/Users/leon/.hermes`.

Dedicated profile:

- Profile: `reachyclinic`
- Profile path: `/Users/leon/.hermes/profiles/reachyclinic`
- API URL: `http://127.0.0.1:8642/v1`
- API key location: `/Users/leon/.hermes/profiles/reachyclinic/.env`
- API server model name: `reachyclinic`
- LLM provider: OpenRouter
- LLM model: `openai/gpt-5.4-mini`
- Profile skills: none seeded for the first latency pass

Hermes API server mode, per current docs:

```bash
# In Hermes profile env, not this repo .env:
API_SERVER_ENABLED=true
API_SERVER_KEY=<local-dev-token>
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642

reachyclinic gateway run --accept-hooks --replace
```

Then the wrapper benchmark can call:

```bash
export HERMES_API_KEY=<local-dev-token>
export HERMES_BASE_URL=http://127.0.0.1:8642/v1
export HERMES_MODEL=reachyclinic

python experiments/agentic_api/benchmark_responses_latency.py \
  --target hermes_conversation \
  --scenario experiments/agentic_api/scenarios/clinic_smoke.json \
  --runs 3
```

Initial m1max smoke result on 2026-06-22:

- `GET /health`: ok
- `GET /v1/models`: advertised `reachyclinic`
- direct `POST /v1/responses`: completed
- benchmark harness one-turn `hermes_stateless`: 3.524s
- benchmark harness one-turn `hermes_chat`: 5.195s

Raw baseline:

```bash
export OPENROUTER_API_KEY=<key>
export RAW_RESPONSES_BASE_URL=https://openrouter.ai/api/v1
export RAW_RESPONSES_MODEL=openai/gpt-5.4-mini

python experiments/agentic_api/benchmark_responses_latency.py \
  --target raw \
  --scenario experiments/agentic_api/scenarios/clinic_smoke.json \
  --runs 3
```

## Initial Text-Only Eval Modes

The init benchmark should not run the full S2S backend. It is text-only so we isolate LLM/API latency
and response quality.

Run all modes:

```bash
python experiments/agentic_api/benchmark_responses_latency.py \
  --target all \
  --scenario experiments/agentic_api/scenarios/clinic_reception_26_turns.json \
  --runs 1 \
  --output experiments/agentic_api/results/clinic_reception_26_turns.jsonl
```

Modes:

| Target | Endpoint | State | Expected use |
| --- | --- | --- | --- |
| `raw_cold` | OpenRouter `/v1/responses` | no history | Current-turn-only bare LLM baseline. |
| `raw_history` | OpenRouter `/v1/responses` | harness-managed transcript | Practical bare LLM baseline with our backend owning short-term history. |
| `hermes_responses_cold` | Hermes `/v1/responses` | no history | Hermes wrapper overhead without memory. |
| `hermes_responses_history` | Hermes `/v1/responses` | harness-managed transcript | Hermes wrapper overhead when our backend owns history. |
| `hermes_conversation` | Hermes `/v1/responses` | Hermes named `conversation` | Hermes server-side state. First turn sends `instructions`; later turns send only `conversation` and current input. |
| `hermes_chat_cold` | Hermes `/v1/chat/completions` | no history | Redundant but useful Chat Completions sanity comparison. |
| `hermes_chat_history` | Hermes `/v1/chat/completions` | harness-managed transcript | Chat Completions with explicit transcript history. |

Compatibility aliases: `raw` means `raw_cold`; `hermes` means `hermes_conversation`;
`hermes_stateless` means `hermes_responses_cold`; `hermes_chat` means `hermes_chat_cold`.

Implementation notes:

- Raw OpenRouter Responses requests must use `store=false`; OpenRouter rejects `store=true`.
- Hermes Responses requests keep `store=true` so named `conversation` can chain through stored
  response state.

## First Benchmark Result

Scenario: `scenarios/clinic_reception_26_turns.json`

Run date: 2026-06-22

Result files:

- `results/clinic_reception_26_turns_20260622-111138.jsonl`
- `results/clinic_reception_26_turns_raw_fix_20260622-112051.jsonl`

The first full run used `store=true` for raw OpenRouter and raw returned HTTP 400 for every raw row.
The raw fix reran only `raw_cold` and `raw_history` with `store=false`; those fixed rows are the raw
numbers below.

| Target | Successful rows | p50 | Mean | Min | Max | Mean request chars | Mean output chars |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `raw_cold` | 26 | 0.826s | 0.963s | 0.672s | 2.150s | 1211.6 | 100.8 |
| `raw_history` | 26 | 0.913s | 1.020s | 0.659s | 2.422s | 2405.5 | 69.5 |
| `hermes_responses_cold` | 26 | 3.333s | 3.617s | 1.962s | 12.444s | 1203.6 | 93.4 |
| `hermes_responses_history` | 26 | 3.055s | 3.445s | 1.859s | 7.317s | 2509.2 | 85.8 |
| `hermes_conversation` | 26 | 3.453s | 3.753s | 2.276s | 7.669s | 316.2 | 84.7 |
| `hermes_chat_cold` | 26 | 3.251s | 3.428s | 2.260s | 6.252s | 1254.6 | 107.2 |
| `hermes_chat_history` | 26 | 3.030s | 3.164s | 0.334s | 6.179s | 2526.7 | 86.8 |

## Acceptance Threshold For This Experiment

Hermes is worth integrating into the S2S backend only if:

- It can expose a stable Responses-compatible surface for the backend.
- It keeps p50 text-response latency close enough that robot response lag is still acceptable.
- It gives us materially better clinic context, memory, or tool policy than a bare prompt.
- It can be configured with restricted tools suitable for a public clinic receptionist.

If Hermes is slower but behaviorally useful, keep it as an optional context/memory service for selected
turns rather than the default path for every utterance.

## Next Steps

1. Configure a restricted receptionist profile:
   - remote LLM provider key/model, likely OpenRouter first for apples-to-apples comparison;
   - API server enabled on `127.0.0.1:8642`;
   - API key stored in Hermes profile env, not committed here;
   - risky tools disabled for the first latency pass.
2. Start `reachyclinic gateway run --accept-hooks --replace` and confirm:
   - `GET http://127.0.0.1:8642/health`
   - `GET http://127.0.0.1:8642/v1/models`
   - one `POST /v1/responses` clinic smoke request.
3. Run this folder's benchmark against both targets:
   - `--target raw` for direct OpenRouter Responses API;
   - `--target hermes_stateless` for Hermes Responses API without server-side conversation state;
   - `--target hermes_conversation` for Hermes Responses API with named conversation state;
   - `--target hermes_chat` for Hermes Chat Completions API;
   - `--target all` for the full init comparison.
4. Save JSONL results under `experiments/agentic_api/results/` and compare p50/mean latency, response
   relevance, and whether Hermes state/memory changes the UX enough to justify any overhead.
