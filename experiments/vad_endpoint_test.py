"""Standalone VAD-endpointer test on the real robot (run with the daemon stopped).

Talk in sentences near the robot. Each COMPLETE utterance (speech..silence) should print as
one clean transcript line — not a 1.5s fragment or a long multi-speaker blob.

  REACHY_HOST=192.168.1.165 .venv/bin/python experiments/vad_endpoint_test.py [seconds]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reachy_mini_brain.session import Session  # noqa: E402

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 45

print("starting session...", flush=True)
s = Session(); s.start(); time.sleep(1.0)
print("listen_start:", s.listen_start(), "(model=medium, Silero-VAD endpointer)", flush=True)
print(f">>> TALK NOW in sentences for ~{DURATION}s. Each utterance prints as one line.", flush=True)

t_end = time.monotonic() + DURATION
n = 0
while time.monotonic() < t_end:
    t0 = time.monotonic()
    res = s.listen_read(timeout=1.0)
    txt = res.get("text", "")
    if txt:
        n += 1
        print(f"  utt {n}: [{res.get('buffer_duration')}s audio, {time.monotonic()-t0:.1f}s transcribe] {txt!r}",
              flush=True)

s.listen_stop()
try:
    s.stop()
except Exception:
    pass
print(f"done — {n} utterances captured", flush=True)
