# OPS test todos

**Scope:** OPS management only (`docs/ops-design.md`, TODO #5). This doc is not a live-test log and
does not cover backend quality, conversation UX, or Rerun diagnosis except where OPS hands off a
latest-run pointer.

**Status:** first-pass OPS accepted. Onsite robot/human gates were accepted from prior preflight
runs plus the rebuilt-uv live/audio checks on 2026-06-24. The backend churn test was completed
offline with no robot contact.

## Offline local tests

- [x] Safety: physical actions fail before robot calls when `--confirm-physical` is missing.
- [x] Runner state: save/load, stale PID detection, unmanaged runner detection.
- [x] Latest-run pointer: save/load shape for #6 handoff.
- [x] Command construction: live runner, audio playback, and scripted policy preflight commands.
- [x] Detached launch: backend/runner subprocesses use `start_new_session=True`.
- [x] Keep-awake launch: backend/runner start a `caffeinate -w <pid>` watcher on macOS when enabled.
- [x] Runner CLI primitives: `runner status/start/stop` route to core actions and preserve safety.
- [x] Phase workflows: `start-session`, `stop-session`, `shutdown`, and `full preflight` call
  resource primitives in order.
- [x] Backend start/stop: mocked port/process paths cover already-running, ready, exited, and timeout.
- [x] Aggregate status: default excludes robot; `--include-robot` includes read-only robot status.
- [x] Runtime path validation: missing backend script or Python binary fails with a clear error.

## m1max tests, no robot contact

- [x] Sync the clean repo OPS files to m1max without deleting remote artifacts.
- [x] Import/CLI smoke: `reception-ops --help` loads on m1max.
- [x] Read-only local status: `reception-ops --json-output status` works without robot queries.
- [x] Backend read-only status: `reception-ops backend status` reports port/process state.
- [x] Unit tests: run `tests/test_ops_management.py` on m1max.
- [x] Optional, only when process churn is acceptable: backend `start -> status -> stop`, with no
  robot commands.

## Robot machine-verified tests

Requires explicit user approval because these commands talk to the robot.

- [x] `status --include-robot`: reads daemon/media/motors/move/volume state.
- [x] `wake-robot`: verifies daemon/motor/media state where exposed.
- [x] `sleep-robot`: verifies sleep/motor/media state where exposed.
- [x] `shutdown`: stops runner if present, sleeps robot, disables motors.
- [x] `preflight robot-state`: read-only robot status substep.

## Robot + human quality gates

Requires explicit user approval and a human near the robot.

- [x] `preflight audio-playback`: machine verifies completion; human accepts/rejects smoothness.
- [x] `preflight policy-goodbye`: machine verifies scripted run; human checks speech/behavior.
- [x] `preflight policy-greet`: machine verifies scripted run; human checks speech/behavior.
- [x] Full `preflight`: playback -> goodbye -> greet sequence.
- [x] `start-session` / `stop-session`: full run lifecycle around a short live test.

## Acceptance notes

- `preflight audio-playback` passed on the rebuilt uv venv in run
  `official-audio-preflight-20260624-152222`; machine result was `ok`, and the human gate was
  accepted by the onsite tester.
- Full live lifecycle on the rebuilt uv venv was accepted by the onsite tester: the ready cue was
  observed physically, live testing proceeded, and `stop-session` later stopped the runner and put
  the robot into sleep/motors-off cleanup.
- Earlier onsite preflight policy checks (`goodbye`, `greet`, full preflight) were accepted before
  the uv rebuild. The rebuilt uv audio-playback check above covered the environment-regression risk
  for the robot speaker path.
- Backend churn test passed offline on 2026-06-24: stopped existing backend PID `57619`, verified
  port `127.0.0.1:8765` down, started fresh backend PID `69873`, verified port/process live, then
  stopped PID `69873` and verified final stopped state.
