# Runbook — bring up the reception robot

## Current Official-Runtime Live Test Flow

Use this path for the refactored official-runtime live tests. Do not start the live runner directly
for normal physical tests; use the ops wrapper so stale processes, media ownership, wake/sleep, and
teardown are handled by one owner.

Run from m1max:

```bash
cd ~/projects/reachy_mini
scripts/m1max/live_ops.sh status
scripts/m1max/live_ops.sh preflight
LIVE_DURATION=900 scripts/m1max/live_ops.sh clean-run
```

Start `clean-run` only after the human preflight check is acceptable. The preflight sequence exercises
the robot speaker path with a known-good WAV, then runs scripted goodbye -> greet through the same
policy/backend/speaker path used by live reception.

While the test runs, drop time-stamped feedback markers from a second pane so your
subjective reactions become queryable timestamps instead of memory. Press Enter to stamp
"now" (type a few words first for an inline note); annotate the rest after Ctrl-D:

```bash
.venv/bin/python scripts/m1max/mark.py        # locks onto the open run automatically
```

This writes `artifacts/markers-<run_id>.jsonl`, aligned by wall-clock `ts` to
events/audio/video for later review.

Expected lifecycle:

1. `clean-run` stops stale live-runner processes only.
2. It releases media, sends `goto_sleep`, and disables motors.
3. It starts the m1max speech-to-speech backend only if the websocket port is not already listening.
4. It acquires robot media, enables motors, and sends `wake_up`.
5. It starts exactly one official-runtime live runner.
6. On stop/interruption, it terminates the live runner, releases media, sleeps the robot, disables
   motors, and leaves the backend warm by default.

Backend lifecycle is intentionally separate from robot lifecycle. Keep the backend running across
robot tests unless you are changing model/voice/config, suspect backend state is wedged, or want a
full cold-start timing measurement. Use `scripts/m1max/live_ops.sh stop-backend` to stop only the
backend, or `scripts/m1max/live_ops.sh stop-all` for a full process shutdown plus robot sleep.

Milestone logging is intentionally split. No single line means "the robot is ready for everything."
Watch for separate lines like:

```text
official-runtime milestone <run-id>: robot_control_ready
official-runtime milestone <run-id>: robot_sdk_connected
official-runtime milestone <run-id>: robot_audio_warmup_ok
official-runtime milestone <run-id>: robot_video_warmup_ok
official-runtime milestone <run-id>: gesture_detector_init_start gestures=['Open_Palm'] threshold=0.5
official-runtime milestone <run-id>: gesture_detector_ready gestures=['Open_Palm'] threshold=0.5 load_ms=...
official-runtime milestone <run-id>: backend_handler_started
official-runtime milestone <run-id>: input_loop_starting
official-runtime milestone <run-id>: first_mic_frame_captured forwarded=False
official-runtime milestone <run-id>: audio_gate_opened audio_gate_open=True reason='wave'
official-runtime milestone <run-id>: first_mic_frame_forwarded_to_backend
official-runtime milestone <run-id>: first_backend_audio_pushed_to_robot
```

With `--audio-gate`, `first_mic_frame_captured` does not mean the backend is receiving speech.
Backend forwarding begins only after the wave policy opens the audio gate.

With `--gestures`, `robot_video_warmup_ok` only proves camera frames are flowing. Wave readiness is
the separate `gesture_detector_ready` milestone. Gesture diagnostics are recorded in
`events-<run-id>-NN.jsonl` as `vision.gesture_candidate`, `vision.gesture_suppressed`, and
`vision.gesture_emitted`; reception ingress is recorded as `policy.wave_received`.

To stop manually during a Codex-run live test, tell Codex `stop`; it should interrupt the wrapper and
let the wrapper perform teardown. If stopping from a shell, press `Ctrl-C` once and wait for the final
`live_ops.sh status` snapshot.

## Legacy Reception Daemon Flow

How to start the daemon and get reactions (greet / goodbye / wave) working, on
**m1max** (the brain computer). Two setup steps are easy to miss — see Gotchas.

All commands run on m1max: `ssh leon@100.127.86.67` (Tailscale). Robot daemon at
`192.168.1.165` (`REACHY_HOST`), control socket at `/tmp/reachy_mini_reception.sock`.

## 1. Start the daemon (must be from the `claude-test` tmux session)

The daemon shells out to `claude -p` for the brain, which needs keychain auth — that
only works from the GUI-rooted tmux session. **Don't launch it from a plain SSH shell.**

```bash
# attach the session:  tmux attach -t claude-test   (or send-keys into it)
cd ~/projects/reachy_mini && export REACHY_HOST=192.168.1.165 && \
  nohup caffeinate -dimsu .venv/bin/python -m reachy_mini_brain.reception serve \
    --perception --gestures --brain --brain-model haiku \
    --vision-interval 0.2 --save-turns > /tmp/reception_brain.log 2>&1 &
```

`caffeinate` keeps the Mac awake; `nohup` survives the SSH session closing. The daemon
prints a `run_id` and writes `artifacts/runs/run-<run_id>.json`.

## 2. Turn on the workers (serve comes up IDLE)

`serve` starts with **vision=off, voice=off** and recording off. Toggle what you need:

```bash
reception() { .venv/bin/python -m reachy_mini_brain.reception "$@"; }
reception vision on          # perception + gestures (required for any detection)
reception record on          # raw video  -> artifacts/video-<run_id>-NN.mkv   (needs vision on)
reception capture on         # per-frame tracks/events -> capture-<run_id>-NN.jsonl
reception audio-record on    # raw Cat-1 mic audio -> audio-<run_id>-NN.wav + .jsonl   (optional)
# voice turns on automatically when a wave starts a conversation — leave it off
```

## 3. Start the alert engine (SEPARATE process — this is what reacts)

The daemon only *detects* and logs events. A second process turns events into robot
reactions. **Without it, the robot sees you but never greets/waves back.**

```bash
nohup .venv/bin/python -m reachy_mini_brain.alert_engine --cooldown 5 \
  > /tmp/alert_engine.log 2>&1 &
```

Event → action: `approach → greet`, `depart → goodbye`, `wave → start a conversation`
(voice on + brain; be ready to talk). Restrict with `--types approach,depart` to skip the
live-conversation path.

## 4. Verify

```bash
reception status                              # run_id, vision/voice, session connected
tail -f /tmp/alert_engine.log                 # should print reactions as you approach/wave
grep <run_id> artifacts/events.jsonl | tail   # approach/depart/wave events being written
```

## 5. Teardown

```bash
reception shutdown          # finalizes record/capture/audio, removes the socket
pkill -f alert_engine       # stop the alert engine separately
```

## Gotchas (why this runbook exists)

1. **Launch the daemon from the `claude-test` tmux session**, not a plain SSH shell —
   `claude -p` needs keychain auth that only the GUI-rooted session has.
2. **The alert engine is a separate process.** `serve` + `vision on` makes the robot
   *detect* approaches/waves (events land in `events.jsonl`), but nothing reacts until
   `alert_engine` is running. Both of us missed this on the first try.
3. **One session only** — the daemon and the official Control app can't both hold the
   robot. Stop one before the other. If `serve` can't connect, check nothing else owns it.
