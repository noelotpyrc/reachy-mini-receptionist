# Live test log

Running record of **on-robot (live) tests** — what we ran, what held up, what didn't.
Newest first. Live tests run on m1max + the real robot (dev machine is plumbing only).

Each entry uses three buckets:
- 🟢 **Good** — worked, met expectations.
- 🟡 **Ugly** — acceptable but needs refining (works after a workaround/fix, or rough edges).
- 🔴 **Bad** — clear issue, fails expectation; needs a fix or a decision.

---

## 2026-06-25 — deploy-checkout preflight playback: start clipped until robot reboot

**Setup:** m1max + real robot, new Git-managed deploy checkout
`/Users/leon/projects/reachy_mini_receptionist_deploy`, OPS preflight audio playback only:

```text
.venv/bin/python -m reachy_mini_brain.official_runtime.ops_cli --confirm-physical --json-output preflight audio-playback
```

This test used the known-good preflight WAV:

```text
/Users/leon/projects/reachy_mini_receptionist_deploy/artifacts/official-runtime-live/audio/playable/audio-response-resp_db3304df3e804556b0aaa7ed7990048f-official-live-20260623-122844-01-pcm16.wav
```

Artifacts:

- clipped pre-reboot playback runs:
  `official-audio-preflight-20260625-130650`,
  `official-audio-preflight-20260625-130956`,
  `official-audio-preflight-20260625-131235`
- smooth post-reboot playback run:
  `official-audio-preflight-20260625-131441`

### 🟢 Good
- **New m1max deploy checkout was runnable.** Readiness checks passed before robot-touching tests:
  deploy checkout at commit `77b2a56`, `.venv` present, `.env` present, preflight WAV present,
  backend stopped, runner stopped, robot daemon reachable, media released, motors disabled.
- **After a full robot reboot, the same preflight playback sounded smooth.** User feedback for
  `official-audio-preflight-20260625-131441`: smooth. Machine logs again showed audio warmup OK,
  the full WAV loaded and pushed (`1.504s`, `24064` samples), scripted playback completed, and
  session cleanup finished.

### 🔴 Bad
- **Before reboot, the robot clipped the beginning of the known WAV.** User feedback:
  - `official-audio-preflight-20260625-130650`: only the late half was spoken.
  - `official-audio-preflight-20260625-130956`: not complete; heard roughly "hi, can I help",
    omitting the leading "how".
  - `official-audio-preflight-20260625-131235`: same missing leading word.
- **Machine logs did not report a sender-side short write.** For the failed runs, the runtime still
  logged the full WAV as loaded and pushed (`1.504s`, `24064` samples) and returned OPS status `ok`.
  Therefore the current artifacts prove the app attempted to send the full audio, but they do not
  prove what the robot speaker actually played.

### 🟡 Ugly / diagnosis
- **This appears stateful on the robot/runtime side, but that is not settled.** The strongest
  observed fact is that the same preflight WAV and same deploy checkout failed repeatedly before
  reboot, then passed after reboot. This resembles earlier cases where robot audio behavior
  recovered after reboot, but it is still an observation, not a root cause.
- **The WebRTC shutdown warning is low diagnostic value for this symptom.** Failed and successful
  runs can log `send failed because receiver is gone` during session teardown after playback has
  already completed. Treat it as a cleanup-side warning unless a future artifact shows it happening
  before or during audio push.
- **Hypotheses for later debugging, not fixes yet:**
  - robot WebRTC/audio sink starts in a stale state and drops the first frames after a session starts;
  - robot audio device / daemon state degrades over time and reboot clears it;
  - media-session setup reports "ready" before the speaker path is fully primed;
  - less likely from today's evidence alone: m1max code/venv corruption, because the post-reboot run
    used the same deploy checkout and same WAV.

### Decision / Next
- Keep the known WAV preflight as a required human gate before live conversation.
- If the clipped-start symptom returns, collect exact run IDs and compare pre/post reboot again before
  changing code.
- Future debugging should instrument or test robot-side playback readiness directly: first-audio
  arrival at the robot, speaker/sink priming state, whether a daemon/media restart is enough, and
  whether full robot reboot is the only reliable recovery.

## 2026-06-23 — official-runtime preflight/live validation after S2S policy-speech rollback

**Setup:** m1max + real robot, `scripts/m1max/live_ops.sh preflight` followed by
`scripts/m1max/live_ops.sh clean-run`, ported `official_runtime.live_app`, local S2S backend at
`ws://127.0.0.1:8765/v1/realtime`, perception + gestures + audio gate + ready cue + conversation
thinking cues. Final accepted backend config for the pass run: Parakeet TDT STT, OpenRouter-compatible
Responses API with `openai/gpt-5.4-mini`, local Qwen3 TTS, Sohee voice.

Artifacts:

- accepted policy preflight manifest:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/runs/run-official-policy-preflight-20260623-142721.json`
- accepted live run manifest:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/runs/run-official-live-20260623-142850.json`
- accepted live event/policy/realtime/capture JSONL:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/events/events-official-live-20260623-142850-01.jsonl`
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/policies/policies-official-live-20260623-142850-01.jsonl`
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/realtime/realtime-official-live-20260623-142850-01.jsonl`
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/capture/capture-official-live-20260623-142850-01.jsonl`
- accepted backend log after switching back to `gpt-5.4-mini`:
  `/Users/leon/projects/reachy_mini/artifacts/logs/s2s-backend-live-20260623-142516.log`
- earlier same-day debug runs, kept for traceability but not fully diagnosed here:
  `official-live-20260623-114545`, `official-live-20260623-121159`,
  `official-live-20260623-121934`, `official-live-20260623-122844`,
  `official-live-20260623-130804`, `official-live-20260623-132930`,
  `official-live-20260623-142016`, plus policy-preflight runs
  `official-policy-preflight-20260623-133758`, `134352`, `134701`, `141136`, and `141821`.

### 🟢 Good
- **Final preflight passed.** The full preflight used a known-good playback probe, then scripted
  goodbye -> greet through the live policy/backend/speaker path. The final policy preflight run
  `official-policy-preflight-20260623-142721` completed with one goodbye and one welcome, then cleaned
  down to media released and motors disabled.
- **Final live run passed.** User feedback for `official-live-20260623-142850`: pass, no issue. Logs
  show audio warmup OK, video warmup OK, gesture detector ready, backend handler started, wave gate
  opened, mic frames forwarded, policy pulses emitted, thinking cues emitted, and backend audio pushed
  to the robot.
- **`gpt-5.4-mini` backend was restored and verified.** The running m1max process showed
  `--model_name openai/gpt-5.4-mini` against the OpenRouter-compatible Responses API. Backend warmup
  completed successfully before the final preflight/live run.
- **Preflight is now useful as an ops gate.** The accepted sequence is: clean-stop -> known-good robot
  playback probe -> human audio confirmation -> scripted goodbye/greet policy-flow probe -> live run.
  This catches both robot playback path problems and S2S/backend latency problems before a human test.

### 🟡 Ugly / diagnosis
- **Earlier same-day runs exposed ops/model fragility.** Before the final pass, we saw a mix of failed
  or rough runs: wave not triggering in one run, laggy/choppy voice in another, cached goodbye/greet WAV
  quality rejected by user, and a temporary `openai/gpt-oss-20b:nitro` backend run that was not accepted
  as the live-test baseline. These are documented as symptoms only; no root cause is settled from the
  live feedback alone.
- **Cached fixed-policy WAVs are not accepted for greet/goodbye.** The policy path was restored to use
  the S2S backend for goodbye/greet so preflight still exercises the LLM/S2S path and can warn on model
  or backend latency.
- **Robot playback health still needs a human preflight check.** Recorded/generated WAVs can sound fine
  locally while the robot playback path is choppy. The reliable acceptance signal for the speaker path
  remains physical confirmation during preflight.
- **Known console warnings are still noisy.** The playback probe can emit `send failed because receiver
  is gone` after finishing playback, and dependencies can emit `portable_clearcut_uploader` warnings.
  Today these were not treated as failures when physical playback and final live behavior were OK.
- **Stop path remains rough but cleanup succeeds.** `clean-stop` still sometimes hard-stops a stuck live
  runner after TERM, but it successfully releases media, disables motors, and stops running moves.

### Decision / Next
- Keep the ported official runtime + m1max local S2S backend as the accepted live path.
- Keep `openai/gpt-5.4-mini` as the current OpenRouter model default for live tests unless explicitly
  changed for an experiment.
- Run full preflight before each live conversation test and wait for human confirmation before starting
  the live run.
- Continue the planned follow-ups: repo housecleaning around the new runtime/backend direction, real
  clinic context for the backend LLM, agentic-context exploration, and better ops/status tooling.

## 2026-06-22 — remote-LLM cue-policy retest on ported official runtime

**Setup:** m1max + real robot, `scripts/m1max/live_ops.sh clean-run`, ported
`official_runtime.live_app`, local S2S backend at `ws://127.0.0.1:8765/v1/realtime`, remote LLM
via OpenRouter-compatible Responses API (`openai/gpt-5.4-mini`), Sohee voice, perception + gestures +
audio gate + ready cue + conversation thinking cues.

Artifacts:

- run manifest:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/runs/run-official-live-20260622-123850.json`
- event/policy/realtime JSONL:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/events/events-official-live-20260622-123850-01.jsonl`
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/policies/policies-official-live-20260622-123850-01.jsonl`
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/realtime/realtime-official-live-20260622-123850-01.jsonl`

### 🟢 Good
- **Thinking antenna cue timing was mostly good.** User feedback: thinking movements were mostly at
  good timings. Artifact diagnosis: 7 thinking-cue cycles fired, with cue-to-first-audio latencies of
  roughly 1.2s, 1.3s, 1.3s, 2.5s, 2.9s, 3.7s, and 2.5s.

### 🟡 Ugly / diagnosis
- **Startup still had a first-wave / greet-goodbye rough edge.** User feedback: the first wave did
  not get a response; after the second wave the robot emitted goodbye + greet. Artifact diagnosis:
  policy events showed `depart` then `approach` on the same tracked id within 0.4s:
  `+110.884s depart id=4`, `+111.292s approach id=4`. The run did not capture per-frame vision
  metadata (`capture_vision=false`), so logs cannot prove whether the first wave was missed by gesture
  confidence, framing, warmup, or policy gating. Do not treat the `depart -> approach` sequence alone
  as a settled root cause; review it with capture metadata before changing behavior.
- **At least one thinking cue was missed.** Artifact diagnosis: later final transcript events did not
  always produce `conversation_cue.thinking_started`. One candidate hypothesis is policy dispatch using
  one async task per runtime event, including high-rate audio frames, but this is not confirmed and
  should be discussed before any behavior change.
- **Console cue milestone labels were noisy.** Cue lifecycle events printed as
  `antenna_cue_thinking_unknown` because the console formatter did not distinguish cue lifecycle phase
  from antenna position phase.

### Fix Applied After Run
- Console milestone labels now distinguish cue lifecycle from antenna position.
- `clean-stop` now cancels lingering move UUIDs with the correct `/api/move/stop` JSON body.
- `live_ops.sh clean-run` now enables lightweight `--capture-vision` by default so the next recurrence
  has frame-level people/tracks/events JSONL without recording video.
- Local regression test passed: `48 passed` for `tests/test_official_runtime.py`; m1max Python compile
  and `bash -n scripts/m1max/live_ops.sh` checks passed.

### Next
- Retest startup wave + greet/goodbye with capture JSONL enabled.
- If first-wave miss recurs, inspect capture JSONL around startup for gesture confidence, detected
  hand label, people count, track area, and whether policy gating or detector output is responsible.
- Discuss candidate fixes before implementation. Possible directions include better startup warmup
  gating, cross-action greet/goodbye debounce, visit-state tuning, or wave priority, but none are
  accepted yet.
- Discuss the missed thinking-cue hypothesis before implementation. Possible directions include
  stronger per-response audio state, FIFO policy dispatch, explicit cue state events, or better
  observability, but none are accepted yet.

## 2026-06-21 — official-runtime wave-chat / greet-goodbye with m1max local S2S backend

**Setup:** m1max + real robot, `scripts/m1max/live_ops.sh clean-run`, ported
`official_runtime.live_app`, local HF-compatible S2S backend at `ws://127.0.0.1:8765/v1/realtime`,
Sohee voice, perception + gestures + audio gate + ready cue + video warmup. Second run reused the
already-warm backend instead of restarting it.

Artifacts:

- run manifest:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/runs/run-official-live-20260621-133812.json`
- antenna retest run manifest:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/runs/run-official-live-20260621-140328.json`
- backend log:
  `/Users/leon/projects/reachy_mini/artifacts/logs/s2s-backend-live-20260621-133759.log`

### 🟢 Good
- **Best wave-chat UX so far.** User feedback: the run was very smooth, the smoothest wave-chat
  session so far.
- **Greet/goodbye triggers were smooth.** User feedback: goodbye/greet trigger behavior was also
  smooth in this run.
- **Antenna retest was successful enough for acceptance.** In the follow-up run after wiring direct
  policy antenna cues, user feedback was again very smooth voice for all policies. Antenna movement
  worked fine overall.
- **Split milestone logging helped distinguish states.** The run printed separate milestones for
  robot control, SDK connect, audio/video warmup, backend handler startup, input loop start, first
  mic frame captured, audio gate opened, first forwarded mic frame, and first backend audio pushed.

### 🟡 Ugly / diagnosis
- **Console milestones are sparse transition markers, not a live conversation monitor.** After the
  first gate/open/audio-output transitions, normal conversation can continue without new console
  milestone lines. Realtime JSONL artifacts still hold turn/backend events. Follow-up: add optional
  per-turn console summaries for final user transcript, assistant text/audio start, and response done.
- **Policy antenna movement was missing.** User feedback: antenna movement seemed absent for
  greet/goodbye and wave-chat. Code diagnosis: the ported live app registered `antenna_pulse`, but
  `RuntimeContext.state["movement_manager"]` was `None`, so `queue_antenna_pulse()` returned `False`.
  The startup ready cue worked because it used a separate direct antenna path.
- **First goodbye may have missed antenna movement.** In the antenna retest, user feedback was that
  antenna worked fine overall but seemed to miss the first goodbye. Treat this as a UX polish item, not
  a blocker for accepting the local-backend + official-runtime direction.
- **Stop path is still rough.** `clean-stop` completed robot cleanup, but the live runner did not exit
  on TERM within the wrapper's short wait and was hard-killed. Backend stayed warm as intended.

### Fix Applied After Run
- Greet/goodbye/wave policy pulses now use a direct antenna-only cue in the ported live runtime,
  emitting `runtime.antenna_cue` separately from startup `runtime.ready_cue`.
- Local regression test passed: `45 passed` for `tests/test_official_runtime.py`.

### Decision / Next
- Accept the m1max local S2S backend + ported official-runtime live app as the new basic-function path
  for reception UX, replacing the legacy daemon as the product direction.
- Keep the legacy daemon available only as fallback/regression reference until repo housecleaning is
  finished.
- Immediate next work: houseclean the repo around the new runtime/backend direction; add real clinic
  context to the backend LLM; add antenna movement during wave-chat speaking; improve ops management
  tools and graceful stop/status behavior.

## 2026-06-17 — ported official-runtime smoke: local HF backend

**Setup:** m1max + real robot, ported `reachy_mini_brain.official_runtime.live_app`, backend
`hf-official` in local mode via `HF_REALTIME_WS_URL=ws://100.127.86.67:8765/v1/realtime`,
Sohee voice, `--no-perception --no-audio-gate`. Initial smoke used 30s input duration; later
ready-cue retests used 60-90s input duration.

Artifacts:

- run manifest:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/runs/run-official-live-20260617-135802.json`
- ready-cue retest manifest:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/runs/run-official-live-20260617-143749.json`
- official-semantics ready-cue retest manifest:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/runs/run-official-live-20260617-151455.json`
- input/output/response WAVs and realtime/event JSONL files under:
  `/Users/leon/projects/reachy_mini/artifacts/official-runtime-live/`

### 🟢 Good
- **Ported path started and exited cleanly.** The run captured robot input audio, reached the local
  HF-compatible backend, produced assistant audio, pushed output through the robot speaker path, and
  wrote input/output/per-response audio plus realtime events.
- **Official-semantics ready-cue retest was mostly good.** After replacing the ported speaker sink with
  the official app's output contract, the cue was present, responses were present, and user feedback was
  only one small choppy word. This is a major improvement versus the prior ready-cue run that stretched
  `4.832s` of backend audio across `18.221s` of output handling.

### 🔴 Bad / diagnosis
- **Robot output was physically choppy with low/high-volume symptoms.** User feedback from the live
  test: it did output, but was choppy and seemed to have the same volume fluctuation issue seen in
  earlier Sohee runs.
- **The ported sink did not use the known-good 20ms WebRTC push shape.** Log timing showed response 2
  contained `9.088s` of audio but its output frames were delivered over `11.780s`, with 26 inter-frame
  gaps more than 50ms late and 4 gaps more than 100ms late. The sink was pacing large backend chunks
  (`1024`/`2048`/`3072` samples) instead of splitting into exact 20ms frames (`320` samples at 16 kHz).
- **Code fix applied after diagnosis:** robot speaker output now derives chunk size from sample rate and
  a shared 20ms WebRTC frame policy, then pushes with monotonic realtime pacing. At 16 kHz this is
  `320` samples; at 24 kHz it is `480`. Local regression tests passed: `41 passed` for
  `tests/test_official_runtime.py` and `tests/test_audio_pacing.py`.
- **The first 20ms-frame fix was still insufficient in the live runtime.** In
  `official-live-20260617-143749`, user feedback was that output was choppy and worse than the first
  run. Objective timing matched the feedback: the backend produced `4.832s` of audio, but the runtime
  took `18.221s` to push the 31 backend chunks through the robot path; every inter-chunk gap had more
  than 200ms of extra delay. The cause is architectural: playback pacing was still happening inside
  the async backend-output loop, so event-loop/input/backend load delayed the sleeps and stretched
  playback.
- **Second code fix applied after the worse retest:** robot speaker output now uses a dedicated
  playback thread. The async backend-output loop enqueues 20ms chunks and returns quickly; the speaker
  thread owns monotonic realtime pacing and the runtime drains/closes it on shutdown. Local tests
  passed: `43 passed` for `tests/test_official_runtime.py` and `tests/test_audio_pacing.py`.
- **Architecture audit superseded the threaded 20ms fix.** The official app does not pace live backend
  output in Python. It converts/resamples each `handler.emit()` audio tuple, calls
  `robot.media.push_audio_sample(audio_frame)` once, then yields to the event loop. The ported
  `ReachyAudioSink` now follows that contract: no worker thread, no pacing queue, no forced 20ms split,
  and no sleep-based output timing. Local regression tests passed: `44 passed, 1 skipped` for
  `tests/test_official_runtime.py`, `tests/test_audio_pacing.py`, and the guarded manual antenna test.

### Next
- Use the official-semantics retest as the new smoke baseline. If choppiness returns, inspect robot
  runtime/media logs and compare the recorded output WAV to physical UX; do not infer audio quality from
  transcripts or backend logs alone.
- Next live UX test can move back to wave/chat or perception once the user is ready.

---

## 2026-06-16 — robot-local playback vs m1max WebRTC pacing A/B

**Setup:** same known-good Sohee response WAV:

- local/dev source and m1max copy SHA-256:
  `7e478ce6218c365e398e432eae9fc491d855c988a6c7cd81192a5fb5847559ca`
- robot-local copy: `/tmp/sohee-long-response.wav`
- m1max copy: `artifacts/dry-live-test/sohee-long-response.wav`

### 🟢 Good
- **Robot-local ALSA playback was clean.** Stopped `reachy-mini-daemon.service`, played the WAV
  directly on the robot with `aplay -D plughw:0,0 /tmp/sohee-long-response.wav`, then restarted the
  daemon. User feedback: clean. This rules down the WAV file and basic robot speaker/audio hardware
  path for this clip.
- **Exact-paced m1max WebRTC playback was smooth.** Retested m1max -> robot WebRTC playback with
  monotonic realtime pacing: 320 samples every 20ms, deadline-driven. User feedback: smooth.

### 🔴 Bad / diagnosis
- **Fast-paced m1max WebRTC playback reproduced choppiness.** The old direct playback loop pushed
  320-sample chunks every `chunk_duration * 0.9` (~18ms for a 20ms chunk), overfeeding the WebRTC
  sender by about 11%. In the A/B retest:
  - Pass A, old `0.9x` pacing: confirmed choppy.
  - Pass B, exact realtime pacing: confirmed smooth.
- **The `send failed because receiver is gone` WebRTC signaller warning appeared after both good and
  bad WebRTC passes.** Treat it as teardown/session-close noise unless it appears during startup or
  active playback; it does not distinguish this pacing failure.

### Decision
- All owned WebRTC speaker-push paths must use monotonic exact realtime pacing. Do not use
  `time.sleep(chunk_duration * 0.9)` or other faster-than-realtime loops.

---

## 2026-06-15 — dry playback + movement degradation test

**Setup:** m1max + real robot, no full conversation session. Direct robot REST/SDK path from
`/Users/leon/projects/reachy_mini`: one known Sohee response WAV replayed through WebRTC speaker output
while wobbling plus small head/antenna movements were active.

Artifacts / control notes:

- Test WAV copied to m1max:
  `artifacts/dry-live-test/sohee-long-response.wav` (`6.69s`, 16 kHz).
- Shutdown snapshot:
  `artifacts/dry-live-test/dry-sleep-20260615-142118-shutdown-snapshot.json`.
- Robot after teardown: media released, motors disabled, daemon stopped with
  `POST /api/daemon/stop?goto_sleep=false` after an explicit `goto_sleep`.

### 🔴 Bad / diagnosis
- **Playback plus movement/wobbling reproduced a severe choppy-output symptom.** User feedback:
  the issue happened again, but this time it was not low/high volume; it was completely choppy.
  The synchronized pass started playback first, then enabled wobbling and sent four small
  head/antenna movement commands while the response WAV was playing.
- **This reproduction did not require the full realtime conversation backend.** The test used direct
  playback plus robot movement controls, so this points the next diagnosis toward robot media/control
  runtime interaction rather than only remote HF, local S2S, or LLM/TTS generation. This is not yet
  proof of the exact root cause.
- **WebRTC teardown logged a signalling warning after playback:** `send failed because receiver is gone`.
  Treat this as a clue to inspect, not as a proven cause.

### Next
- Repeat the same dry playback + movement test after the robot has stayed slept overnight. Compare
  whether the choppy symptom appears immediately after a clean next-day wake/start.
- Add an explicit live-test teardown command that disables wobbling, releases media, sends
  `goto_sleep`, disables motors, and then stops the robot daemon.
- For the next reproduction, capture robot-side daemon/media logs if direct robot SSH is available.

---

## 2026-06-14 — official-runtime replay-observability Sohee run

**Setup:** m1max + real robot, official-app refactor in
`/Users/leon/projects/reachy_mini_conversation_app`, Hugging Face deployed realtime backend,
`REACHY_MINI_CUSTOM_PROFILE=clinic_receptionist`, Sohee voice, reception perception + gestures +
audio/video/capture/realtime artifacts enabled.

Run:

- `replayobs-sohee-20260614-123126` — Sohee live test with the expanded replay artifacts
  (session snapshot, realtime events, policy events, per-response output WAV/JSONL).

### 🔴 Bad / diagnosis
- **Conversation could drop after a partial/final transcript mismatch.** In this run, the backend
  emitted VAD/STT events for a user speech item: speech started, partial transcript text reached
  `I still cannot hear you.`, speech stopped, then the completed transcript for the same item was
  empty (`""`). No `response.created` followed for that user item. The current client logic treats
  only non-empty completed transcripts as user turns, so the meaningful partial was not promoted into
  a usable turn and the reception policy did not refresh conversation activity. This is evidence of
  both a backend/STT edge case and a client robustness gap; it does **not** yet explain why the backend
  finalized the transcript as empty.
- **Robot playback can degrade when speech overlaps movement/wobbling.** The recorded per-response
  Sohee WAVs inspected locally sounded loud and clear, so the backend-generated audio is not the
  primary suspect for the live "volume changes / disappears" symptom. A controlled playback diagnostic
  replayed a known-good Sohee WAV through the official `LocalStream.play_loop()` path. Playback alone
  sounded usable, while playback with the official movement manager / wobbling path reproduced the
  live issue per user feedback. A later "antenna-only" isolation attempt was inconclusive because
  `robot.enable_wobbling()` is a global runtime setting and may have persisted; do not treat antenna
  movement alone as proven guilty yet.
- **After robot restart, the Sohee volume/dropout issue disappeared in a full-runtime retest.** User
  feedback on `full-retest-sohee-20260614-1346`: the sound-volume issue was gone. This means the
  earlier movement/wobbling suspicion is not a simple deterministic cause. A more likely framing is
  that robot playback/control state can degrade across runs, and movement/wobbling/control-loop load
  may amplify it when the robot/daemon connection is unhealthy. The retest still logged one
  `Failed to set robot target: Lost connection with the server.` error, so movement transport health
  remains suspect even when the audio sounds acceptable.
- **Realtime tool protocol errors still occur.** During `full-retest-sohee-20260614-1346`, the backend
  repeatedly called `remember`, the tool executed, and then the backend returned
  `invalid_conversation_item` because the `call_id` was not found in conversation history. This is
  separate from the audio-quality issue but can pollute the conversation with visible error messages.

### Next
- Add transcript-fallback handling for realtime speech items: track the latest meaningful partial per
  item, refresh conversation activity on speech/partial events, and explicitly log when an empty final
  transcript falls back to a partial.
- Investigate the official runtime's audio-reactive wobbling / movement overlap with speaker playback:
  disable wobbling in reception mode, retest speech while movement is active, and only then decide
  whether antenna cues need to be delayed, shortened, or separated from audio playback.
- Add process-log capture to every live test command until the movement/control and realtime-tool
  errors are understood.

---

## 2026-06-12 — official-runtime refactor live tests; user feedback from live UX

**Setup:** m1max + real robot, official-app refactor in
`/Users/leon/projects/reachy_mini_conversation_app`, Hugging Face deployed realtime backend,
`REACHY_MINI_CUSTOM_PROFILE=clinic_receptionist`, reception perception + gestures + audio/video/
capture artifacts enabled.

Runs discussed during live testing:

- `idle-smoke-20260612-1510` — no physical interaction; readiness/recording smoke only.
- `goodbye-20260612-1530` — invalid UX test; startup exposed a movement/perception import race.
- `goodbye-20260612-1540` — failed UX: robot kept moving after startup, with goodbye→welcome behavior.
- `goodbye-20260612-134651` — greet/goodbye retest after idle-motion fix.
- `wavechat-20260612-135545` — Aiden/default HF voice; longer wave→chat test.
- `voice-sohee-20260612-140938` — Sohee voice audition; wave/chat re-test.
- `voice-sohee-retest-20260612-142630` — Sohee voice re-test after the first rough Sohee run.

### 🟢 Good
- **Official realtime runtime feels much better than the legacy daemon implementation.** User feedback
  after `wavechat-20260612-135545`: "that was so much better than our daemon impl." The realtime path
  felt closer to an actual conversation than the old local STT → brain → TTS sandwich.
- **Goodbye and greet worked as expected after the idle-motion fix.** In `goodbye-20260612-134651`,
  user feedback: "goodbye and greet worked as expected." This was after disabling the official app's
  continuous idle breathing in reception mode.
- **Wave→chat can work.** During `wavechat-20260612-135545`, wave opened the conversation, the robot
  responded to speech, and the user was able to have a multi-turn chat.
- **Conversation quality was notably better than the old daemon path.** The robot could handle natural
  follow-ups, barge-in/interruption, a joke request, and clinic questions. It still made mistakes, but
  the interaction was usable in a way the previous daemon was not.
- **The second Sohee run was much better than the first Sohee run.** User feedback on
  `voice-sohee-retest-20260612-142630`: "this was a much better Sohee run." The run stayed usable
  through multi-turn English/Korean switching, with only one obvious long-lag event called out live.

### 🟡 Ugly
- **Default startup/reset antenna pose looks mechanically bad.** User feedback after the better
  wave/chat run: whenever the app starts, the robot returns to a default position where an antenna has
  a mechanical issue. This is a physical UX issue and needs live calibration, not a guessed code change.
- **Clinic-fact response had a noticeable long lag.** User feedback after `wavechat-20260612-135545`:
  asking about clinic facts took a long time to respond. Root cause is not known yet.
- **Aiden is the wrong receptionist personality.** User feedback: Aiden feels too "hippy" for a clinic
  receptionist. Need voice audition and a more professional default.
- **Sohee still has a Korean/English transition rough edge.** In the better Sohee re-test, user feedback
  called out one long lag after a Korean input, and when the user switched back to English the robot
  still responded in Korean. We do not know yet whether this was language-mode persistence, a delayed
  response to stale Korean input, or both. Sohee is not rejected, but it is not accepted as the final
  clinic voice yet.
- **STT still mishears some speech.** During live chat, misheard phrases affected quality. Examples
  observed in the conversation/logs include `voice`→`wife`, `staff`→`STEM`, and `Dr. Park`→`Dr. Punkier`.

### 🔴 Bad / unresolved live feedback
- **Before the idle-motion fix, the robot kept moving.** In `goodbye-20260612-1540`, user feedback:
  it triggered goodbye then welcome and kept moving its antenna/head. That was fixed enough for the
  later greet/goodbye test, but the physical movement policy still needs careful validation.
- **Wave/chat UX regressed in the Sohee run.** User feedback on `voice-sohee-20260612-140938`: trying
  to wave and trigger chat mostly produced welcome/goodbye; later summarized as "wave triggered
  welcome/goodbye twice." We should not assume root cause yet.
- **Chat stopped for no clear reason in the Sohee run.** User feedback: "chat stopped for no reason."
  Artifact notes can help debug, but the live UX requirement is clear: the robot must make conversation
  state obvious and should not silently fall out of chat.
- **Tool-call path produced a visible chat error.** During the Sohee run, the backend called `move_head`
  and the user-facing conversation received an error. This was observed live/logged, but root cause is
  not investigated yet.

### Supporting artifact notes
- All official-runtime live runs saved useful artifacts on m1max: input/output WAV, realtime JSONL,
  policy JSONL, capture JSONL, video, and manifest where enabled.
- In `voice-sohee-20260612-140938`, policy logs show wave events did occur, but the user-facing result
  was still confusing because welcome/goodbye also fired during the overall attempt to start chat.
- Treat artifact analysis as supporting evidence only; the primary source for UX quality here is the
  live feedback above.

### Next
- Preserve the above as live feedback first. Do not jump straight to policy changes from artifact
  snippets.
- Add more backend visibility if the provider exposes it: backend-side VAD/speech boundaries,
  transcript-finalization timing, response creation, audio-generation timing, model/voice/session
  metadata, cancellations, and backend error payloads.
- Build a review timeline that aligns user feedback, policy events, realtime events, output audio, and
  video frames.
- Then run controlled A/B tests: wave-only, approach/depart-only, voice audition, antenna neutral
  calibration, and clinic-fact latency repro.

---

## 2026-06-10 — live daemon sessions; raw-audio review diagnosed next day (2026-06-11)

**Setup:** m1max + real robot. Two useful recorded daemon sessions were inspected offline on
2026-06-11 with the new `review_audio` utility:

- `20260610-141813-89a059` — Pydantic/OpenRouter brain using `openai/gpt-oss-20b`; voice was
  manually turned on as always-on (`conversation=False`) while perception/gestures/record/capture/
  raw-audio were also running.
- `20260610-145250-1a7624` — `claude -p --model haiku`; wave→conversation style session, voice
  mostly off outside conversation windows; same raw-audio/video/capture/turn artifacts enabled.

### 🟢 Good
- **Raw audio + turn review harness is now actionable.** The run artifacts synced locally and the
  review utility generated `review.md`/`review.csv`/`review.json` plus exact-turn/context/wide WAVs
  under `artifacts/reviews/<run_id>/`. This made it possible to separate raw mic quality, VAD/queue
  timing, STT quality, and brain behavior instead of relying only on live impressions.
- **The Haiku/wave-chat run has healthy raw mic continuity.** `145250` recorded 959.38s of audio over
  965.32s wall time (audio/wall ≈ 0.994), median chunk gap 20ms, and only 28 gaps over 100ms. User
  review: the clips are much clearer and do **not** show the choppy-audio problem seen in the first
  run.

### 🟡 Ugly
- **STT is still not reliable enough even with clear input.** In the clearer Haiku run, user review
  flagged omissions/mistranscriptions around turns 05/06/13, and turns 03/04 split one realistic
  human turn into two separate VAD/STT turns. Cleaner audio helps a lot, but faster-whisper/VAD
  still need tuning or replacement for receptionist UX.
- **Turn timestamps are not UX timestamps.** `turns-*.jsonl` writes after brain response / robot
  speech, so it can lag the actual user utterance by ~10–40s. The review utility now signal-matches
  turn WAVs back into the continuous raw WAV to recover the real audio timing.

### 🔴 Bad / diagnosis
- **First recorded session's raw mic audio is choppy / time-compressed relative to wall time.**
  In `141813`, the WAV contains 1301.98s of audio over 1721.53s wall time (audio/wall ≈ 0.756):
  about 419.5s of live time is not represented by recorded samples. Median chunk gap was 27ms for
  20ms chunks, with 1109 gaps over 100ms. User review of the clips matched the live UX: choppy sound
  and nonsensical STT (`heard` text not close to what was said).
- **Working hypothesis, not proven root cause:** the bad first session is consistent with audio I/O
  starvation or WebRTC mic-delivery stalls under single-process load. That run had always-on voice
  plus perception/MediaPipe, raw recording, video/capture, STT, brain calls, and TTS playback active
  continuously. The clearer Haiku run was more gated: voice mostly ran only during wave-triggered
  conversation windows, and vision pauses while the robot is speaking. This points toward resource
  contention as a plausible cause of both choppy input recordings and choppy robot output, but it
  still needs a controlled A/B before being treated as settled.

### Next
- Live-validate the first-pass STT-worker transcript stream (implemented 2026-06-11 offline):
  timestamped utterance queue, separate lower-priority STT process, ordered transcript batching,
  transcript JSONL, and per-utterance WAVs with `--save-turns`.
- Watch queue age/backlog counters and audio continuity to see whether STT isolation reduces stale
  utterance behavior without reintroducing choppy mic/playback.
- Add VAD merge logic for adjacent utterances separated by very short gaps.
- Re-test STT alternatives/tuning on the clearer Haiku clips, not on the corrupted/choppy first run.

---

## 2026-06-09 — head recalibration; pydantic brain live; streaming-TTS rejected; VAD endpointing FIXES the STT garble; turn-capture for debug

**Setup:** m1max daemon, many restart cycles. Robot recalibrated mid-session via the Reachy app.

### 🟢 Good
- **VAD endpointing (Silero) + `medium` STT → clean single-utterance turns.** Each `heard` is now ONE
  complete utterance (1–5s, speech-start→silence) instead of 1.5s fragments or 15s multi-speaker blobs.
  Validated over a ~14-turn live conversation — natural flow, brain handled clean input well (it even
  caught its own ambiguity: "I said friendly *faces*, not friends"; privacy guardrail on patient names).
  The STT garble that wrecked earlier conversations is fixed at the *listen* layer (silero-vad +
  utterance-queue `listen_read` + voice-loop rewire; uncommitted).
- **Pydantic-AI brain runs live on the robot** (step D) — gpt-oss-20b via OpenRouter, wave→conversation,
  in-character, memory; **no keychain/tmux hack**. m1max got `pydantic-ai-slim[openai]` + the `.env` key.
  See `docs/archive/legacy/brain-backend-research.md`.
- **Head recalibration (Reachy app) leveled the head** (roll 7.5°→~0°); `reset` is deterministic
  (body yaw exact ±0.2°). See `docs/head-pose-calibration-notes.md`.
- **`--save-turns` debug capture built** — per-turn utterance WAV + heard/reply → `artifacts/turns/`,
  to attribute off replies to STT vs brain.

### 🟡 Ugly
- **gpt-oss-20b latency spikes** — mostly 1–3s/turn, occasional 9–14s (its measured spikiness).
- **`medium` STT ~2s/utterance** — accurate but slow; `turbo`/`small` would be faster (→ STT-replacement
  research underway; this model is the suspected weak link).
- **STT still mis-transcribes short/fast/mumbled speech even with VAD** (e.g. "Also they're going" → an
  off reply). The brain mostly reacts correctly to bad text, so **STT is the weak link**.
- **Head orientation ~±6–7° non-repeatable** (matrix-confirmed); body exact. Use the 4×4-matrix API,
  not euler. See `docs/head-pose-calibration-notes.md`.

### 🔴 Bad → resolved / rejected
- **Streaming TTS = choppy → REJECTED.** Chunked render-ahead over the WebRTC pipeline starves the audio
  thread → "choppiest voice ever." Kept whole-utterance `speak()` + the thinking-antenna mask. See
  `voice-ai-research.md`.
- **STT garble (no endpointing)** → root-caused + **FIXED** by the VAD endpointer (above).

### Process notes (mine)
- Jumped from VAD code straight into a live mic test without confirming someone was there → captured the
  room's **noise floor** (±0.04 RMS) → wrongly concluded "the Reachy mic runs quiet" + added a gain hack.
  A real speech test (after asking) showed the VAD works at the **default threshold, no gain**. *Lesson:
  ask before live tests that need the user; don't conclude from data taken in the wrong conditions.*

---

## 2026-06-08 (eval, not on-robot) — Brain backend trial: `agy` (Antigravity CLI) + Gemini 3.5 Flash → REJECTED

**Context:** Evaluated swapping the conversation brain from `claude -p` (Haiku) to **`agy`** (Google's
"Antigravity" agentic CLI) on **Gemini 3.5 Flash**. Dev-machine only (agy isn't on m1max yet);
**`brain.py` was NOT changed** — throwaway tests in `/tmp`.

### 🔴 Decision: do NOT adopt — keep the `claude -p` brain.
**~2–3s/turn slower with no offsetting benefit, on a brittle CLI.**

### Findings (kept in case revisited)
- **Latency:** bare `agy -p` ≈ **3–5s/turn** (default ≈ Low ≈ ~3s — the tier override does *not* speed
  it up). Current **claude/Haiku** ≈ **1–2s/turn**. agy is slower because it has **no persistent
  process** — each turn = a fresh CLI spawn + model call (claude spawns once and reuses it).
- **Derails on ANY flag.** `--model`, `-c`/`--continue`, `--print-timeout` each make agy introspect its
  own invocation (lists files, opens its sqlite conversation DBs, documents its own flags) instead of
  replying. **Only bare `agy -p "<prompt>"` chats reliably**; bound runtime with a `timeout` *wrapper*,
  never `--print-timeout`.
- **Model:** default is already **Gemini 3.5 Flash** (self-reported). Tier *maybe* settable via env
  `CASCADE_DEFAULT_MODEL_OVERRIDE` (sets cleanly but unverified it changes the tier — the self-report is
  a baked-in "Antigravity powered by Gemini Flash" identity, so it can't confirm).
- **Memory:** agy's own `--continue` persists conversations (sqlite under `~/.gemini/antigravity-cli/`)
  but derails; **client-managed history** (persona + transcript embedded in each bare prompt) is clean
  and **recalls correctly** across turns — the approach we'd have used.
- **Quality (when bare):** clean, in-character, fact-grounded (recalled "Dr. Park"; gave restrooms +
  hours from the embedded facts). The model is fine — the **CLI** is the blocker.

**If revisited:** a direct **Gemini API** call (`google-genai`: system prompt = persona, no tools,
managed history) would sidestep the CLI fragility entirely and could be faster — but that wasn't the ask.

---

## 2026-06-08 — Phase C voice loop + wave→conversation LIVE-validated; greet/goodbye low-fire-rate root-caused to a dropped fps flag; conversation startup-lag reduced + "thinking" antenna UX

**Setup:** robot recovered (was in `state: error` "Motor communication error" overnight; the user
restarted it + updated its SDK to **1.8.0**). Our SDK was 1.5.0 → version mismatch → our daemon's
`ReachyMini()` failed; **matched to 1.8.0** and it connected. Daemon launched **from a GUI-rooted
tmux session** so `brain.py`'s `claude -p` is keychain-authed (the Phase C auth fix).

### 🟢 Good
- **Phase C voice loop validated (first pass).** listen → faster-whisper STT → `claude -p` (Haiku)
  brain → speak. Fact-grounded + in-character (e.g. restroom location verbatim from `clinic_facts.md`,
  no hallucination on absent departments). claude -p authed via the tmux daemon.
- **Wave → conversation wired + validated end-to-end.** wave (`Open_Palm`) → `start_conversation`:
  opener spoken → voice/brain loop (conversation mode) → multi-turn → **interaction gate** (daemon
  suppresses `react`/`farewell` while a conversation is active — proven: `react: suppressed`) →
  **idle close** (`conversation ended (idle 46s)`) / 480s max cap.
- **Conversation startup-lag reduced + made to *feel* responsive (afternoon session).** Three
  fixes, live-validated: (1) **brain prewarm** — the `claude -p` process is spawned at the start
  of the voice worker so the FIRST reply no longer pays cold-start; live per-turn `heard→reply`
  dropped ~3s → **1–2s**. (2) **Opener audio cushion cut** 1.0s → 0.4s (`_play_speech` prime) —
  opener "feels quicker" off the wave (the opener is already pre-rendered, so the cushion was its
  dominant latency). (3) **"Thinking" antenna animation** — antennas sway during the `heard→reply`
  dead time (brain call + TTS synth) and settle to neutral the instant reply audio starts (gated
  on `session._speaking`, so motion flows straight into speech). User: **"much better UX."**

### 🟡 Ugly
- **Per-turn latency is structural (~4–6s) — now *masked*, not removed.** Traced the `you stop
  talking → you hear the reply` gap to a **serial 4-stage pipeline**: mic-poll wait (≤1.5s — the
  loop reads the mic on a fixed `--voice-interval` timer, no endpointing) + STT/faster-whisper
  (~1s) + brain/Haiku (~1.5–2s) + TTS-start synth+cushion (~1s). No single villain — it's the sum.
  The thinking-antenna fills the dead air so it *reads* as "thinking," and prewarm/cushion shaved
  the worst, but the real fix is **VAD endpointing + a streaming STT/LLM/TTS stack**
  (see `voice-ai-research.md`) — deferred.
- **STT quality variable** — clean when close/clear single utterances, garbled when continuous/far.
- **Wave needs a min distance** (scores hover near the 0.5 floor).

### 🔴 Bad → root-caused + fixed: greet/goodbye **fire rate very low**
- Symptom: after restarts today, greet/goodbye barely fired; the visit-state trace showed `greet`/
  `depart` latches **stuck True for ~7 min** without re-arming.
- **Root cause = a dropped flag, NOT the code/position.** The tmux launches I wrote **omitted
  `--vision-interval 0.2`**, so the daemon ran at the **2.0s default = 0.5 fps** (yesterday's working
  daemon was 5 fps). `reset_absent` is **40 frames** → at 0.5 fps that's **~80s** of continuous
  absence to re-arm (vs ~8s at 5 fps), so visits almost never reset → latches stuck → low fire rate.
- **Fix:** restored 5 fps; at 5 fps the RESET fires (`visit RESET (absent 40 frames)`) and greet/
  goodbye work. **Changed the default `--vision-interval` 2.0 → 0.2** so a launch without the flag
  can't silently break it again.
- **Process note (mine):** I first over-asserted the *visit-latch design* as the cause. The user
  correctly pushed back ("code unchanged, worked yesterday; I moved its position") — the real
  variable was the frame rate. Confirmed via the record-fps hint ("~0.5 fps"). *(Don't assert a
  root cause when an unchanged-code symptom appears — find what actually changed.)*

---

## 2026-06-07 — wave detection LIVE-validated; recording/persistence hardened; greet/goodbye FP confirmed on record; Phase C auth pinned

**Setup:** m1max daemon (`serve --perception --gestures`), vision + stream on, alert engine.
Several restart cycles. Recording switched to `.mkv`. Head re-leveled each restart.

### 🟢 Good
- **Wave detection (Feature 2) works end-to-end live.** MediaPipe **Gesture Recognizer**
  (`Open_Palm`) → debounced `wave` event → alert engine (`wave → wave_back`) → robot says
  **"Hi there!"** (deliberately distinct from the greet). Detected at score **0.61–0.73**.
  New `gesture.py` + `perception --gestures` + `wave_back` command; mediapipe installed on
  m1max without breaking the RF-DETR/supervision stack (numpy 2 kept).
- **Recording persistence hardened (A/B):** `daemon.stop()` now finalizes record+capture, and
  the daemon writes a **durable** log to `artifacts/logs/` (not `/tmp`). Verified: stop *while
  recording* → 581-frame clip **READABLE** (pre-fix it was left corrupt).
- **`.mkv` recording (crash-resilient):** container mp4→**mkv**, same `mp4v` codec & size. A
  hard kill / battery-off now keeps footage up to the crash (mp4 = total loss — no `moov`).
  Proven empirically (truncation-survival test); per-frame extraction identical to mp4.
- **Head-stable greet/goodbye:** removed `look("center")` from `_express` — reactions no longer
  move the head, so the camera (which rides on the head) keeps a level frame.
- **First real eval dataset:** `video-153822.mkv` (1191 frames) + aligned `capture-153822.jsonl`
  — **4 approach / 4 depart / 11 wave**, with the misfires below baked in.
- **Phase C auth pinned + dev workaround works:** `claude -p` over plain SSH fails (keychain),
  but a GUI-rooted **tmux `claude-test`** runs it authenticated (verified `OK`/exit 0).

### 🟡 Ugly (acceptable / needs refining)
- **Wave needs a minimum distance** — MediaPipe needs the hand a min size in-frame; a wave from
  across the room won't register (scores hovered near the 0.5 floor). Characterize the working
  range / lower the threshold later.
- **Head roll mis-calibration (~8°)** — commanding "level" (roll 0) physically sits ~8° tilted.
  It's a *robot calibration offset*, not our code (motors fine, head responds; commanding
  roll ≈ **−5.7°** levels it). Matters because the camera is head-mounted (tilts every frame).
  Proper fix = recalibrate; **deferred**. `reset` currently commands true-zero, so it re-tilts.
- **New voice lines:** greet "Welcome!", goodbye "Goodbye! Have a nice day!",
  wave "Hi there!".

### 🔴 Bad (clear issue)
- **Greet + goodbye misfire on a stationary, *interacting* person — confirmed on record.**
  During the wave test (same person, id=1, standing + waving):
  - false **approach** (area grew to 0.385 as you stepped in -> "Welcome!"),
  - false **depart** (area dropped to 0.284 ≈ 0.6 × peak from a **pose change / arm-raise**
    narrowing the box → "Goodbye!") — **you never left.**
  Same over-sensitive visit logic as the sitting-fidget FP. User: greet/goodbye were "messy —
  misfire *and* non-fire both happened." This is the priority correctness issue.
- The yesterday "fire-then-no-fire" bug wasn't re-diagnosed in isolation today — but we now have
  the **eval framework + real datasets** to dissect it with data instead of guessing.

### Next — the eval framework (agreed; see `docs/archive/legacy/plan-reception.md` -> Testing strategy)
record → annotate → **auto-label (model proposes, human verifies)** → `score` → iterate. Build
`score` + auto-`label`; run on `video-153822.mkv`. Fix candidates (validate via the framework,
don't ship blind): **interaction gate** (suppress greet/goodbye while waving), **depart
robustness** (larger/sustained recession), **DetectionsSmoother**.

---

## 2026-06-06 — Phase B: vision → approach → greet (m1max + real robot)

**Setup:** m1max drives the daemon over SSH. `serve --perception --vision-interval 1`
+ `alert_engine`; robot on WiFi (192.168.1.165); m1max SDK aligned to robot daemon
**1.5.0**. Audio = piper TTS streamed to the robot speaker over WebRTC.

**What we did:** empty-room baseline → stand in view (presence) → walk up (approach) →
robot greets. Iterated hard on audio quality. Added a `capture on/off` debug recorder
and an A/B (vision on vs off) on the audio.

### 🟢 Good
- **Phase B works end-to-end on hardware:** real camera → RF-DETR person detect →
  ByteTrack → approach geometry → `events.jsonl` → alert engine → robot greets
  (look + antenna flick + speak).
- **Presence vs approach holds live:** standing still in view = detected but **no** greet;
  walking up = greet.
- **Clean detection:** zero false positives in an empty room; reliable 1-person detection.
- **Version alignment:** m1max SDK → 1.5.0 matched the robot; audio/video warm up healthy.
- **Daemon control plane solid:** serve / vision on·off / react / capture / shutdown all
  reliable on hardware.

### 🟡 Ugly (acceptable, needs refining)
- **Approach fires on the 2nd walk-up, not the 1st** ("twice"). Cause: growth measured
  from *first-seen* + ByteTrack keeping the track id alive across a step-out → stale
  baseline. **Fix applied** (baseline = track's farthest point / min-area; `growth_factor`
  1.6→1.3, `min_dwell` 5→3) — **needs one clean re-test.**
- **`react`'s `look("center")` re-aims the head**, undoing a head-tilt setup → camera
  loses the approach area. **Workaround:** orient the robot *body* at the path so
  center = path. Refine: configurable home-look, or don't re-center on greet.
- **Audio buffering was fragile** — start-clip ("he" of "Hello") and tail-drop
  ("…someone will") both happened and were **fixed** (0.3 s silence lead-in; prime+pace
  cushion ~1.0 s). Took many iterations; the "pause vision during speak" guard we added
  turned out to treat a **non-cause** (see Bad #1).
- **Version matched by downgrading m1max** to 1.5.0; the cleaner direction (upgrade the
  *robot* to latest via the Control app, then re-match the SDK) is deferred.

### 🔴 Bad (clear issue / failed expectation)
1. **Spoken greeting intermittently choppy / not continuous.** OPEN.
   - **A/B proved it's vision-independent** (vision on vs off: no difference) — so the
     "pause vision while speaking" guard is not the fix.
   - Measured WiFi jitter m1max→robot: 0 % loss but **3.8–37.6 ms, σ≈9.6 ms**.
   - The pipeline **buffers** our audio (over-pushing overflowed it → the tail-drop), so
     send-side pacing isn't the bottleneck → **leading suspect: the WebRTC-over-WiFi
     stream underrunning the robot's receive buffer.**
   - **Correct next isolation test (NOT yet done):** SSH to the robot
     (`ssh pollen@reachy-mini.local`, pw `root`) and play a WAV on the **robot's own
     audio device** (`aplay`), bypassing WebRTC. Smooth there → it's the stream/link;
     choppy there → the robot's audio device. *(The earlier "play on m1max" idea was
     useless — m1max's audio path was never in question.)*
2. **Phase C (voice brain) not tested at all** — blocked: m1max has no `claude` binary
   (no Node). Decision pending: install Claude Code + auth on m1max, **or** switch the
   brain to the Anthropic API + key.

### Carried into backlog
- **Separate-process architecture for perception** (own OS process fed frames via shared
  memory → `events.jsonl`; removes GIL/CPU contention by design, vision never pauses).
  Recorded, not done.
- Landed this session: `reception capture on/off` + per-frame approach debug
  (`area / min_area / growth / dwell / near / approaching / fired`); `reception`
  console-script entry point; `[vision]` pyproject extra.

### Update — same session, after fixing the capture tool
- 🟢 **"Twice" confirmed resolved** — repeated far→near runs detect approaches reliably
  (e.g. capture `092447`: a stationary person at area ~0.20 correctly never fired, while
  the approacher grew 0.018→0.063 and fired on crossing near). Capture tool's `float32`
  JSON-serialization bug found + fixed + verified.
- 🟡 **Greet timing feels slow** — you have to get close before it starts. Fires at ~6 %
  of frame but the react latency (look + TTS) makes it feel late. One-knob tune
  (`min_area_frac` and/or react latency).
- 🔴 **Sitting-fidget false positive** — a stationary person moving hands/head can trip
  the min-area-baseline growth (box wobble reads as "approach"). The min-baseline that
  fixed "twice" is the cause; needs a *sustained/smoothed* growth signal (or VLM later).
- 🔴 **Audio choppy persists even on manual `reception react`** — reconfirmed
  vision-independent. The robot-local `aplay` isolation is still the pending diagnostic.

### Method takeaway → new testing strategy
We spent this whole session hand-tuning approach logic *on the robot in real time*. Going
forward: **Stage 1 semi-live (video-driven) → Stage 2 live** — see the Testing-strategy
section in `docs/archive/legacy/plan-reception.md`. The false-positive and threshold cases above become
labelled scenario clips so they're reproducible and regression-tested off-robot.

---

## 2026-06-06 (semi-live, video harness) — Feature 1: departure → "Goodbye"

First real use of the Stage-1 harness (`python -m reachy_mini_brain.replay`). **No robot.**

### 🟢 Good
- **Harness works** — pumps a recorded clip through the real perception pipeline and
  reports approach/depart events. Flags: `--trace` (per-frame stats), `--reverse` (play
  backwards), `--expect-approach/-depart N` (CI asserts).
- **Approach** — `video-094157.mp4` forward → `approach=1, depart=0`.
- **Depart** — same clip `--reverse` (receding) → `approach=0, depart=1` (fired at area
  0.043, below half its peak). Clean separation, and we validated departure with **no
  "leaving" clip and no robot** by reversing an approach clip.
- **Reaction wired** — alert engine maps event type → action (`approach`→`react`,
  `depart`→`farewell` = "Goodbye, have a nice day!"), independent per-type cooldowns;
  daemon gained a `farewell` command + `reception farewell` CLI.

### ⏳ Pending
- New code (farewell + mapping) **not yet loaded** — deliberately did NOT restart the
  daemon (an hours-long user recording was running). Loads on next restart.
- **Live test** of depart→goodbye (walk away → robot says goodbye) needs a person present.
- Record real `leaving` + `waving` clips for the labelled scenario suite when back.

---

## 2026-06-06 (later) — Feature 1 departure: rebuilt id-agnostic + goodbye LIVE-validated

After the per-track depart proved fragile live (track id churns when you turn to leave →
peak reset → fired at the door / not at all), rebuilt departure **id-agnostic**: track the
dominant visitor's area envelope, fire when it drops to ~0.6× the visit peak (2 sustained
visible frames), survive the ~4s close-range **blind spot** (the camera loses you when
you're right at the desk — confirmed: the gap frame is an empty room).

### 🟢 Good
- Tuned + validated **offline** on 3 clean walk-away clips (`134128/146/202`): depart fires
  at 39–48% of peak, **approach never falsely fires** — proving departure is independent of
  walk-up (no walk-up in those clips).
- **Goodbye live-validated** on the robot (goodbye-only, no greet bundled): fires reliably
  on a real walk-away.
- New harness flags landed: `--reverse`, `--from-frame`; alert engine `--types` filter.

### 🟡 Ugly
- Goodbyes are **spaced ~15s** (visit re-arms after ~8s absence + 15s alert cooldown), so
  rapid back-to-back walk-aways get one goodbye. Fine for a desk; a "re-arm on every
  leave+return" refinement would remove the spacing need — needs a multi-cycle clip to
  validate offline first (didn't change it live).

### 🔴 Bad / finding
- **Close-range blind spot**: the camera can't see a person right at the desk (~4s gap).
  OK for greet+goodbye (both happen in view) and for a conversation (audio), but means the
  robot is vision-blind during the close interaction. Camera FOV/angle is the lever.

---

## 2026-06-06 (later still) — gated greet; greet + goodbye both LIVE-validated

Rebuilt the greet to mirror departure — one id-agnostic gated state machine on the
dominant visitor's area envelope:
- **Gate 1** — a new visitor is present (a visit starts).
- **Gate 2** — area rising + grown from entry **and** reached `greet_floor` (0.10), i.e.
  clearly approaching and in the area (not a distant speck). Greet has no stable
  reference like departure's peak, so it needs that one small floor; depart stays
  peak-relative (fires at ≤ `depart_factor` × the visit peak).

### 🟢 Good
- Offline on 4 walk-up + 3 walk-away clips: walk-ups fire **greet only** (~0.11–0.18),
  walk-aways fire **goodbye only** (~half peak). **No cross-firing.**
- **Live: both greet and goodbye work** (walk away → goodbye, walk back in → greet).

### 🟡 Ugly / to tune
- **Voice fires ~0.5–1s late** — a visitor could miss it. Likely the react chain:
  alert-engine poll (0.3s) + `look("center")` goto *before* the speak + TTS synth latency.
  Tuning ideas: **speak first / move concurrently**, faster alert poll, and
  **pre-synthesize** the fixed greeting/goodbye lines. Deferred.
- `greet_floor` (0.10) sets greet timing — one knob, lower = greet sooner/farther.

---

## 2026-06-06 (evening) — OPEN BUG: greet/goodbye "fire then no-fire"

**Symptom (corrected by the user):** greet + goodbye fire correctly for a stretch, then
**stop firing entirely** within the same session ("fired, then no fire"). Recurs. Last
real triggers were 15:20:01 in `events.jsonl`, then nothing for >1h. The **live stream
stayed up the whole time — video was NOT dead.**

### 🔴 Bad — the bug (unresolved; collect data tomorrow)
- `events.jsonl` *stops getting new events* → the **tracker stops emitting**, not the
  alert engine. In `approach.py`, greet/goodbye each fire **once per "visit,"** and a visit
  only re-arms after the person is **absent ≥ `reset_absent` (40 frames / 8s)**.
- **Theory (to confirm, NOT asserted):** if anything keeps a detection alive ≥
  `present_frac` (0.03) — a lingering person, a false-positive, or the head pointed at a
  mis-detected object — the visit **never resets**, both latches stay stuck, nothing fires
  again.

### 🟡 My process failures this session (do not repeat)
- **Misread `video_ready:false` as "video dead."** It's a single `try_pull_sample(20ms)`
  that *consumes* a frame from the GStreamer appsink → returns None routinely (between
  frames, or when the vision thread just popped it). NOT an aliveness check; the live
  stream proved frames were flowing.
- **Overwrote the logs** by `rm`-ing `/tmp/reception_live.log` on every restart → no trace
  left to diagnose. **Fix: restart with a timestamped log file, never `rm`.**

### Instrumented (loads on next restart)
- `approach.py` now logs `visit RESET …` on each re-arm + a throttled
  `visit: dom=… absent=… greet=… depart=…` every ~5s. The failure will show whether the
  visit is stuck (greet/depart=True while `absent` never climbs = something pinning a
  detection ≥ present_frac).

### Plan for tomorrow (user present)
1. Restart with a **timestamped log** + `capture on` (and A/B the stream on vs off per the
   user's suspicion).
2. Run cycles until "fire then no-fire" reproduces.
3. Read the `visit:` trace at the failure point → confirm/refute the stuck-latch theory and
   find *what* is pinning the detection. Then fix (candidates: re-arm on leave+return,
   raise `present_frac`, or a max-visit timeout) — validated on a recorded clip first.

> A stateless rewrite was attempted + then **rolled back** — it was a fix on an *unconfirmed*
> root cause and it overwrote the diagnostics. Code stays on the instrumented visit-based
> version until tomorrow's trace confirms the actual cause.

### Code review (no code changes) — two compounding bugs to verify with data

**Bug A — a walk-away can fire a phantom GREET, then goodbye.** Leaving the desk means
stepping back INTO frame from the close blind spot, so the box *grows* (more of the body
becomes visible) before it shrinks. The greet test is `area / visit_min ≥ growth_factor`,
and `visit_min` is the *smallest area this visit* = the tiny step-into-frame sliver. So
"grew 2.5×" isn't approaching — it's just becoming visible → false greet; then the recede
fires goodbye. (Also: `visit_min` is permanently poisoned by ANY single small/partial/noisy
detection.) The 3 recorded walk-away clips don't repro it — their step-in growth was ~1.23×
(< the 1.3× threshold); the live one started closer to the desk → bigger grow-in → crossed it.
- **Data check:** record a walk-away that STARTS right at the desk (through the blind spot);
  replay it — does it emit a spurious `approach`?

**Bug B — "no fire after" = the visit never resets** (separate bug; A just burns both latches
at once). A greet+goodbye sets BOTH `_greet_fired` and `_depart_fired`. They re-arm only via
`_reset_visit()`, called only when `_absent ≥ reset_absent` (40 frames = **8s of CONTINUOUS
no-detection**; any detected frame resets `_absent` to 0). So after the combo, nothing fires
again until 8s of clean absence. Two ways that never happens (need the trace to tell which):
  1. **cadence** — up/away/up/away keeps someone intermittently detected, so `_absent` never
     accumulates 8s.
  2. **phantom detection ≥ present_frac (0.03)** — e.g. greet/goodbye `look("center")` leaving
     the head on an object RF-DETR misreads as a person → `_absent` pinned at 0 → permanent lockup.
- **Data check:** read the instrumented `visit: … absent=N …` trace at the no-fire point.
  `absent` climbing toward 40 → it's #1 (cadence). `absent` stuck at 0 while you're gone → it's
  #2 (phantom — also watch the live stream for where the head/camera is pointed).
