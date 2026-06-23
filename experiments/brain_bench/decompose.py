"""Decompose where PydanticBrain's first-turn time goes: import vs build vs cold call vs warm."""
import time
from pathlib import Path
import sys

_t = time.perf_counter()
import pydantic_ai  # noqa: F401  -- first (cold) import of the whole LLM client stack
imp = time.perf_counter() - _t

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src")))
from reachy_mini_brain.brain import PydanticBrain  # noqa: E402

b = PydanticBrain()
print(f"import pydantic_ai (cold):   {imp:.2f}s")
print(f"system prompt (persona+facts): {len(b.persona)} chars")
_t = time.perf_counter(); b._build(); build = time.perf_counter() - _t
print(f"build agent:                 {build:.2f}s")
print(f"=> prewarm (import+build):   {imp + build:.2f}s")
print("--- respond() calls (reset between, so connection is the only thing that stays warm) ---")
for i in range(5):
    _t = time.perf_counter(); b.respond("Hi, I'm here to see Dr. Park."); dt = time.perf_counter() - _t
    print(f"call {i+1}: {dt:.2f}s {'<- cold HTTP connection + model' if i == 0 else '<- warm'}")
    b.reset()
