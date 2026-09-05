# Reachy Mini Robot Runtime Debugging

This note captures how to inspect and control the robot-side runtime during live tests. It is separate
from the refactor plan so it can be used as an operational checklist.

## Why This Matters

Recent official-runtime live tests suggest some failures may live on the robot runtime path, not only in
the m1max app or realtime backend:

- Backend-generated Sohee response WAVs sounded clean when played locally.
- The same audio could sound low, variable-volume, or partially disappear on the robot speaker.
- Playback plus movement/wobbling reproduced the symptom in one diagnostic run.
- A 2026-06-15 dry test, without a full realtime conversation session, replayed a known Sohee WAV over
  WebRTC while wobbling and small head/antenna moves were active. User feedback: output became completely
  choppy, not merely low/high volume.
- A 2026-06-16 isolation pass split the issue: robot-local ALSA playback of the same WAV was clean,
  m1max -> robot WebRTC playback with the old `0.9x` sender pacing was choppy, and exact monotonic
  realtime pacing was smooth.
- After a robot restart, the same full runtime path sounded clean again.
- Bad/rough runs sometimes logged robot control instability such as:

```text
Failed to set robot target: Lost connection with the server.
```

Current interpretation: the first confirmed root cause for the dry WAV playback choppiness is sender
overfeed on the m1max WebRTC path. The legacy direct playback loop sent 320 samples every ~18ms instead
of every 20ms. This can make playback sound choppy even when the WAV, robot audio hardware, and WiFi are
good enough. Robot-side runtime state, movement/wobbling, and network jitter can still be secondary
contributors, but check sender pacing first.

## Ambient Noise And Server VAD

An August 28, 2026 comparison suggests elevated ambient noise can disrupt server-VAD segmentation even
when the recorded speech remains intelligible to STT:

- `official-live-20260828-134642` recorded the later user phrases, and direct Parakeet transcription
  recovered them, but the live backend emitted only one speech-start/stop pair and one final transcript.
- After the nearby window was closed, `official-live-20260828-142205` used the same runtime setup and
  emitted 11 complete speech-start/stop and transcript cycles.
- The failed run's low-level input-audio floor was approximately 6-7 dB higher than the quiet repeat.
- Replaying the failed input through the unchanged VAD reproduced the missed turns; resetting the VAD
  state during an offline experiment recovered additional segments.

This is evidence for a noise-sensitive, stateful VAD failure mode, not yet proof that ambient noise is
the sole cause. Keep the raw input WAV and realtime events, compare per-frame VAD probabilities under
controlled noisy and quiet conditions, and confirm that direct STT can recover any allegedly missed
speech before changing VAD thresholds or reset behavior.

A separate observed edge case can make a conversation appear to stop and restart even though the
same backend session remains connected: progressive Parakeet output contains recognizable words,
but final transcription returns empty, and the runtime's cue path treats that completion as a new
thinking transition. Noise-sensitive endpointing and empty-final cue behavior are accepted
limitations for the first assisted production pass. A future improvement should replay retained raw
input, measure VAD probabilities and empty-final frequency, then evaluate bounded VAD-state reset and
empty-final cue suppression. Do not change thresholds or reset behavior without that comparison.

The failed run's two `session.created` events were expected: policy greet/goodbye TTS used the startup
connection, so the runtime opened a fresh S2S connection at the visitor-conversation boundary. The
second connection successfully handled the opener and first user turn; it was not a failed reconnect.

### September 5: Missed Speech After News Playback

**Status:** unresolved and deferred by the operator on 2026-09-05. No VAD model, threshold, state-reset,
or microphone-processing change was approved. Track under [TODO 7c](todo-official-runtime.md).

Run `official-live-20260905-103441`, first chat session
`session_cc4cb214e99c49ec966bcb889f0524eb`, used app `37c7042` and backend `2e4449c`.
After the local-news answer, follow-up speech remained in the microphone recording but produced no
accepted live speech-start event or final transcript. All times below are EDT:

- At `10:44:57.238`, TTS finished generating and released its MLX lock. At `10:44:57.250`, the
  response completed and the backend explicitly enabled listening. These are generation/transport
  events, not physical speaker-playback completion.
- At `10:45:08.709`, VAD discarded one 852 ms segment containing only 256 ms classified as active
  speech, below its 384 ms minimum. This does not measure the duration of the user's actual speech.
- No other backend activity, errors, cancellations, or disconnects were logged before the app's
  `10:45:30.387` idle timeout. Microphone frames continued to be recorded and forwarded until that
  timeout closed the app audio gate. The backend connection remained open until the next chat.
- The review app's VAD lane contains accepted public speech events, not rejected candidates or
  per-frame classification. An empty lane cannot establish that the backend stopped consuming audio.

The original INFO-level log lacks VAD input-consumption summaries (currently DEBUG-only) and
per-frame speech probabilities. The discard establishes VAD activity at that instant, not continuous
processing of every forwarded frame. Noise, weakened post-microphone-processing speech, accumulated
VAD context, framing, and unobserved delivery/processing problems remain hypotheses, not proven causes.

Offline evidence:

- Fresh isolated VAD/STT replay of the 25-second post-cutoff clip recognized `So according to what?`
  and `Hello?`. Including the preceding playback period in a 42-second clip still recognized the
  first phrase but rejected the later fragment. Neither replay reconstructed the full live state.
- A continuous replay from the news request through idle cutoff sent all 2,492 original microphone
  chunks through the running new backend, preserving relative arrival timing. It recognized
  `According to what?` and responded; it rejected the later fragment with 128 ms active speech below
  384 ms. However, the initial STT heard `No cool news` rather than `local news`, so no web search
  occurred and the response/timing differed. This did not reproduce the exact news-readout failure.
- m1max's cached Silero model checksum matched official v6.2.1. The upstream README's v5 label is
  not a reliable runtime version: both loaders use the cached `snakers4/silero-vad:master` resource.

Evidence is retained under `artifacts/diagnosis/official-live-20260905-103441/`, including
`original-backend-service-snapshot.log`, `microphone/README.md`, and
`continuous-endpoint-01/README.md`; original artifacts are under `artifacts/official-runtime-live/`.
Raw artifacts remain outside Git and subject to the existing retention workflow. The separate news
TTS truncation is not evidence that VAD was deliberately disabled during speech.

### September 5: News TTS Cutoff

**Status:** unresolved generation-limit risk; no limit or dependency change approved.
The later Sohee instruction change is a delivery preference, not a confirmed fix.

In the same run, response `resp_44b96d6f05954d408f534218612b2e46` included the ending
"according to New Jersey high school sports" in its text. The operator heard both
the retained WAV and live speaker stop after "according to New Jersey high".
Transport reported completion without cancellation/error; retained audio was
237,568 samples at 16 kHz (14.848 seconds).

The pinned mlx-audio 0.4.2 CustomVoice path caps generation at
`min(requested_max_tokens, max(75, text_token_count * 6))`. The complete news text,
including "school sports", had 31 text tokens: the handler's 360-token request
was reduced to 186 codec steps, about 14.88 seconds at 12.5 steps/second. This close
match supports a generation-ceiling explanation, but the retained trace does not
explicitly identify EOS versus limit as the stop reason. It is not evidence that
the input tokenizer omitted the last words. Increasing the outer limit alone would
not lift this inner cap.

Subsequent offline voice tests retained the same caps. The operator selected the
moderately brisk instruction at temperature 0.9; those two news samples lasted
10.272 and 10.688 seconds. Other settings still reached the ceiling. Shorter audio
and successful response events do not by themselves prove complete spoken wording.
See [voice configuration and deployment](s2s-production-promotion.md#sohee-delivery-configuration-september-5).

Original evidence is under `artifacts/diagnosis/official-live-20260905-103441/`;
voice comparisons are under `artifacts/qwen-tts-diagnostics/sohee-tuning-20260905-01/`
and `sohee-tuning-20260905-02/`. These private/ignored artifacts remain outside Git.
This output cutoff and the missed follow-up input are separate observations; neither
establishes the other's cause.

## Runtime Access Layers

### 1. Daemon REST API

Official docs: the Reachy Mini daemon exposes HTTP and WebSocket APIs at:

```text
http://<robot>:8000/api
```

For wireless robots, the documented default host is:

```text
http://reachy-mini.local:8000
```

Useful inspect endpoints:

```text
GET /api/daemon/status
GET /api/media/status
GET /api/motors/status
GET /api/state/full
GET /api/move/running
GET /api/volume/current
```

Useful control endpoints:

```text
POST /api/daemon/start
POST /api/daemon/stop
POST /api/daemon/restart
POST /api/media/release
POST /api/media/acquire
POST /api/media/wobbling/enable
POST /api/media/wobbling/disable
POST /api/motors/set_mode/enabled
POST /api/motors/set_mode/disabled
```

`POST /api/daemon/stop` requires a `goto_sleep` query parameter on the tested robot daemon version.
If the robot has already been explicitly slept, use:

```text
POST /api/daemon/stop?goto_sleep=false
```

Audio-board configuration endpoints:

```text
GET  /api/audio/config/parameter/<name>
POST /api/audio/config/apply
```

Volume caution: use `GET /api/volume/current` for inspection. Avoid casual `POST /api/volume/set`
during tests because the official app code notes this can trigger the daemon's test sound. Prefer the
SDK typed volume command when changing volume from app code.

### 2. SDK / WebSocket Control

The SDK connects to:

```text
ws://<robot>:8000/ws/sdk
```

The installed m1max SDK exposes these useful controls:

```text
wake_up
goto_sleep
enable_motors
disable_motors
enable_gravity_compensation
disable_gravity_compensation
goto_target
set_target
set_target_head_pose
set_target_antenna_joint_positions
set_target_body_yaw
enable_wobbling
disable_wobbling
release_media
acquire_media
start_recording
stop_recording
get_current_head_pose
get_current_joint_positions
get_present_antenna_joint_positions
```

The installed SDK protocol also includes:

- daemon log subscription over the typed transport
- audio parameter read/apply commands
- daemon restart command

The daemon restart command tears down WebRTC/control transport and expects the client to reconnect.

### 3. Direct Robot SSH / systemd

Direct SSH is needed when REST/SDK state is insufficient, especially for service logs, runtime refresh,
and audio/video device inspection.

The installed SDK scripts identify this service name:

```text
reachy-mini-daemon.service
```

First commands to run once robot SSH is available:

```bash
hostname
uptime
systemctl status reachy-mini-daemon --no-pager
journalctl -u reachy-mini-daemon -n 300 --no-pager
ss -ltnp | grep -E ':8000|:8443'
aplay -l
arecord -l
rpicam-hello --list
gst-inspect-1.0 libcamerasrc
```

## Refresh Order

Use the least disruptive refresh that can answer the question.

1. Stop the m1max official app process cleanly.
2. Check daemon/media/motor status through REST.
3. Try media release/acquire if the symptom is camera/audio pipeline specific.
4. Try daemon restart through REST or SDK if media/control state looks bad.
5. Use `systemctl restart reachy-mini-daemon.service` over robot SSH if REST/SDK restart is unavailable
   or stuck.
6. Full robot reboot only after the above fails or when the robot OS/device state appears broken.

## Bad Run Incident Packet

After a bad live run, capture these before restarting anything:

- m1max app process log.
- official-runtime run manifest.
- REST snapshots:
  - daemon status
  - media status
  - motor status
  - full robot state
  - running moves
  - current volume
- robot daemon journal covering at least 2 minutes before app start through shutdown.
- audio/video device state if playback/capture symptoms occurred:
  - `aplay -l`
  - `arecord -l`
  - camera list
- whether SDK/REST daemon restart fixed the next run, or full robot reboot was required.

## Useful One-Off Commands

From m1max or any machine on the same network:

```bash
curl http://reachy-mini.local:8000/api/daemon/status
curl http://reachy-mini.local:8000/api/media/status
curl http://reachy-mini.local:8000/api/motors/status
curl http://reachy-mini.local:8000/api/state/full
curl http://reachy-mini.local:8000/api/move/running
curl http://reachy-mini.local:8000/api/volume/current
```

If mDNS is flaky, use the robot IP instead of `reachy-mini.local`.

## Sources

- Reachy Mini REST API docs: https://huggingface.co/docs/reachy_mini/API/rest-api
- Reachy Mini core architecture docs: https://huggingface.co/docs/reachy_mini/SDK/core-concept
- Reachy Mini media architecture docs: https://huggingface.co/docs/reachy_mini/SDK/media-architecture
- Reachy Mini Python SDK docs: https://huggingface.co/docs/reachy_mini/SDK/python-sdk
- Reachy Mini advanced media controls: https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/media_advanced_controls
- Local legacy REST client reference: `src/reachy_mini_brain/robot.py`
- Native reception runtime: `src/reachy_mini_brain/official_runtime/live_app.py`
- Native S2S stream handler: `src/reachy_mini_brain/official_runtime/s2s_realtime.py`
