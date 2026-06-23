"""Step-a standalone test of the REAL daemon backend: reachy_mini_brain.brain.PydanticBrain.

Imports the actual package class (not the bench copy), runs a receptionist conversation, and
checks memory + that reset() starts a fresh conversation. No robot, no main-venv changes — run
with the experiment venv (has pydantic-ai); OPENROUTER_API_KEY is read from the project .env.

  experiments/brain_bench/.venv/bin/python experiments/brain_bench/test_pydantic_brain.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from reachy_mini_brain.brain import PydanticBrain  # noqa: E402

CONVO = [
    "Hi, I'm here for a 3 o'clock with Dr. Park.",
    "Thanks. Where are the restrooms?",
    "Got it - what's the guest wifi?",
    "Sorry, remind me - who did I say I'm here to see today?",  # memory probe
]

b = PydanticBrain()
print(f"backend: PydanticBrain | model={b.model}")
t0 = time.perf_counter(); b.prewarm(); print(f"prewarm: {time.perf_counter()-t0:.2f}s")

last, times = "", []
for i, u in enumerate(CONVO, 1):
    t = time.perf_counter(); last = b.respond(u); dt = time.perf_counter() - t
    times.append(dt)
    print(f"turn {i} ({dt:4.1f}s)  V: {u}")
    print(f"              G: {last}")
print(f"-- avg {sum(times)/len(times):.1f}s/turn | mem-probe recalled 'Park': "
      f"{'YES' if 'park' in last.lower() else 'NO'}")

# reset() => new visitor; the brain must NOT recall the previous conversation.
b.reset()
t = time.perf_counter(); fresh = b.respond("Who did I just say I'm here to see?")
print(f"\nafter reset() ({time.perf_counter()-t:4.1f}s)  G: {fresh}")
print(f"-- correctly forgot after reset: {'YES' if 'park' not in fresh.lower() else 'NO'}")
