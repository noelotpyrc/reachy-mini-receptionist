"""Test 3 reasoning-capable models with reasoning ON vs OFF (OpenRouter `reasoning.enabled`).

Reasoning/thinking models were slow in the main sweep (8-13s). This checks whether disabling
reasoning makes them fast enough for the voice brain — same receptionist convo + memory + quality.

  experiments/brain_bench/.venv/bin/python experiments/brain_bench/reasoning_sweep.py
"""
import sys
import time
import statistics as st
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from reachy_mini_brain.brain import PERSONA, _FACTS_PATH, ReceptionBrain, _openrouter_key  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings  # noqa: E402
from pydantic_ai.providers.openrouter import OpenRouterProvider  # noqa: E402

PERSONA_FULL = ReceptionBrain._with_facts(PERSONA, _FACTS_PATH)
KEY = _openrouter_key()
MODELS = sys.argv[1:] or ["openai/gpt-oss-20b", "qwen/qwen3.5-35b-a3b", "qwen/qwen3-32b"]
CONVO = [
    "Hi, I'm here for a 3 o'clock with Dr. Park.",
    "Thanks. Where are the restrooms?",
    "Got it - what's the guest wifi?",
    "Sorry, remind me - who did I say I'm here to see today?",
]
SAMPLE = "Hi, I'm here to see Dr. Park."


def run_mode(slug: str, reasoning_off: bool) -> dict:
    agent = Agent(OpenAIChatModel(slug, provider=OpenRouterProvider(api_key=KEY)),
                  instructions=PERSONA_FULL)
    body = {"reasoning": {"enabled": False}} if reasoning_off else None
    settings = OpenAIChatModelSettings(timeout=60.0, extra_body=body) if body else \
        OpenAIChatModelSettings(timeout=60.0)
    hist, times, replies = [], [], []
    for u in CONVO:
        t = time.perf_counter()
        r = agent.run_sync(u, message_history=hist, model_settings=settings)
        times.append(time.perf_counter() - t)
        hist = r.all_messages(); replies.append((r.output or "").strip())
    for _ in range(2):
        t = time.perf_counter(); agent.run_sync(SAMPLE, model_settings=settings)
        times.append(time.perf_counter() - t)
    return dict(median=st.median(times), lo=min(times), hi=max(times),
                mem="park" in replies[-1].lower(), t1=replies[0])


for slug in MODELS:
    print(f"\n{'='*72}\n### {slug}\n{'='*72}")
    for off in (False, True):
        label = "reasoning OFF" if off else "reasoning ON "
        try:
            r = run_mode(slug, off)
            print(f"  [{label}]  median {r['median']:5.1f}s | range {r['lo']:4.1f}-{r['hi']:4.1f}s "
                  f"| mem {'Y' if r['mem'] else 'N'}")
            print(f"               turn1: {r['t1'][:120]}")
        except Exception as e:  # noqa: BLE001
            print(f"  [{label}]  FAILED: {e.__class__.__name__}: {str(e)[:140]}")
