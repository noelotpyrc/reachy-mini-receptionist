# Brain backend research — replacing `claude -p`

`claude -p` is a **first-pass stopgap** for the conversation brain, not the target. Its problems:
single-provider (Anthropic only), needs the GUI-rooted tmux keychain-auth hack to run on m1max, no
clean way to compare other models, and per-turn cost is the model call. Goal: a **headless,
lightweight, multi-provider** conversation layer with **memory** so we can swap model APIs, benchmark,
and decide on real numbers.

> See also `live-test-log.md` 2026-06-08 — `agy` (Google "Antigravity" CLI) + Gemini Flash was
> evaluated and **REJECTED**: agentic CLI that derails on *any* flag, ~3–5s/turn from per-call CLI
> spawn (no persistent process). The lesson below (prefer an **in-process library**, not a CLI) comes
> straight from that.

## Requirements
- **Headless** — driven programmatically, no TTY (the agy/claude-p pain point).
- **Multi-provider** — swap Gemini / Claude / GPT / Groq / local with ~a string, to compare.
- **In-process / low-latency** — persistent, NOT a per-turn CLI spawn (agy's killer, ~3–5s).
- **Memory** — multi-turn conversation state (non-negotiable).
- **Tool-capable but not tool-*compelled*** — chat by default; tools only when we add them (agy's flaw).

## Two layers

### A) Model-access layer (to compare APIs)
- **OpenRouter** — one key → 500+ models (Gemini 3 Flash, Claude Opus 4.7 / Haiku 4.5, GPT-5, DeepSeek,
  Groq-hosted Llama …), normalized streaming, auto-fallback. ~0.40s gateway first-token overhead.
  **Best on-ramp for "try diff model API to compare"** — one integration, every model.
- **LiteLLM** — SDK/proxy, 100+ providers in OpenAI format. Can go **direct-to-provider** (lower
  latency than the OpenRouter hop) or sit under a harness. Use to drop the gateway once a model is chosen.

### B) Agent harness (headless, lightweight, in-process)
| Framework | Weight | Providers | Memory | Notes |
|---|---|---|---|---|
| **Pydantic AI** | light, typed | ~all (incl. OpenRouter, LiteLLM, Ollama) | `message_history` (we manage; serialize/reload) | minimal abstraction, transparent, easy to debug. **Top pick for a controllable brain.** |
| **Agno** (ex-Phidata) | lightest (≈2µs create, 50× less mem) | 23+ | **built-in** short/long-term + sessions | plug-and-play; built-in memory = less code. **Top pick if we want memory handled for us.** |
| smolagents (HF) | minimal (~1k LOC) | via LiteLLM/transformers | — | **code-agent** (writes/exec Python) → security risk for a public robot; wrong shape for pure chat. |
| Goose (AAIF / Linux Fdn, 44k★) | heavier (CLI+desktop+API) | 15+ (OpenRouter, Ollama …) | agent state | OSS analog to claude-p/agy; **headless CLI** + embeddable API + MCP. Agentic by design (same tool-compelled risk); CLI path risks spawn latency. |
| Letta (MemGPT) | heavy (runs a server) | via LiteLLM | **persistent memory (its specialty)** | overkill unless long-term memory is THE feature. |

## Latency notes (2026 benchmarks)
- Fastest first-token: **Claude Haiku 4.5 ~597ms**; **Groq / SambaNova ~0.13s**.
- Fastest throughput: **Gemini Flash ~146–173 tok/s**.
- OpenRouter gateway adds ~0.40s first-token vs direct provider.
- Takeaway: an **in-process library + direct-or-OpenRouter API** beats agy's ~3–5s easily, and gives
  us the model-swap axis for free.

## Recommendation
Try **Pydantic AI** and **Agno**, both over **OpenRouter** (one key, many models), benchmarked with our
receptionist roleplay (persona + `clinic_facts.md` + multi-turn) across **Gemini 3 Flash, Claude Haiku
4.5, a Groq Llama, GPT-5-mini**. Measure **per-turn latency + quality + memory**. Pick framework +
model from real numbers — the same method that rejected agy. This fixes both agy failures
(**in-process = no spawn lag**, **trivial model comparison**) and isn't tool-compelled.

## Next step
Build a small benchmark harness (one script, N models via OpenRouter, the receptionist roleplay +
timing). Needs an **OpenRouter key** (or direct provider keys). Then run → compare → decide the backend
and wire it as a selectable `--brain-backend` (claude stays as fallback until the new one is proven).

## Benchmark results (2026-06-09) — all three viable, all fast

Setup: isolated venv (`experiments/brain_bench/`), `bench.py`, model `google/gemini-2.5-flash` via
OpenRouter (placeholder — model comparison deferred), 4-turn receptionist script with a memory-probe
turn (recall "Dr. Park" from turn 1).

| Framework | init | avg/turn | total | memory | notes |
|---|---|---|---|---|---|
| **DSPy** 3.2.1 | 0.94s | **0.6s** | 2.6s | ✅ `dspy.History` | signature-based; slower init (compile); enables prompt *optimization* on data later |
| **Pydantic AI** 1.106 | 0.34s | **0.6s** | 2.5s | ✅ `message_history` (we own it) | cleanest/typed/minimal — easiest to drop into `brain.py` |
| **Agno** 2.6.12 | 0.13s | 0.7s | 2.7s | ✅ (needs `db=InMemoryDb()`) | lightest init; built-in sessions/memory/teams platform |

All three: clean, in-character, fact-grounded (restrooms/wifi verbatim), **no agentic derailment**
(unlike agy), memory correct. **~0.6–0.7s/turn — ≈5–8× faster than agy (3–5s), ≈2–3× faster than
`claude -p` (1–2s).** Gotcha: **Agno silently drops history without a db** (warns + forgets);
`db=InMemoryDb()` fixes it.

### Decision (2026-06-09)
All three viable — this approach is the right replacement for the `claude -p`/agy stopgaps.
- **DSPy = long-term target** — for prompt/persona *optimization* on the conversations we log.
- **Pydantic AI = next iteration** — simpler start (typed, we own history, easiest daemon wiring).
- **Keep both**; Agno parked (works, but not the chosen path).
- **Dev model = `openai/gpt-oss-20b`** (cheapest at $0.03/M, ~0.9s median — but spiky, p90 2.3s; fine
  for iteration). Set as `_DEFAULT_OR_MODEL` in `brain.py`, overridable via `REACHY_BRAIN_MODEL`.
  Final model still TBD (see sweep below; nova-lite/mistral-small fastest, haiku warmest).

## Model sweep (2026-06-09) — Pydantic AI / OpenRouter

Real `PydanticBrain` per model: 4-turn receptionist convo + single-turn samples (~6–8 latency points).
Memory = recall "Dr. Park" — **passed on ALL** (it's our embedded transcript, model-agnostic). Latency
is API-bound + spiky; **medians are the signal, single spikes are noise.** One snapshot.

### Round 1 — 10 cheap models (default settings), by median latency
| model | $/M in | median | range | note |
|---|---|---|---|---|
| gemini-2.5-flash-lite | 0.10 | 0.5s | 0.4–8.2 | fast, one spike |
| mistral-small-3.2-24b | 0.07 | 0.6s | 0.4–1.6 | tight; terse |
| amazon/nova-lite-v1 | 0.06 | 0.6s | 0.5–0.8 | tightest; terse |
| claude-haiku-4.5 | 1.00 | 1.2s | 0.8–1.3 | tight; **warmest/best replies** |
| llama-3.3-70b | 0.10 | 1.9s | 0.7–14.8 | variable |
| gemini-2.5-flash (old baseline) | 0.30 | 2.8s | 1.6–21.7 | middling + spiky |
| glm-4.7-flash | 0.06 | 5.0s | 1.7–8.7 | slow-ish |
| gpt-5-nano | 0.05 | 8.5s | 4.8–21.7 | reasoning → slow |
| gpt-5-mini | 0.25 | 11.4s | 3.2–17.1 | reasoning → slow |
| qwen3.5-flash | 0.07 | 12.8s | 7.7–25.5 | thinking → slow |

### Round 2 — reasoning ON vs OFF (`reasoning:{enabled:false}` via `extra_body`)
| model | $/M | ON median | OFF median | note |
|---|---|---|---|---|
| qwen3.5-35b-a3b | 0.14 | 31.0s | **0.9s** | disabling thinking ≈ 34× faster; clean |
| qwen3-32b | 0.08 | 6.6s | 4.3s | off helps but still slow; minor quality slip |
| gpt-oss-20b | 0.03 | 1.8s | — | "reasoning mandatory, cannot disable" (400) — but default is light → already fast |
| deepseek-v4-flash | 0.10 | 1.3s | 1.3s (+11s spike) | "flash" reasoning already light → leave ON; warm + 1M ctx |

### Consolidated viable shortlist (fast + consistent + good quality, all sub-2s)
gemini-2.5-flash-lite (0.5s), nova-lite-v1 (0.6s), mistral-small-3.2-24b (0.6s),
qwen3.5-35b-a3b + reasoning-off (0.9s), claude-haiku-4.5 (1.2s, warmest), deepseek-v4-flash (1.3s,
warm + cheap + 1M ctx), gpt-oss-20b (1.8s, cheapest).
**Out:** gpt-5-mini/nano + qwen3.5-flash (reasoning, 8–13s), qwen3-32b (slow even off), glm-4.7-flash.
**Backend note:** a chosen Qwen reasoning model REQUIRES `reasoning:{enabled:false}` (extra_body), else ~31s.

## Sources
- [Langfuse — Comparing Open-Source AI Agent Frameworks](https://langfuse.com/blog/2025-03-19-ai-agent-comparison)
- [Agno — GitHub](https://github.com/agno-agi/agno) · [docs](https://docs.agno.com/)
- [Pydantic AI — models/providers](https://ai.pydantic.dev/models/) · [message history](https://pydantic.dev/docs/ai/core-concepts/message-history/) · [GitHub](https://github.com/pydantic/pydantic-ai)
- [LiteLLM — GitHub](https://github.com/BerriAI/litellm/) · [providers](https://docs.litellm.ai/docs/providers)
- [Goose — GitHub](https://github.com/aaif-goose/goose) · [docs](https://goose-docs.ai/)
- [OpenRouter](https://openrouter.ai/) · [models](https://www.llmreference.com/provider/openrouter/models)
- [LLM API latency benchmarks 2026](https://www.kunalganglani.com/blog/llm-api-latency-benchmarks-2026)
