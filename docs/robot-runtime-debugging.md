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
- Local official-app runtime reference:
  `/Users/noel/projects/reachy_mini_conversation_app/src/reachy_mini_conversation_app/main.py`
- Local official-app stream reference:
  `/Users/noel/projects/reachy_mini_conversation_app/src/reachy_mini_conversation_app/console.py`
