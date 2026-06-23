"""Brain-backend benchmark — DSPy vs Pydantic AI vs Agno, all over OpenRouter.

Goal: a headless, in-process, multi-provider conversation brain to replace the `claude -p`
stopgap. This validates each harness does clean multi-turn receptionist chat with memory, and
times per-turn latency. Model choice is deferred — one placeholder model for apples-to-apples.

Run:  experiments/brain_bench/.venv/bin/python experiments/brain_bench/bench.py
Model override:  BENCH_MODEL="anthropic/claude-haiku-4.5" .../python bench.py
"""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
KEY = os.environ["OPENROUTER_API_KEY"]
os.environ.setdefault("OPENROUTER_API_KEY", KEY)  # so env-reading SDKs (Agno) pick it up
MODEL = os.environ.get("BENCH_MODEL", "google/gemini-2.5-flash")  # OpenRouter slug

PERSONA = """You are "Genie", the warm, concise front-desk receptionist at Lakeside Family Clinic.
Stay in character at all times. Reply ONLY with what Genie says out loud - 1-2 short, natural spoken
sentences. Never mention being an AI, tools, or files.

Clinic facts (the truth - never invent beyond these):
- Hours: Mon-Fri 9:00am-5:00pm; closed weekends.
- Address: 200 Lakeside Drive, 2nd floor; elevator by the main entrance.
- Parking: free lot behind the building.
- Restrooms: down the hall, past the waiting area, on the right.
- Guest Wi-Fi: network "Lakeside-Guest", no password.
- Departments: family medicine, pediatrics, lab/bloodwork (lab is room 210).
- Providers: Dr. Lee (family medicine), Dr. Park (pediatrics), Dr. Gomez (family medicine).
- The front desk CANNOT look up individual appointment details yet - take the visitor's name and
  say a staff member will confirm shortly.
- Pharmacy: Lakeside Pharmacy, ground floor. Emergencies: tell them to call 911."""

# 4-turn script; turn 4 is the memory probe (must recall "Dr. Park" said in turn 1, plus parking).
CONVO = [
    "Hi, I'm here for a 3 o'clock with Dr. Park.",
    "Thanks. Where are the restrooms?",
    "Got it - what's the guest wifi?",
    "Sorry, remind me - who did I say I'm here to see today?",  # pure memory probe
]


# --- backends: each exposes respond(utterance) -> str with in-process memory ---

class PydanticBrain:
    name = "Pydantic AI"

    def __init__(self):
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openrouter import OpenRouterProvider
        model = OpenAIChatModel(MODEL, provider=OpenRouterProvider(api_key=KEY))
        self.agent = Agent(model, instructions=PERSONA)
        self.history = []

    def respond(self, utt: str) -> str:
        r = self.agent.run_sync(utt, message_history=self.history)
        self.history = r.all_messages()
        return r.output


class AgnoBrain:
    name = "Agno"

    def __init__(self):
        from agno.agent import Agent
        from agno.models.openrouter import OpenRouter
        from agno.db.in_memory import InMemoryDb
        self.agent = Agent(
            model=OpenRouter(id=MODEL),
            instructions=PERSONA,
            db=InMemoryDb(),            # required for history retention
            session_id="bench",
            add_history_to_context=True,
            num_history_runs=10,
            markdown=False,
        )

    def respond(self, utt: str) -> str:
        return self.agent.run(utt).content.strip()


class DspyBrain:
    name = "DSPy"

    def __init__(self):
        import dspy
        lm = dspy.LM(f"openrouter/{MODEL}", api_base="https://openrouter.ai/api/v1",
                     api_key=KEY, model_type="chat", max_tokens=400, cache=False)
        dspy.configure(lm=lm)

        class Receptionist(dspy.Signature):
            history: dspy.History = dspy.InputField()
            utterance: str = dspy.InputField()
            reply: str = dspy.OutputField(desc="what Genie says out loud, 1-2 short sentences")

        Receptionist.__doc__ = PERSONA
        self.predict = dspy.Predict(Receptionist)
        self.history = dspy.History(messages=[])

    def respond(self, utt: str) -> str:
        out = self.predict(history=self.history, utterance=utt)
        self.history.messages.append({"utterance": utt, "reply": out.reply})
        return out.reply


def run(backend_cls) -> None:
    print(f"\n{'='*70}\n### {backend_cls.name}  (model={MODEL})\n{'='*70}")
    try:
        t0 = time.perf_counter()
        brain = backend_cls()
        init_dt = time.perf_counter() - t0
    except Exception:
        print(f"  INIT FAILED:\n{traceback.format_exc()}")
        return
    print(f"  init: {init_dt:.2f}s")
    times, last = [], ""
    for i, utt in enumerate(CONVO, 1):
        try:
            t = time.perf_counter()
            reply = brain.respond(utt)
            dt = time.perf_counter() - t
        except Exception:
            print(f"  turn {i} FAILED:\n{traceback.format_exc()}")
            return
        times.append(dt)
        last = reply
        print(f"  turn {i} ({dt:4.1f}s)  V: {utt}")
        print(f"               G: {reply}")
    recalled = "park" in last.lower()
    print(f"  -- total {sum(times):.1f}s | avg {sum(times)/len(times):.1f}s/turn "
          f"| mem-probe recalled 'Park': {'YES' if recalled else 'NO'}")


if __name__ == "__main__":
    print(f"Brain-backend benchmark | model={MODEL} | {len(CONVO)} turns")
    for cls in (DspyBrain, PydanticBrain, AgnoBrain):
        run(cls)
