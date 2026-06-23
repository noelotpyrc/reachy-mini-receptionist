"""Show full replies + per-call latency distribution for two models (define 'warm' and 'spike')."""
import sys
import time
import statistics as st
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from reachy_mini_brain.brain import PydanticBrain  # noqa: E402

MODELS = ["google/gemini-2.5-flash-lite", "deepseek/deepseek-v4-flash"]
CONVO = [
    "Hi, I'm here for a 3 o'clock with Dr. Park.",
    "Thanks. Where are the restrooms?",
    "Got it - what's the guest wifi?",
    "Sorry, remind me - who did I say I'm here to see today?",
]
SAMPLE = "Hi, I'm here to see Dr. Park."

for slug in MODELS:
    print(f"\n{'='*72}\n===== {slug} =====\n{'='*72}")
    b = PydanticBrain(model=slug); b.prewarm()
    print("--- 4-turn conversation (FULL replies) ---")
    for u in CONVO:
        t = time.perf_counter(); r = b.respond(u); dt = time.perf_counter() - t
        print(f"[{dt:4.1f}s] V: {u}")
        print(f"        G: {r}")
    print("--- 10 identical calls (latency only; reset between) — watch for one-off spikes ---")
    samp = []
    for i in range(10):
        b.reset(); t = time.perf_counter(); b.respond(SAMPLE); dt = time.perf_counter() - t
        samp.append(dt); print(f"  call {i+1:2d}: {dt:5.1f}s")
    print(f"  -> median {st.median(samp):.1f}s | min {min(samp):.1f}s | max {max(samp):.1f}s "
          f"(max-vs-median gap = the 'spike')")
