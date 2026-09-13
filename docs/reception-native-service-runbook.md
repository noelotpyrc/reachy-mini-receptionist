# Native Reception Candidate: Prepare And Roll Back

Status: First attended candidate run failed, September 13, 2026; not accepted.
The production launcher, backend and AV-probe listener are unchanged. The robot's
native app has been upgraded to 0.1.2 with rollback files retained; its configuration
selects the new `clinic-candidate` service. Only its control listener is running;
Reception requires official UI Start. After the failed run the service remains
fault-latched; review/reset is required before another Start.

## September 13 Preparation

- User approved pushing only `native-candidate-20260913` to the m1max bare Git
  remote and replacing native 0.1.1 with 0.1.2 after retaining rollback files.
- Candidate source: `ba24fc273c58650e04084995060c07fac32d4a0c`, in
  `/Users/leon/projects/reachy_mini_receptionist_release_ba24fc2_frozen`.
  GitHub and production main were not pushed or switched.
- A fresh Python 3.12.13 `.release-venv` was synced from `uv.lock` with the four
  production extras. All 126 top-level installed package versions match frozen
  production `4f0f52e`. Vision, gesture, service and SDK imports passed. Required
  GStreamer AV element factories are available; the documented optional
  `libgstpython.dylib` scanner warning also occurred.
- Native 0.1.2 was installed with `--no-deps` in robot `/venvs/apps_venv`.
  SDK remains 1.10.0; native import and official entry-point discovery passed.
  Under robot `~/.config/reachy-mini-reception/`, the 0.1.1 wheel is retained and
  `config.pre-0.1.2-20260913.json` preserves the prior configuration.
- Wheel and proposed runtime JSON are staged in `/tmp/reception-native-20260913/`
  on the local Mac and m1max. Runtime choices match production: private
  `reachyclinic` profile, `time-web`, door-v4, broker 15 FPS, audio recording on,
  video recording off, Rerun off. Native Stop owns run duration.
- User explicitly approved reuse of production `.env`. The service references
  `/Users/leon/projects/reachy_mini_receptionist_deploy/.env` directly through
  `--env-file`; its contents were neither copied, displayed nor modified.
- Bundle: shared `artifacts/official-runtime-live/native-service/native-candidate-20260913/`.
  Source, wheel, configuration, private-file permissions and TLS validation passed.
  `com.reachy.reception.native-candidate` is running as Interactive on port 8877,
  with no automatic restart. The bundle's `started`/`installed` fields describe
  preparation time, not live deployment status.
- Robot config now selects `wss://192.168.1.163:8877/reception/control` and
  `clinic-candidate`. Authenticated TLS connection and WebSocket ping passed from
  `/venvs/apps_venv`; no Start message was sent and no physical run was created.
- Final pre-start checks: no official app or CLI reception run active; robot daemon
  stopped with no reported error; existing S2S PID 55385 unchanged and healthy,
  Interactive verified, provider authorization HTTP 200, storage healthy.
  The operator should wake the robot through the official control UI if needed,
  then Start Reception and confirm Starting -> Ready before testing speech/chat.

## September 13 Interrupted Chat Failure

Run `native-dfac31b9db20433cb0d152114a7388ee`, backend session
`session_038b4b0bf4964d0ab02c0a873976a615`. Times below are EDT.

- 16:27:31.554: VAD barge-in cancelled the active response and TTS. Cancellation
  completed; there is no evidence of a stuck LLM request after this interruption.
- 16:27:32.147 and 16:27:33.156: Parakeet published empty transcripts for turn 8
  revisions 0 and 1 (1.332 s and 2.132 s input segments). No new LLM request followed.
- 16:27:33.169: the conversation cue started on the second empty-transcript event.
  `_summarize_event` omits empty text fields, and `_is_final_user_transcript` treats
  missing text as a valid turn for this event type. Local reproduction through both
  functions confirms this bug. The cue waits for audio/runtime completion that
  never follows an empty transcript. This file is identical to production 4f0f52e.
- Input forwarding continued: 1,836 microphone frames after the last empty
  transcript, through 16:28:13.143. Runtime ticks also continued. No further VAD
  turn events were recorded before shutdown; this alone does not establish why.
- 16:28:05.129: final published video frame. Capture/consumer summaries report no
  producer or inference failure. At 16:28:13 the service detected `video_stale`
  after the eight-second limit and stopped the run. Audio and event-loop activity
  remained recent. The terminal receipt reports video age 9.032 s after cleanup.
- 16:28:14: runtime cleanup completed with zero SDK cleanup errors, and the native
  app deliberately exited with code 1 on the reported service fault. This was a
  liveness-triggered shutdown, not an unexplained process crash. The thinking cue
  ran for about 40 seconds and stopped during cleanup.

Unresolved: why STT returned empty text, and why video delivery stopped. Scoped
robot journals show no corresponding camera/transport error before the stale
window; control requests and signalling pongs continued. No evidence establishes
that interruption caused the video loss. Do not attribute either to noise, network
jitter or compute starvation without further evidence.

Evidence remains on m1max: this run's events/manifest/receipt under the shared
`official-runtime-live` root, the candidate service logs, and
`artifacts/s2s-backend-trace/backend-trace-20260913-f119c3721803.jsonl`.
Raw-log copying locally was permission-blocked; diagnosis used in-place reads.
No runtime fix, backend restart or new physical run was performed during diagnosis.

Follow-up fix (candidate deployment requires separate verification): final S2S transcript summaries now
preserve empty text, and the conversation cue requires a nonblank transcript to
start from a transcript event. Missing/null/empty/whitespace values cannot start
thinking. The regression reproduces the two empty results around audio completion,
then checks that a subsequent valid turn starts/stops its cue normally. Full offline
suite: 497 passed, 1 skipped, 36 deselected; targeted lint and diff checks passed.
This does not add a generic thinking timeout or change STT/VAD/cancellation.

Relationship to video loss: camera polling runs in the broker's dedicated thread;
antenna commands are sequential, offloaded from the async loop, with 0.22/0.38 s
waits. The cue has no camera-disable path. Video continued for approximately
32 seconds after the erroneous thinking start, and audio/event-loop activity
continued through the video-stale window. These facts do not establish a causal
link or prove coincidence. Shared robot/network resource effects remain possible
but unverified. Per the user's decision, do not investigate the empty STT result
further for this fix.

## What The Operator Sees

The official app retains Start/Stop. Reception's embedded page shows run phase,
elapsed time, selected configuration/profile/tools/vision policy, recording flags,
input freshness, processing-loop freshness, run ID, robot ID and terminal reason.
It does not add manual robot controls, a camera viewer or another Start/Stop path.
Recording labels are configured settings, not proof that a file is intact. Input
freshness and SDK submission are not proof of physical speaker playback.

One server-owned configuration is selected for this candidate. The embedded page
is read-only: it cannot change credentials, tool permissions, recording settings
or configuration mid-run. Change the reviewed server configuration and matching
native config ID between runs, not through an unauthenticated browser endpoint.
Multiple selectable configurations can follow once actual operator choices are
agreed. No profiles, provider keys, or arbitrary commands travel through the UI.

When native control status becomes stale, Ready is replaced by Status stale and
health stops showing live readings. HTTP/service loss shows Status unavailable.
The official framework may remove the page when the app exits; the m1max lifecycle
receipt survives that exit. There is no hidden reconnect or automatic restart.

## Prepare The Matching Release

1. Review and commit the candidate source. Keep production's current release,
   launcher, native 0.1.1 wheel/config and candidate probe plist for rollback.
2. Create a separate checkout/release directory on m1max. Use the existing locked
   release-environment procedure and `uv.lock`; do not upgrade shared environments
   or modify production's venv. Verify the selected runtime's SDK is 1.10.0 and its
   required vision/gesture/diagnosis imports. Do not alter the separate S2S backend
   or its MLX pins and Interactive configuration.
3. Build the lean native wheel from this same source:

   ```sh
   uv build --offline --wheel native_app --out-dir /tmp/reception-native-wheel
   ```

4. Review a private runtime JSON using `native_app/reception-runtime.example.json`.
   Use the approved real profile directories, `time-web` tools, backend endpoint,
   door-v4 settings and recording choices. Explicitly check those choices against
   the working production configuration; the example is not a deployable profile.
5. Reuse the existing private m1max environment file. No new provider-key `.env`
   copy is generated. Prepare a control token and matching TLS certificate/key/CA
   through the existing credential provisioning process. They must already exist;
   the packager neither generates nor rotates credentials. Token, environment and
   private key files must have owner-only permissions.
6. Place the new bundle below the selected artifact root's `native-service/` so
   report-only retention includes its logs/configuration metadata. The output
   directory must not exist. Example future command, from the candidate checkout:

   ```sh
   .release-venv/bin/python -m reachy_mini_brain.reception_package \
     --output /ABSOLUTE/ARTIFACT_ROOT/native-service/CANDIDATE_ID \
     --repo /ABSOLUTE/CANDIDATE_RELEASE \
     --python /ABSOLUTE/CANDIDATE_RELEASE/.release-venv/bin/python \
     --runtime-config /ABSOLUTE/PRIVATE/runtime.json \
     --env-file /ABSOLUTE/EXISTING/.env \
     --token-file /ABSOLUTE/PRIVATE/control.token \
     --cert /ABSOLUTE/PRIVATE/service.pem --key /ABSOLUTE/PRIVATE/service.key \
     --ca /ABSOLUTE/PRIVATE/ca.pem \
     --wheel /ABSOLUTE/reachy_mini_reception_app-0.1.2-py3-none-any.whl \
     --host M1MAX_LAN_IP --robot-id ROBOT_ID --config-id clinic-candidate
   ```

The packager verifies clean Git state, configuration shape, private-file
permissions, certificate trust/address/expiry, official app entry point, native
wheel contents and source matching. It records source/lock/wheel/config/certificate
hashes in `bundle.json`. It does not certify installed dependency equivalence or
robot availability. Those remain m1max import/pre-start checks before deployment.

Outputs: `runtime.json`, `robot-config.json`, `bundle.json`, and
`com.reachy.reception.native-candidate.plist`. No secret contents are copied into
the plist or robot config. The plist references the existing environment/token/key
files, preserves the selected venv executable, and sets only explicit bootstrap
environment values (including its GStreamer GI path, not inherited wheel paths).

## Candidate Service Behavior

The candidate uses port 8877 by default, separately from the existing 8876 probe.
Its label is `com.reachy.reception.native-candidate`, not a production/S2S label.
`ProcessType=Interactive`, `RunAtLoad=false`, `KeepAlive=false`, `ExitTimeOut=15`.
Loading/starting this listener alone does not start a reception run. A valid
authenticated native Start is required. A crashed/faulted service requires operator
review before a manual restart. Launchd must not automatically clear a fault by
restarting it. The 15-second process-exit budget is a candidate value for live
acceptance, not proof of physical Stop latency.

Normal Stop closes only this SDK client and its outputs; it does not invoke global
media release, daemon sleep or backend stop. SIGTERM follows the same service
cleanup lifecycle. An uninterruptible native call may still require process-level
termination; do not start another owner until the previous process/resources have
been checked. Official daemon app-manager sleep behavior is unchanged.

## Approved Deployment Window

1. Confirm no CLI reception run is active. Stop the installed native probe and
   confirm it no longer owns media. Record the current app/config/service state.
2. Review the bundle and dependency/import checks on m1max. Confirm TLS reachability
   from the robot and that port 8877 is free. Do not start reception for this check.
3. With installation approval, install the 0.1.2 lean wheel in the robot apps venv,
   retaining the known-good wheel/config. No inference packages go onto the robot.
   Set native config to the bundle's service URL/config ID/robot ID. Copy only its
   config, control token and public CA; never the server/CA private key or `.env`.
4. Register/start only this plist, after review:

   ```sh
   launchctl bootstrap gui/$(id -u) /ABSOLUTE/BUNDLE/com.reachy.reception.native-candidate.plist
   launchctl kickstart gui/$(id -u)/com.reachy.reception.native-candidate
   ```

5. Use official UI Start for the agreed attended test. Confirm the displayed
   configuration and recording flags, advancing inputs, smooth speech, chat and
   reception policies. Then test Stop during speech, clean restart, startup Stop,
   control loss, and desktop preview afterward. Do not count mock health as physical
   acceptance. Freeze the matching release only after this gate passes.

## Receipts, Logs And Retention

- `<artifact_root>/service-receipts/native-<session_id>.json`: latest lifecycle
  transition, terminal reason, service PID, wall timestamp, bounded health/config
  snapshot and run ID. A startup-only/ready receipt after a process crash is not a
  clean Stop. Elapsed time freezes only when the service task actually ends.
- `<bundle>/service.stdout.log` and `service.stderr.log`: detailed service/runtime
  logs. Credentials and clinic instructions are not sent to the browser. A failed
  receipt write is logged and must not prevent output cancellation or SDK cleanup.
- Existing audio/video/vision/event manifests use `native-<session_id>` IDs and the
  existing diagnostic tools. Neither the backend trace layout nor model logging
  is replaced by these small lifecycle receipts.
- The retention scanner now also reports `native-service/` and `service-receipts/`
  under the selected artifact root, with the same 30-day/report-only semantics.
  No deletion is automated. The currently deployed older scanner does not yet have
  this addition: during candidate acceptance run the candidate scanner explicitly;
  point the managed reminder at the matching release only at approved promotion.
  Use the existing shared artifact root or include the separate candidate root in
  the review, rather than assuming the production reminder discovers arbitrary paths.

## Rollback

1. Official UI Stop; confirm the receipt reached stopped and the worker released
   media/output ownership. If faulted or timed out, inspect the service process and
   robot state before starting any replacement owner.
2. Unload the candidate only:

   ```sh
   launchctl bootout gui/$(id -u)/com.reachy.reception.native-candidate
   ```

3. Confirm the candidate process is gone. The production launcher/backend were
   never switched, so use the existing approved production start procedure. Restore
   the retained native app wheel/config only if reverting to the probe is wanted.
4. Keep the candidate bundle, logs, receipts and artifacts for diagnosis. No cleanup
   deletion, dependency downgrade, global media release or S2S restart is implied.

## Offline Evidence

- Full default offline suite: 480 passed, 1 skipped, 36 deselected. Ruff for the
  changed modules and `git diff --check` passed. Hardware/optional LiveKit tests
  remain excluded; no physical acceptance is claimed.
- Native 0.1.2 wheel built and inspected: control package/static assets only, SDK
  1.10.0 plus websockets; no model/service package or clinic data.
- Official SDK settings-page routes and assets tested without starting its robot
  lifecycle. Local Chrome checks passed at 920, 360 and 320 pixels, across all
  lifecycle/stale/disconnected states, with no page errors or horizontal overflow.
- Packaging tests cover clean-source enforcement, source/wheel match, inert
  Interactive plist generation, secret exclusion and refusal to overwrite bundles.
- A real local service subprocess in mock mode accepted Start, then SIGTERM closed
  the session and wrote a stopped receipt. No launchd or robot action was performed.

Screenshots: `artifacts/diagnosis/native-step5-ui/`. For a clearly labelled,
fabricated-status preview only: `.venv/bin/python native_app/preview_status.py`.
Optional browser check: `node native_app/check_status_ui.cjs URL OUTPUT_DIRECTORY`
using an installed Playwright/Chrome. Public app-store publication and the copied
official icon's redistribution terms still need separate review.
