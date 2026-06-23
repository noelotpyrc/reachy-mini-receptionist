"""Streaming-speak test on the REAL robot: chunked render-ahead vs whole-utterance speak.

Run on m1max with the daemon stopped (it owns the session):
  .venv/bin/python experiments/stream_speak_test.py

Plays two versions of the same reply, prefixed by a spoken label so you can tell them apart:
  "Test one, current style."  -> speak(whole reply)            (today's behavior)
  "Test two, streamed style." -> continuous-push streamed reply (sentence render-ahead)
Listen for: does test two start sooner, and is it smooth (no gaps between sentences)?
"""
import sys, time, queue, threading, re
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reachy_mini_brain.session import Session  # noqa: E402

VOICE = "en_US-lessac-medium"
REPLY = ("Welcome to Lakeside Family Clinic. The restrooms are down the hall, past the "
         "waiting area, on the right. Is there anything else I can help you with today?")


def sents(t):
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", t) if x.strip()]


print("starting session...", flush=True)
s = Session(); s.start(); time.sleep(1.0)


def render(txt):
    return s._render_speech(txt, VOICE, cache=False)


def play_stream(chunk_q, t0, first_audio):
    """Continuous prime+pace push across a stream of rendered audio chunks: cushion at the
    START, pace the rest, drain only at the END — the streaming analog of _play_speech."""
    s._speaking = True
    try:
        CH, PRIME, pushed = 3200, 6400, 0
        buf = None
        done = False
        while not done:
            a = chunk_q.get()
            if a is None:
                done = True
            else:
                a = np.asarray(a)
                buf = a if buf is None else np.concatenate([buf, a])
            if buf is None:
                continue
            i = 0
            while i + CH <= len(buf):
                s.push_audio_sample(np.ascontiguousarray(buf[i:i + CH]))
                if first_audio[0] is None:
                    first_audio[0] = time.perf_counter() - t0
                i += CH; pushed += CH
                if pushed >= PRIME:
                    time.sleep(CH / 16000.0 * 0.95)
            buf = buf[i:]
        if buf is not None and len(buf):
            s.push_audio_sample(np.ascontiguousarray(buf))
        time.sleep(PRIME / 16000.0 + 0.5)
    finally:
        s._speaking = False


S = sents(REPLY)
print("reply = %d sentences" % len(S), flush=True)
t = time.perf_counter(); render(REPLY); whole = time.perf_counter() - t
t = time.perf_counter(); render(S[0]); first = time.perf_counter() - t
print("synth: whole=%.2fs  sentence1=%.2fs  => first audio ~%.2fs sooner" % (whole, first, whole - first), flush=True)

# A — baseline: speak the whole reply
s.speak("Test one, current style.", cache=False); time.sleep(0.4)
t = time.perf_counter(); s.speak(REPLY, cache=False)
print("BASELINE (speak whole): total %.2fs" % (time.perf_counter() - t), flush=True)
time.sleep(1.3)

# B — streamed: render sentences ahead, push continuously
s.speak("Test two, streamed style.", cache=False); time.sleep(0.4)
q = queue.Queue(); fa = [None]
def renderer():
    for c in S:
        q.put(render(c))
    q.put(None)
t0 = time.perf_counter()
rt = threading.Thread(target=renderer); rt.start()
play_stream(q, t0, fa)
rt.join()
print("STREAMED (chunked): first-audio %.2fs | total %.2fs" % (fa[0], time.perf_counter() - t0), flush=True)

try:
    s.stop()
except Exception:
    pass
print("done", flush=True)
