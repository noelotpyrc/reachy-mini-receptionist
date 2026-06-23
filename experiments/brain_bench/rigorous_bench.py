"""Authoritative latency benchmark — interleaved, high-N, with percentiles.

Fixes the earlier flaw (sequential blocks + tiny N → drift). Round-robin: each round calls every
model once, so all models sample the SAME time window. Warmup call discarded. Identical single-turn
prompt (memory/quality validated separately). Reports a distribution, not a lone number.

  experiments/brain_bench/.venv/bin/python experiments/brain_bench/rigorous_bench.py [ROUNDS]
"""
import sys
import time
import statistics as st
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from reachy_mini_brain.brain import PERSONA, _FACTS_PATH, ReceptionBrain, _openrouter_key  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings  # noqa: E402
from pydantic_ai.providers.openrouter import OpenRouterProvider  # noqa: E402

PERSONA_FULL = ReceptionBrain._with_facts(PERSONA, _FACTS_PATH)
KEY = _openrouter_key()
PROMPT = "Hi, I'm here for a 3 o'clock with Dr. Park."
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 20

# (slug, reasoning_off)
SPEC = [
    ("google/gemini-2.5-flash-lite", False),
    ("amazon/nova-lite-v1", False),
    ("mistralai/mistral-small-3.2-24b-instruct", False),
    ("qwen/qwen3.5-35b-a3b", True),          # reasoning disabled
    ("anthropic/claude-haiku-4.5", False),
    ("deepseek/deepseek-v4-flash", False),
    ("openai/gpt-oss-20b", False),
]


def make_settings(off):
    body = {"reasoning": {"enabled": False}} if off else None
    return OpenAIChatModelSettings(timeout=60.0, extra_body=body) if body \
        else OpenAIChatModelSettings(timeout=60.0)


def pct(xs, p):
    xs = sorted(xs); k = (len(xs) - 1) * p / 100.0; f = int(k)
    return xs[f] if f + 1 >= len(xs) else xs[f] + (xs[f + 1] - xs[f]) * (k - f)


agents = {}
for slug, off in SPEC:
    agents[slug] = (Agent(OpenAIChatModel(slug, provider=OpenRouterProvider(api_key=KEY)),
                          instructions=PERSONA_FULL), make_settings(off))

print(f"warmup (untimed) + {ROUNDS} interleaved rounds over {len(SPEC)} models...")
for slug, (a, s) in agents.items():
    try:
        a.run_sync(PROMPT, model_settings=s)
    except Exception as e:  # noqa: BLE001
        print(f"  warmup FAILED {slug}: {e.__class__.__name__}")

samples = {slug: [] for slug, _ in SPEC}
for r in range(ROUNDS):
    for slug, _ in SPEC:
        a, s = agents[slug]
        try:
            t = time.perf_counter(); a.run_sync(PROMPT, model_settings=s)
            samples[slug].append(time.perf_counter() - t)
        except Exception:  # noqa: BLE001
            pass
    print(f"  round {r + 1}/{ROUNDS} done", flush=True)

print(f"\n{'model':42s} {'N':>2s} {'med':>5s} {'p10':>5s} {'p90':>5s} {'min':>5s} {'max':>5s} {'mean':>5s}")
for slug, xs in sorted(samples.items(), key=lambda kv: st.median(kv[1]) if kv[1] else 9e9):
    if not xs:
        print(f"{slug:42s}  NO DATA"); continue
    print(f"{slug:42s} {len(xs):2d} {st.median(xs):5.1f} {pct(xs,10):5.1f} {pct(xs,90):5.1f} "
          f"{min(xs):5.1f} {max(xs):5.1f} {st.mean(xs):5.1f}")
