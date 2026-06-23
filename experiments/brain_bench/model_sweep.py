"""Model sweep — run the real PydanticBrain across a slate of cheap OpenRouter models.

Latency is variable (server/API-bound), so we take several samples per model and report
median + spread, plus a memory check and the replies for quality eyeballing.

  experiments/brain_bench/.venv/bin/python experiments/brain_bench/model_sweep.py
"""
import sys
import time
import statistics as st
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from reachy_mini_brain.brain import PydanticBrain  # noqa: E402

MODELS = [
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
    "anthropic/claude-haiku-4.5",
    "openai/gpt-5-mini",
    "openai/gpt-5-nano",
    "meta-llama/llama-3.3-70b-instruct",
    "mistralai/mistral-small-3.2-24b-instruct",
    "qwen/qwen3.5-flash-02-23",
    "z-ai/glm-4.7-flash",
    "amazon/nova-lite-v1",
]

CONVO = [
    "Hi, I'm here for a 3 o'clock with Dr. Park.",
    "Thanks. Where are the restrooms?",
    "Got it - what's the guest wifi?",
    "Sorry, remind me - who did I say I'm here to see today?",  # memory probe
]
SAMPLE = "Hi, I'm here to see Dr. Park."  # repeated for a controlled latency sample
N_SAMPLES = 4


def test_model(slug: str) -> dict:
    b = PydanticBrain(model=slug)
    b.prewarm()
    times, replies = [], []
    for u in CONVO:                      # 4-turn convo: quality + memory
        t = time.perf_counter(); r = b.respond(u); times.append(time.perf_counter() - t)
        replies.append(r)
    for _ in range(N_SAMPLES):           # controlled single-turn latency samples
        b.reset()
        t = time.perf_counter(); b.respond(SAMPLE); times.append(time.perf_counter() - t)
    return dict(slug=slug, median=st.median(times), lo=min(times), hi=max(times),
                convo=sum(times[:4]), mem="park" in replies[-1].lower(),
                t1=replies[0], recall=replies[-1])


results = []
for slug in MODELS:
    print(f"\n{'='*72}\n### {slug}\n{'='*72}")
    try:
        r = test_model(slug)
        results.append(r)
        print(f"  turn1:  {r['t1']}")
        print(f"  recall: {r['recall']}")
        print(f"  median {r['median']:.1f}s | range {r['lo']:.1f}-{r['hi']:.1f}s | "
              f"4-turn convo {r['convo']:.1f}s | memory {'YES' if r['mem'] else 'NO'}")
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED: {e.__class__.__name__}: {e}")
        traceback.print_exc()

print(f"\n\n{'='*72}\nSUMMARY (sorted by median latency)\n{'='*72}")
print(f"{'model':44s} {'median':>7s} {'range':>13s} {'mem':>4s}")
for r in sorted(results, key=lambda x: x['median']):
    print(f"{r['slug']:44s} {r['median']:6.1f}s {r['lo']:4.1f}-{r['hi']:4.1f}s "
          f"{'YES' if r['mem'] else 'NO':>4s}")
