# Reachy Mini — Robot CLI Guide

Full reference for all CLI commands. All commands run from project root.

> **Bringing the reception robot up on m1max?** Follow [`runbook.md`](./runbook.md).
> The current path is the Python `reception-ops` CLI over `official_runtime.ops_core`.
> `scripts/m1max/live_ops.sh` is compatibility-only.

## Current Reception Live Path

Use this path for live receptionist tests unless explicitly comparing against legacy behavior.
Run from m1max:

```bash
~/.local/bin/reception-prod status
~/.local/bin/reception-prod --confirm-physical start-session
~/.local/bin/reception-prod --confirm-physical stop-session
```

The wrapper owns the normal live-test lifecycle:

- `status` prints backend process state plus robot daemon/media/motor status.
- `preflight` runs the known-good playback probe, then scripted goodbye -> greet through the
  policy/backend/speaker path. Wait for human confirmation before starting a conversation run.
- `clean-run` sleeps/cleans stale state, keeps or starts the local m1max S2S backend, wakes the
  robot, starts `official_runtime.live_app`, and cleans up on interruption.
- `clean-stop` stops the live runner, releases media, sends sleep, disables motors, and drains
  running moves.

Direct current-runtime entrypoint:

```bash
.venv/bin/python -m reachy_mini_brain.official_runtime.live_app --help
```

## Vision

```bash
# Default (720p, saves to artifacts/reachy_photo.jpg)
.venv/bin/python -m reachy_mini_brain.vision take-photo

# Higher resolution for documents/OCR
.venv/bin/python -m reachy_mini_brain.vision take-photo --resolution 1080p
.venv/bin/python -m reachy_mini_brain.vision take-photo --resolution 4k
.venv/bin/python -m reachy_mini_brain.vision take-photo --resolution max   # 3840x2592

# Custom output path
.venv/bin/python -m reachy_mini_brain.vision take-photo --out /tmp/photo.jpg
```

| Flag | Values | Default | Notes |
|------|--------|---------|-------|
| `--out` | file path | `artifacts/reachy_photo.jpg` | Output JPEG path |
| `--resolution` | `720p`, `1080p`, `4k`, `max` | `720p` | Higher = slower first frame |
| `--retries` | integer | `5` | Frame grab retries (WebRTC warmup) |

**Resolutions:**
- `720p` — 1280×720 @ 30fps (fast, default)
- `1080p` — 1920×1080 @ 30fps (good for general use)
- `4k` — 3840×2160 @ 10fps (high detail)
- `max` — 3840×2592 @ 10fps (near-full-sensor, highest)

## Motion

```bash
# Lifecycle
.venv/bin/python -m reachy_mini_brain.motion wake-up
.venv/bin/python -m reachy_mini_brain.motion sleep

# Gestures
.venv/bin/python -m reachy_mini_brain.motion nod        # Yes gesture
.venv/bin/python -m reachy_mini_brain.motion shake       # No gesture

# Head control (degrees)
.venv/bin/python -m reachy_mini_brain.motion move-head --pitch 10 --yaw 30
.venv/bin/python -m reachy_mini_brain.motion move-head --pitch 0 --roll 0 --yaw 0  # center

# Look presets
.venv/bin/python -m reachy_mini_brain.motion look --direction left
.venv/bin/python -m reachy_mini_brain.motion look --direction right
.venv/bin/python -m reachy_mini_brain.motion look --direction up
.venv/bin/python -m reachy_mini_brain.motion look --direction down
.venv/bin/python -m reachy_mini_brain.motion look --direction center

# Body rotation (degrees)
.venv/bin/python -m reachy_mini_brain.motion rotate-body --angle 90

# Antennas (degrees, positive = up)
.venv/bin/python -m reachy_mini_brain.motion antennas --left 30 --right -30
```

### Commands

| Command | Flags | Notes |
|---------|-------|-------|
| `wake-up` | — | Always run first |
| `sleep` | — | Run when done |
| `move-head` | `--pitch --roll --yaw --duration` | Degrees. Interpolated movement |
| `look` | `--direction left\|right\|up\|down\|center` | Preset positions |
| `rotate-body` | `--angle --duration` | Body yaw in degrees |
| `antennas` | `--left --right` | Degrees, positive = up |
| `nod` | — | Yes gesture (2× pitch cycle) |
| `shake` | — | No gesture (3× yaw cycle) |

### Angle Conventions

- **Pitch:** positive = look down, negative = look up
- **Yaw:** positive = look left, negative = look right
- **Roll:** positive = tilt right, negative = tilt left
- **Antennas:** positive = up, negative = down (both sides)
- **Body:** degrees, full 360° rotation supported

## Audio

```bash
# Listen (record from robot mic, transcribe with Whisper)
.venv/bin/python -m reachy_mini_brain.audio listen --duration 5
.venv/bin/python -m reachy_mini_brain.audio listen --duration 10 --model small --language en

# Speak (TTS through robot speaker)
.venv/bin/python -m reachy_mini_brain.audio speak "Hello, I am Reachy"

# Play a WAV file through robot speaker
.venv/bin/python -m reachy_mini_brain.audio play-sound path/to/file.wav

# Direction of arrival (mic array)
.venv/bin/python -m reachy_mini_brain.audio doa
```

### Commands

| Command | Flags | Notes |
|---------|-------|-------|
| `listen` | `--duration SEC --model tiny\|base\|small\|medium --language CODE --save-wav PATH` | Mic → STT → prints transcript |
| `speak` | `TEXT --voice NAME` | TTS → robot speaker. Default voice: `en_US-lessac-medium` |
| `play-sound` | `PATH` | Play a WAV file through robot speaker |
| `doa` | — | Direction of arrival JSON: `{angle_degrees, speech_detected}` |

### Notes

- Audio goes through SDK WebRTC (same connection as camera), so first call has ~30-60s warmup
- Mic records at 16kHz stereo; STT runs locally via faster-whisper
- TTS runs locally via piper-tts; audio is pushed to robot speaker via WebRTC
- Voice models auto-download on first use (~60MB for whisper-base, ~60MB for piper voice)
- Install audio deps: `uv pip install -e ".[audio]"`

## Video

```bash
# Record 10 seconds of video
.venv/bin/python -m reachy_mini_brain.video record --duration 10

# Custom output and resolution
.venv/bin/python -m reachy_mini_brain.video record --duration 5 --out artifacts/clip.mp4 --resolution 1080p
```

| Flag | Values | Default | Notes |
|------|--------|---------|-------|
| `--duration` | seconds | `10` | Recording length |
| `--out` | file path | `artifacts/reachy_video.mp4` | Output MP4 path |
| `--resolution` | `720p`, `1080p` | `720p` | Capture resolution |
| `--fps` | number | auto (30 or 10) | Target FPS |

## State

```bash
.venv/bin/python -m reachy_mini_brain.state get-state
```

Prints JSON with head pose matrix, antenna angles, and IMU data (if wireless).

## Replay And Review

Use the official-runtime diagnostic tools for current artifacts:

```bash
.venv/bin/python -m reachy_mini_brain.official_runtime.replay_vision --help
.venv/bin/python -m reachy_mini_brain.official_runtime.audio_review --help
.venv/bin/python -m reachy_mini_brain.official_runtime.door_review --help
```

The removed reception daemon, alert engine, and old replay/review tools remain recoverable at Git
tag `legacy-daemon-last`. Their historical design is documented in
`docs/archive/legacy/plan-reception.md`; they are not supported runtime fallbacks.
