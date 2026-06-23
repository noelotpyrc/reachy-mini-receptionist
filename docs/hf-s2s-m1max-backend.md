# Hugging Face Speech-to-Speech Backend on m1max

Date: 2026-06-15

## Purpose

Run the open-source Hugging Face `speech-to-speech` realtime backend on the m1max and connect to it through
the official app's Hugging Face realtime handler:

```text
official app / replay harness
  -> HF_REALTIME_CONNECTION_MODE=local
  -> HF_REALTIME_WS_URL=ws://100.127.86.67:8765/v1/realtime
  -> m1max speech-to-speech backend
```

This is a control experiment between:

- deployed Pollen/HF backend: server-side `parakeet-tdt / responses-api / qwen3`
- self-run m1max backend: local `parakeet-tdt / responses-api / qwen3`

It is closer to deployed HF than the LiveKit prototype because the robot-facing protocol and backend stack
are the same family.

## m1max Setup

Backend directory:

```text
/Users/leon/projects/speech_to_speech_backend
```

Virtualenv:

```text
/Users/leon/projects/speech_to_speech_backend/.venv
```

Installed package:

```text
speech-to-speech==0.2.10
```

Launcher synced from this repo:

```text
scripts/m1max/run_s2s_backend.sh
-> /Users/leon/projects/speech_to_speech_backend/run_s2s_backend.sh
```

Current startup command:

```bash
cd /Users/leon/projects/speech_to_speech_backend
S2S_HOST=100.127.86.67 S2S_PROVIDER=openrouter ./run_s2s_backend.sh
```

Current running process:

```text
pid: 88313
url: ws://100.127.86.67:8765/v1/realtime
log: /Users/leon/projects/speech_to_speech_backend/logs/s2s-20260615-132956.log
```

## Backend Config

Confirmed startup config:

```text
STT: parakeet-tdt
  resolved on Apple Silicon to mlx-community/parakeet-tdt-0.6b-v3 on mps

LLM: responses-api
  model: openai/gpt-5.4-mini through OpenRouter
  note: m1max had OPENROUTER_API_KEY, not OPENAI_API_KEY

TTS: qwen3
  resolved on Apple Silicon to mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit
  speaker: Sohee
  language: auto
```

Caveat: this is not guaranteed bit-for-bit identical to deployed HF. The deployed compute endpoint exposes
`stt=parakeet-tdt`, `llm=responses-api`, and `tts=qwen3`, but not its exact LLM provider credential path.
The m1max run uses the same likely model name through OpenRouter because no `OPENAI_API_KEY` is present on
m1max.

## Startup Observations

- Parakeet model downloaded and loaded successfully.
- Responses API warmup through OpenRouter succeeded in about `1.39s`.
- Qwen3-TTS 6bit MLX model downloaded and loaded successfully.
- Qwen3-TTS warmup reported TTFA about `1.45s`; after warmup per-turn TTFA was around `0.18-0.22s`.
- The backend exposes only the websocket route; HTTP `/` and `/health` return 404.

## Offline Benchmark

Input set:

```text
artifacts/official-runtime/full-retest-sohee-20260614-1346/input_speech_review/
  full-retest-sohee-20260614-1346-speech-01.wav
  full-retest-sohee-20260614-1346-speech-02.wav
  full-retest-sohee-20260614-1346-speech-03.wav
  full-retest-sohee-20260614-1346-speech-04.wav
  full-retest-sohee-20260614-1346-speech-05.wav
  full-retest-sohee-20260614-1346-speech-06.wav
```

Artifacts:

```text
deployed HF baseline:
  artifacts/backend-benchmarks/full-retest-sohee-backend-compare-001/

m1max local S2S:
  artifacts/backend-benchmarks/s2s-m1max-local-20260615-01/
  artifacts/backend-benchmarks/s2s-m1max-local-20260615-02-rerun/
  artifacts/backend-benchmarks/s2s-m1max-local-20260615-04-rerun/
  artifacts/backend-benchmarks/s2s-m1max-local-20260615-06-rerun/
```

The first full local run hit a single-session release race on every other clip:

```text
ConnectionClosedError: All session slots are in use
```

This came from rapid benchmark reconnects against `num_pipelines=1`. Rerunning the failed clips separately
with a short gap succeeded. For future multi-clip runs, add benchmark inter-run delay or start the backend
with a larger pipeline pool if memory allows.

## Result Summary

| Backend | Output coverage | Median start -> first audio | Mean start -> first audio | Notes |
| --- | ---: | ---: | ---: | --- |
| Deployed HF | 5 / 6 | 2.581s | 3.853s | Prior same-day baseline. Clip 04 had no output. |
| m1max local S2S | 5 / 6 | 5.454s | 4.784s | Clip 04 also had no output; clip 06 was slower. |

Per-clip user transcript comparison:

| Clip | Deployed HF transcript | m1max local S2S transcript | Note |
| --- | --- | --- | --- |
| 01 | `This is much better...`; `...How are you doing?` | same | Strong parity. |
| 02 | `Nice to hear your voice smooth and your movement smooth too.` | same | Strong parity. |
| 03 | `What soccer?` | `What's soccer?` | Equivalent. |
| 04 | none | none | Both produced no useful turn. |
| 05 | `Stalker.` | `Soccer.` | Local S2S worse on this clip. |
| 06 | `Korea.`; `...supporting current team?` | `career.`; `Korean...supporting Korean team.` | Local S2S diverged in STT/turning. |

## Takeaways

- The m1max backend is viable: it runs the intended local Parakeet/Qwen stack and responds through the
  official app's Hugging Face handler boundary.
- It is much closer to deployed HF than the first LiveKit model choices were.
- The local S2S stack is not automatically better than deployed HF. It matched clips 01-04 well, but
  diverged on clips 05 and 06.
- For production simulation, the next useful test is live robot runtime with the official app pointed at:

```env
BACKEND_PROVIDER=huggingface
HF_REALTIME_CONNECTION_MODE=local
HF_REALTIME_WS_URL=ws://100.127.86.67:8765/v1/realtime
```

- For offline benchmarking, fix the rapid reconnect race before running larger batches:
  add an inter-run delay to the benchmark harness or restart the backend with `S2S_NUM_PIPELINES=2`
  if memory allows.
