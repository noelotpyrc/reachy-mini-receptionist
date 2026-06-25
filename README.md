# Reachy Mini Brain

Reception-robot runtime for a [Reachy Mini](https://www.pollen-robotics.com/reachy-mini/).

The current product path is the ported **official-runtime** stack: robot audio/video/movement are
handled in an official-app-style live loop, while deterministic reception policies and a local m1max
speech-to-speech backend provide greet, goodbye, wave-to-chat, recording, and replay artifacts.

This path is based on Pollen Robotics' official Reachy Mini conversation app:
<https://github.com/pollen-robotics/reachy_mini_conversation_app>. This repo is not a clean fork; it
ports and adapts the official app's runtime design for our clinic-reception UX, local backend work,
and replay/logging harness.

The older `reception` daemon remains in the repo as a legacy fallback/reference path. Do not use it
as the default live-test path unless explicitly comparing against legacy behavior.

## Setup

```bash
uv sync --extra official-runtime --extra vision --extra gesture --extra diagnosis --extra dev
uv sync --extra audio --extra brain  # optional: legacy listen/speak + legacy brain tools
```

## Structure

```
src/reachy_mini_brain/
├── robot.py         # REST API client (urllib, no SDK)
├── motion.py        # CLI: wake-up, sleep, move-head, look, nod, shake, antennas
├── vision.py        # CLI: take-photo (SDK WebRTC camera)
├── audio.py         # CLI: listen, speak, play-sound, doa (SDK WebRTC audio)
├── video.py         # CLI: record (SDK WebRTC + OpenCV)
├── state.py         # CLI: get-state
├── audio_pacing.py  # shared WebRTC audio timing constants/helpers
├── official_runtime/
│   ├── live_app.py          # current live robot runner
│   ├── stream_runtime.py    # official-style audio/backend loop
│   ├── robot_io.py          # SDK session, mic, speaker, camera adapters
│   ├── reception.py         # deterministic reception policy
│   ├── perception.py        # person + gesture pipeline for the new runtime
│   ├── artifacts.py         # run manifests, audio, events, policies, realtime JSONL
│   └── ...                  # backend adapters, replay tools, cues, capabilities
└── legacy daemon modules    # reception.py/session.py/brain.py/etc.; fallback only
```

## Usage

```bash
# Take a photo
.venv/bin/python -m reachy_mini_brain.vision take-photo

# Move the robot
.venv/bin/python -m reachy_mini_brain.motion wake-up
.venv/bin/python -m reachy_mini_brain.motion look --direction left
.venv/bin/python -m reachy_mini_brain.motion nod
.venv/bin/python -m reachy_mini_brain.motion sleep

# Listen and speak
.venv/bin/python -m reachy_mini_brain.audio listen --duration 5
.venv/bin/python -m reachy_mini_brain.audio speak "Hello, I am Reachy"

# Record video
.venv/bin/python -m reachy_mini_brain.video record --duration 10

# Read state
.venv/bin/python -m reachy_mini_brain.state get-state
```

### Current reception live path

```bash
# Current live-test path on m1max
ssh leon@100.127.86.67
cd ~/projects/reachy_mini_receptionist_clean
scripts/m1max/live_ops.sh status
scripts/m1max/live_ops.sh preflight     # requires human confirmation before live test
LIVE_DURATION=900 scripts/m1max/live_ops.sh clean-run
scripts/m1max/live_ops.sh clean-stop

# Direct CLI entrypoint used by the ops wrapper
.venv/bin/python -m reachy_mini_brain.official_runtime.live_app --help

# Legacy fallback only: old resident daemon + alert engine
.venv/bin/python -m reachy_mini_brain.reception serve --perception --brain
.venv/bin/python -m reachy_mini_brain.alert_engine
```

See `docs/robot-guide.md` for the full CLI reference.

## Architecture

```text
m1max live_ops.sh
    -> local speech-to-speech backend (Parakeet STT + remote LLM + Qwen3 TTS)
    -> official_runtime.live_app
        -> robot REST lifecycle/motors
        -> SDK WebRTC audio/video
        -> reception policies + artifact recorder
    -> Reachy Mini robot runtime
```

- `docs/runbook.md` is the operational entrypoint for live tests.
- `scripts/m1max/live_ops.sh` owns preflight, backend lifecycle, wake/sleep, live start, and cleanup.
- `official_runtime.live_app` owns the live stream loop, reception policies, cues, and run artifacts.
- `robot.py` still wraps daemon REST lifecycle/motor APIs used by both current and legacy paths.
- Camera and audio use the SDK WebRTC pipeline; simple one-shot CLIs remain available for debugging.

## Conventions

- `robot.ensure_ready()` before any robot interaction (handles daemon startup + caching)
- Always `wake_up()` before motion, `go_to_sleep()` when done
- CLI modules use `click` with `@click.group()` + `@cli.command()` pattern
- Photos save to `artifacts/` by default (gitignored)
- Live-test commands that require physical confirmation should run through `live_ops.sh`.

## Tests

```bash
# Automated integration tests
.venv/bin/python -m pytest tests/test_integration.py -v

# Human-observable e2e tests (requires -s for confirm prompts)
.venv/bin/python -m pytest tests/test_e2e.py -v -s
.venv/bin/python -m pytest tests/test_e2e_vision.py -v -s
```

## Docs

- `docs/runbook.md` — current live-test operations for the official-runtime path
- `docs/todo-official-runtime.md` — ordered post-pivot execution checklist
- `docs/plan-official-runtime-refactor.md` — accepted architecture and cleanup direction
- `docs/live-test-log.md` — **on-robot test log** (good / ugly / bad, newest first)
- `docs/robot-guide.md` — full CLI reference, including current official-runtime ops and legacy CLIs
- `docs/archive/legacy/plan-reception.md` — legacy daemon plan and historical design context
- `docs/archive/legacy/plan.md`, `docs/archive/legacy/progress.md` — earlier generic roadmap +
  implementation log
  (kept for history)
