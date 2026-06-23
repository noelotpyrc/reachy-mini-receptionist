# Continuous Listening

## Problem

`listen(5)` records a fixed duration, transcribes, and returns. While Claude Code is thinking or the robot is speaking, the mic is off — anything said during that time is lost.

## Design

Add a background listener thread to the session server that continuously buffers raw audio from the mic. Transcription only happens when Claude Code calls `listen_read`.

### Components

**Listener thread** (daemon, runs inside session server process):
- Tight loop: `get_audio_sample()` → append to buffer
- That's all. No STT, no interpretation. Just accumulates raw samples.
- Protected by a lock for thread-safe buffer access.

**`listen_start`** — starts the listener thread.

**`listen_read`** — grabs the entire buffer, clears it, transcribes (STT), returns text. Blocks during transcription. This is the only place STT runs.

**`listen_stop`** — sets stop event, joins the thread.

### Thread interaction with other commands

```
Session Server
│
├── Main thread (socket accept loop)
│   "speak"        → pushes audio to speaker (send side of pipeline)
│   "listen_read"  → grabs buffer, clears, transcribes, returns text
│   "nod"          → REST API call
│   "take_photo"   → reads from camera appsink
│
└── Listener thread (background)
    → reads from mic appsink (receive side of pipeline)
    → appends samples to buffer
```

- **Mic vs speaker**: different sides of the GStreamer pipeline. No conflict.
- **Camera**: separate appsink. No conflict.
- **Motion**: REST API. No conflict.
- **Buffer access**: protected by threading.Lock.
- **During `speak`**: `speak()` sets a `_speaking` flag. The listener thread checks this flag and discards samples while it's set, so the robot's own voice isn't buffered.

### Buffer management

- `listen_read` clears the buffer after transcribing.
- If Claude Code reads regularly (every few seconds), the buffer stays small.
- No max-size cap for now — rely on regular reads.

### How Claude Code uses it

Claude Code IS the robot's brain. It uses the session server as a tool to hear, speak, see, and move. The key insight: instead of building a separate conversation script, Claude Code drives the entire loop through `call` commands.

**Conversation flow (tested and working):**

```
Terminal 1 (session server — stays alive):
  python -m reachy_mini_brain.session serve

Terminal 2 (Claude Code — the brain):
  call listen_start                     ← mic starts buffering in background
  sleep 5 && call listen_read           ← poll: wait 5s, then transcribe buffer
                                           returns "hey reachy, how are you?"
  call speak "I'm doing great!"         ← robot speaks (mic discards own voice)
  sleep 5 && call listen_read           ← poll again
                                           returns "can you look left?"
  call look left                        ← robot moves
  call speak "Sure, looking left"       ← robot responds
  call take_photo artifacts/pic.jpg     ← robot sees
  (Claude describes the photo)
  call speak "I can see a desk with..." ← robot describes what it sees
  sleep 5 && call listen_read           ← keep polling...
  ...
  call listen_stop                      ← user types "stop" in chat
```

**Polling pattern**: Claude Code uses `sleep N && call listen_read` to autonomously check the mic buffer every N seconds. This lets it react to voice input without the user typing anything in the chat. The sleep duration controls responsiveness vs. CPU usage — 5s works well in practice.

**Multi-modal interaction**: Between polls, Claude Code can issue any command — speak, move, take photos, describe scenes. All channels work concurrently because they use different parts of the pipeline.

### Stopping

Only through:
1. User types in chat → Claude Code calls `listen_stop`
2. Force quit server (Ctrl+C) → daemon thread dies with process

No auto-stop based on voice content. The listener is a dumb buffer. Claude Code interprets the transcripts and decides what to do.

### What this does NOT include (future)

- VAD (voice activity detection) — would let us know when someone starts/stops talking
- Wake word detection — "hey reachy" trigger
- Echo cancellation — filtering out robot's own voice during playback
- Streaming STT — transcribe as audio arrives instead of in batches
