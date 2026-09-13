# Native Reception App Feasibility

Date: September 9, 2026. Status: historical source review and boundary audit.
The revised [native-app specification](reception-native-app-spec.md) is authoritative.
Implementation has resumed using the existing bidirectional SDK media path.

**Latest decision:** service-side SDK playback is retained. The native app owns
run lifecycle/control, not a separate speaker pipeline. This supersedes the
split-input/output design and the stop boundary below. Automatic return silence
is normal on the retained path, not an implementation blocker. The earlier
sections are retained as investigation history, not current instructions.

Decision: retain combined daemon camera/microphone input to m1max and route
physical output through the native app. The suggested native-microphone
alternative was not adopted. The specification, not that alternative, is the
implementation baseline.

## Agreed Principles

- A native app, not an installed controller that starts a remote robot-owning runner.
- Keep the robot workload small: media plumbing, physical output, local stop, and UI.
- Keep inference, reception policy, chat lifecycle, profiles, tools, and diagnostics
  together on m1max. Avoid vision-result round trips through robot-side policy.
- Reuse our current processing modules and the SDK's public media/lifecycle APIs.
- Preserve production unchanged while evaluating the native architecture.

This replaces the initial placement proposal that moved reception policy,
profile composition, and tool execution onto the robot and split vision into a
separate service. That additional distribution is not required.

## Official Conversation App Reviewed

Local clone: `/Users/noel/projects/reachy_mini_conversation_app`.

Upstream: `pollen-robotics/reachy_mini_conversation_app`.
Commit: `531baaa1753054eb3c1a4c86259349f353fc3053`, tag `v1.0.1`.
The clone is clean. No dependencies were installed or app/tests launched.
Its dependency constraint permits SDK `>=1.10.0rc5`; its lock contains
`1.10.0rc5`. Our deployed SDK is `1.10.0`; do not replace our environment with
their lock as part of this review.

The following is confirmed from that source, not inferred from an architecture
illustration.

### Native Installation And UI

`pyproject.toml` declares a `reachy_mini_apps` entry point pointing to
`ReachyMiniConversationApp`. The class subclasses `ReachyMiniApp`, advertises a
web UI on port 7860, and passes the provided robot, stop event, and settings app
into the runtime. The release workflow mirrors it to a Hugging Face Space.

The desktop is a control/display surface, not the host of this Python process
on a wireless robot. The same application can also run from a CLI: if no SDK
instance is supplied, it constructs one and lets the SDK select local or remote
media. Do not confuse this remote CLI mode with its robot-installed mode.

The settings UI uses the SDK `JsonRpcServer` on `/rpc` for status, mute,
interruption, connection settings, and profile/tool management. The SDK also
supports relaying app RPC over its control DataChannel. This UI/control surface
is distinct from the audio websocket to the inference backend.

### Audio And Backend

```text
Official desktop / daemon app lifecycle
  -> Conversation app on wireless robot
       supplied SDK -> LocalStream microphone loop
                         -> HuggingFaceRealtimeHandler
                            <-> remote realtime backend
                         <- audio deltas
       supplied SDK <- LocalStream playback loop -> speaker

       local tool execution + queued movement -> supplied SDK
       optional settings UI -> SDK JSON-RPC
```

`LocalStream.launch()` explicitly starts microphone capture and playback, then
runs concurrent backend, record, and play tasks. Capture reads SDK samples;
the handler sends mono PCM16 as base64 in realtime input-buffer events. Returned
audio deltas become queued PCM samples and are submitted to local SDK playback.

The README explicitly documents a wireless robot connecting to a laptop backend
over Wi-Fi using `HF_REALTIME_CONNECTION_MODE=local` and a LAN websocket URL.
Here "local" means a directly selected backend, not necessarily the robot itself.
It also documents a tunnel alternative; that is not our proposed production wiring.

The client config requests server-side VAD. It contains no local STT/LLM/TTS model
stack in this audio loop. Backend internals are outside this app repository;
our existing S2S service can retain its current model configuration.

### Motion, Cancellation, And Stop

- Motion requests go through a local movement queue/worker and SDK calls.
  Its configured movement frequency is 60 Hz; an introductory comment still says
  approximately 100 Hz. Do not copy the whole motion system merely for reception cues.
- Speech-related wobble and face tracking use SDK facilities; face tracking is
  delegated to the daemon, not implemented as our RF-DETR visitor pipeline.
- On server `input_audio_buffer.speech_started`, the app flushes SDK playback
  through `clear_player()` and drains pending handler audio in place.
- App stop stops local media first, signals/cancels async tasks, closes the backend
  connection, and stops motion writes. Normal external Stop intentionally does not
  mean sleep; explicit sleep/inactivity are separate paths.
- Their startup also writes XVF3800 audio-processing settings. Those settings are
  behavioral changes, not required app-framework plumbing; do not adopt them
  automatically with the architecture.

The current SDK 1.10.0 source contains `clear_player()` for both LOCAL and WebRTC
audio. We should build on that API rather than invent a speaker-buffer flush
mechanism. It is not proof of physical audibility or measured cancellation latency.

### Camera And Agent Features

The camera tool captures a JPEG snapshot through `get_frame_jpeg()` when called.
The handler adds that image to the remote conversation. This is not a continuous
video upload for door/person/gesture detection.

The reviewed app has no equivalent of our RF-DETR/DINO/MediaPipe reception pipeline.
Its profiles, tool registry, memory handling, and client-side tool execution live
with the app. We can learn from these interfaces without moving our own profiles
and tools to the robot or enabling their tool collection in production.

## Implications For Our Draft

### Keep The Processing Placement

| Robot native app | One reception service on m1max |
| --- | --- |
| Official entry point, settings UI, stop handling | Existing runtime assembly adapted from CLI to service sessions |
| SDK playback and movement adapters | Combined daemon AV consumer, frame broker, RF-DETR, tracking, DINO, MediaPipe, door fusion |
| Bounded output/cue execution | Reception policy, cooldowns, chat latch, timeout ticks |
| Reject stale-session output; report errors/progress | S2S client and existing loopback S2S backend |
| Service credentials and non-secret run selection | Clinic profile, time/web tools, provider keys, retained artifacts |

m1max decides behavior but does not make direct motor or speaker calls. The native
app executes or rejects session-scoped outputs and immediately stops locally when
its lifecycle ends. No remote OPS start/stop bridge is needed for a second runner.

Our `OfficialStyleStreamRuntime` already accepts audio source/sink interfaces.
Keep the processing flow and replace its external I/O edges. Keep door/person
fusion and frame identities together rather than creating separate inference
services with additional network coordination.

### Selected Input And Output Paths

Retain the user's original two-connection topology:

```text
Robot daemon: encoded camera + microphone
       --> existing combined WebRTC input to reception service
Robot native app: local SDK speaker + movement
       <-> persistent reception session/output connection
m1max reception service: existing policy/chat/tools/runtime
       <-> existing loopback connection
m1max S2S backend: unchanged VAD/STT/LLM/TTS
```

The native hardware handler need not implement the full agent or profile layer.
Reuse our source/sink/handler shapes, with a bounded reception transport and typed
cue/cancel/status messages. Use SDK JSON-RPC for the UI where it fits; it is not
automatically an audio streaming or high-rate vision transport.

The official app instead captures microphone input through its native client.
Its source does not establish our selected split path's audio/echo behavior or
resource use. Coexistence remains a required feasibility check; it is not a
reason to silently replace the agreed input path with native microphone capture.

Copy public API usage and lifecycle patterns, not the whole app or its old imported
dependency. Preserve our fixed-text `tts.create` policy speech: upstream `say()`
instead inserts a user message and asks the model to respond, which is not equivalent.

### Continuous Video Remains Our Extension

The official app provides no equivalent of our continuous vision pipeline.
Our selected design retains combined daemon video/audio consumption on m1max,
without return-audio submission or direct movement. Verify SDK setup side effects
and coexistence with native playback before integrating the whole runtime.

SDK 1.10.0's local camera IPC builder caps delivery at 10 FPS; that path is not
selected here. Our current production broker targets 15 FPS over remote WebRTC.
Preserve image framing/resolution and verify actual cadence before policy parity.
Do not add native frame forwarding, lower the production rate, or patch SDK limits.

Do not add a per-frame request/response loop or send raw BGR over Wi-Fi. For scale,
720p BGR at 10 FPS is about 27.6 MB/s (221 Mb/s) before overhead. Video transport
must not block audio or cancellation. Preserve run/generation/frame IDs, source
timestamps, and explicit dropped-frame accounting for offline review.

## Work Still Required

1. Extract service-session initialization from `live_app.py`; inject remote audio
   sinks and motion/cue adapters instead of `ReachyRobotSession` and global REST
   robot calls. Keep policy, profile/tool, and model behavior unchanged.
2. Create a lean native SDK app with local playback and movement, stop handling,
   session identity, output flushing, and settings/status UI. Start local playback
   explicitly without adding a native microphone capture loop.
3. Establish a private authenticated reception-service endpoint. The existing S2S
   listener was observed on `127.0.0.1:8765`; with this placement it can remain
   loopback-only behind the service. Do not expose model/robot endpoints publicly.
4. Verify combined daemon AV input plus native output in a bounded experiment.
   There is no measured latency/CPU evidence yet for this arrangement.
5. Preserve liveness and artifacts: service decisions/capture data plus native
   output submissions/flushes/errors. Keep archives on m1max; do not infer local
   playback from service send timestamps. Do not compare cross-host monotonic clocks.
6. Adapt failure/stop semantics without losing current supervisor protections.
   Official subprocess status is not advancing audio/video or event-loop health.
   Cleanup must fit the daemon app manager's 20-second stop budget; long archive
   finalization must not delay local output stop.
7. Package shared lightweight modules for Linux ARM separately from m1max model
   dependencies. Inspect the robot's Python/SDK/apps venv before installing:
   our current uv configuration resolves for macOS only.

SDK `no_media` is not a passive way to skip subscription: it releases daemon
hardware. Do not use it for this native audio path. Official desktop 0.9.34 also
requests daemon stop on Wi-Fi window close; this separate behavior still needs
resolution before claiming close-and-continue operation.

## Validation Boundary And Implementation Gate

This investigation cloned and read source only. No app dependency, robot package,
daemon setting, backend setting, or live-run configuration was changed. The ongoing
three-hour production run was not touched. No build, test suite, or performance
benchmark was run in the new clone.

The finalized specification retains daemon AV input and native physical output.
Confirm its implementation sequence, then prove the transport/lifecycle gates
before broad runtime refactoring. No measured claim that this path is faster or
already compatible is made by this source review.

Rollback remains the existing production runtime: stop the native app and confirm
output release before starting the old runner. Never allow both to control the
same robot. No deletion or automatic dependency upgrade is authorized here.

## Implementation Boundary Audit: September 9

Implementation was authorized after the architecture was finalized. Step 1
checked the actual SDK boundaries before building the transport prototype.
It found behavior that the earlier source review did not follow far enough.

### Evidence Scope

Inspected installed `reachy-mini==1.10.0` under the local `.venv`. A read-only SSH
check of the frozen m1max release's `.release-venv` confirmed version 1.10.0 and
byte-identical hashes for all four files below. The check used package metadata
and file reads only; it did not import/instantiate the SDK, connect to the robot,
or change the active run. The robot's own installed files were not hash-checked.

Paths below are relative to the installed `reachy_mini` package:

| File | SHA-256, same locally and on m1max |
| --- | --- |
| `media/webrtc_client_gstreamer.py` | `549cfac84af64aeb41574b2b968c39719dde01081b424b7158d9d5f55b3ad841` |
| `media/audio_gstreamer.py` | `ea00eb1baaa7670001c33b026eabe8e4bbd002fd0bcbd71ef3f77374e31f59fe` |
| `media/media_manager.py` | `d2cbe24f32b8a2e650f91313fabd6af20beeebbdd04147f1ab6ae748e876b15c` |
| `reachy_mini.py` | `143fb920f2a537e43ec82f9fd265916e7adab54dd8dede80db441c02e148db49` |

### Findings

1. **WebRTC consumption is not receive-only.** In
   `webrtc_client_gstreamer.py`, `_on_new_transceiver()` selects SENDRECV
   (lines 196-218). The audio pad callback invokes `_setup_audio_send_chain()`
   (line 299), which creates a live silent `audiotestsrc`, mixer, Opus encoder,
   and RTP return stream (lines 363-505). `start_playing()` is explicitly a no-op
   because setup is automatic (line 507). Not calling `push_audio_sample()` does
   not suppress this stream. Neither `GstWebRTCClient` nor `MediaManager`
   exposes a receive-only constructor selector in the reviewed source.
2. **This is more than an unused negotiated track.** The reviewed local
   `media_server.py` handles incoming audio by building a per-peer playback
   pipeline with a physical audio sink (lines 530-685). A successful return
   connection can therefore create a daemon-side speaker writer even if its
   payload is silence. This violates the spec's strict no-competing-writer
   assumption. It does not establish that ALSA mixing or actual playback fails.
3. **Local playback starts microphone capture internally.**
   `audio_gstreamer.py` constructs capture and playback in one pipeline
   (lines 123-133). Both `start_recording()` and `start_playing()` set that same
   pipeline to PLAYING (lines 480-494). There is no exposed playback-only switch.
   The app can refrain from reading/forwarding microphone samples, but cannot
   claim no local capture simply by omitting `start_recording()`. Coexistence
   with daemon capture and the extra robot workload remain unmeasured.
4. **The existing top-level session is not a passive media adapter.**
   `ReachyMini.__init__()` calls `set_automatic_body_yaw()` (line 177);
   `ReachyRobotSession.start()` additionally calls our global `robot.ensure_ready()`.
   Passing `automatic_body_yaw=False` still sends a setting command. Direct use
   of the documented `MediaManager` constructor avoids that top-level movement
   setup, but does not fix automatic return audio.
5. **Media cleanup also crosses the output boundary.**
   `MediaManager.close()` calls `audio.stop_playing()` (line 192).
   WebRTC `stop_playing()` posts `/api/media/stop_sound` when a daemon URL exists
   (lines 511-522). A nominal input adapter therefore cannot blindly inherit
   this teardown and claim it never invokes daemon playback controls.

### Stop Boundary And Next Decision

Step 2 is paused, not completed. No production code or SDK was edited and no
candidate app was installed. No tests of physical media coexistence were run.
The source findings establish SDK behavior, not physical echo or choppy audio.

Before continuing, decide how to obtain supported receive-only WebRTC and
playback-only local audio, or explicitly relax the strict ownership/capture
requirements and authorize a bounded coexistence test. The latter would retain
our application-level input/output routing but acknowledge SDK-level extra
capture and daemon silence playback; it is not equivalent to the original
strict claim. Investigating an upstream SDK option/change is preferable to
private-method overrides, dependency monkey patches, or an unapproved rewrite
of the media stack. Do not switch to native microphone forwarding automatically.

## Source References

Official Conversation app paths below are pinned to the reviewed commit:

- [App package and entry point](https://github.com/pollen-robotics/reachy_mini_conversation_app/blob/531baaa1753054eb3c1a4c86259349f353fc3053/pyproject.toml)
- [Native entry and lifecycle](https://github.com/pollen-robotics/reachy_mini_conversation_app/blob/531baaa1753054eb3c1a4c86259349f353fc3053/src/reachy_mini_conversation_app/main.py)
- [Local audio loops and UI RPC](https://github.com/pollen-robotics/reachy_mini_conversation_app/blob/531baaa1753054eb3c1a4c86259349f353fc3053/src/reachy_mini_conversation_app/console.py)
- [Realtime audio, events, and tools](https://github.com/pollen-robotics/reachy_mini_conversation_app/blob/531baaa1753054eb3c1a4c86259349f353fc3053/src/reachy_mini_conversation_app/huggingface_realtime.py)
- [Camera tool](https://github.com/pollen-robotics/reachy_mini_conversation_app/blob/531baaa1753054eb3c1a4c86259349f353fc3053/src/reachy_mini_conversation_app/tools/camera.py)
- [Wireless-to-laptop backend setup](https://github.com/pollen-robotics/reachy_mini_conversation_app/blob/531baaa1753054eb3c1a4c86259349f353fc3053/README.md#hugging-face-connection-modes)
- [SDK 1.10.0 app base](https://github.com/pollen-robotics/reachy_mini/blob/v1.10.0/src/reachy_mini/apps/app.py)
- [SDK 1.10.0 camera server](https://github.com/pollen-robotics/reachy_mini/blob/v1.10.0/src/reachy_mini/media/media_server.py)
