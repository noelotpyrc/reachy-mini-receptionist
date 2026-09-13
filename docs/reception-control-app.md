# Reception Control In Reachy Mini Control

**Status:** historical investigation; native architecture selected September 9, 2026.

**Source review:** completed September 9, 2026 against the installed desktop version
`0.9.34`; implementation has not started. See the findings and revised sequence below.

**Architecture decision:** implement a proper native Reception app with processing
on m1max, existing bidirectional daemon AV, and native lifecycle/control. The
authoritative baseline is the [native-app specification](reception-native-app-spec.md).
See [native-app feasibility](reception-native-app-feasibility.md) for source evidence.
The desktop-fork and wrapper sections below are historical investigations, not
approved implementation plans. None of these approaches has been built.

**Scope:** add administrative reception-run control to the existing Reachy Mini Control desktop
application installed on m1max. Preserve the official application's existing robot functions.

## First-Pass Goal

An administrator should use one desktop application to operate the robot and the receptionist. The
first pass is local to m1max and intentionally does not introduce a remote operations service,
non-technical operator mode, or a replacement for the official controls.

Add a **Reception** section to the official application with:

- aggregate Backend / Robot / Runner status;
- Start Reception using the selected production configuration;
- Stop Reception through the normal artifact-finalizing and robot-cleanup lifecycle;
- Emergency Stop using the existing idempotent OPS shutdown path;
- startup and stopping progress, terminal fault reason, run ID, and elapsed time;
- recording state and the latest-run/artifact location; and
- a clear distinction between `Offline`, `Starting`, `Live`, `Stopping`, and `Faulted`.

Reception sessions started by the app run until an administrator selects **End Reception** or
**Emergency Stop**. The app must request first-class unlimited-session semantics from OPS rather
than emulating them with a large numeric duration. Fixed-duration runs remain available to the CLI
for assisted tests and deliberately bounded shifts.

The existing official application remains responsible for its current daemon, motor, camera/audio,
application-management, update, and manual-position controls. Reception controls compose those
capabilities through this repository's OPS lifecycle rather than duplicating them.

## Minimal Integration

For the local first pass, the Tauri application can call an allowlisted adapter around the existing
`reception-ops` CLI and consume its structured output. The app polls structured status and launches
only named OPS actions; it must not expose an arbitrary shell-command field.

This keeps `ops_core` as the lifecycle source of truth:

```text
Reachy Mini Control (m1max)
  -> local allowlisted Tauri adapter
    -> reception-ops / ops_core
      -> Backend OPS
      -> Robot OPS
      -> Runner OPS
```

No new network listener is required for this version. A future authenticated operations service can
wrap the same `ops_core` functions if control from another machine becomes necessary.

## Production Configuration

The app should select one named production configuration and show its resolved, non-secret summary
before startup: release, profile, recording mode, provider/model, voice, vision profile, and session
duration. Secrets and clinic context remain outside Git and must not be displayed or copied into the
application's logs.

For the first pass, configuration remains administrator-managed. The app prevents accidental legacy
or incomplete launches by validating the selected configuration before invoking Start Reception.

## Resource Ownership

The official application and reception runner can otherwise compete for robot resources. While a
reception run is active:

- the Runner remains the single owner of the robot media session;
- actions that start another robot application or acquire competing media require an explicit stop
  or ownership transfer;
- physical actions retain the existing authorization/confirmation semantics; and
- status must identify the active owner rather than reporting only that media is busy.

The first pass does not need an in-app reception video preview. If added later, it should subscribe
to frames already received by the reception frame broker instead of opening a second robot WebRTC
session. Rerun remains a diagnostic surface, not the operator stream.

## Position And Reception Framing

The official application already provides manual position controls. The unresolved product issue is
defining and preserving a camera position suitable for door and Open Palm policies.

Current OPS startup calls the robot's canned `wake_up` move before starting the runner. That move
returns the head to the robot's predefined initial pose, so manual adjustment performed before Start
Reception can be lost. A later framing iteration should separate robot preparation from runner start
or restore a saved reception pose after wake-up, then validate the view before enabling visual
policies.

Possible future framing functions include a saved reception pose, door-box overlay, visual readiness
check, and Open Palm test. They are intentionally outside the first control-app pass.

## Safety And Failure Behavior

- Start and Stop are asynchronous UI operations with one operation in progress at a time.
- Repeated Start or Stop requests are idempotent and cannot create parallel runners.
- Emergency Stop remains available while another operation is pending.
- Closing the desktop window does not terminate an active reception run.
- A failed action shows the retained OPS terminal reason and recovery instruction.
- The app does not automatically restart a physical run after a media or network fault.
- The robot daemon, Hermes, and S2S backend remain private and are never exposed publicly.

## Deferred Functions

- remote browser or remote desktop client support;
- non-technical Operator and Administrator roles;
- live reception video/audio monitoring;
- manual-motion arbitration during an active reception run;
- saved reception pose and visual framing calibration;
- integrated Rerun/audio-review artifact browsing; and
- automatic physical-run restart.

These can be added independently after the local administrative lifecycle is accepted.

## Version-Matched Source Review: September 9

Installed on m1max: `/Applications/Reachy Mini Control.app`, bundle version `0.9.34`,
identifier `com.pollen-robotics.reachy-mini`. The separate local clone is
`/Users/noel/projects/reachy-mini-desktop-app`, checked out clean at tag `v0.9.34`,
commit `467ad30e00855cd5051c8483fed00b4e00b57d1a`.

Upstream: [pollen-robotics/reachy-mini-desktop-app](https://github.com/pollen-robotics/reachy-mini-desktop-app/tree/467ad30e00855cd5051c8483fed00b4e00b57d1a).
The tag's package and Tauri configuration files still say `0.9.32`; the release
workflow injects the version from the release tag. Do not mistake those source
defaults for the installed app version. This was a static source review, not a
build, a UI test, or a robot coexistence test. No dependencies were installed.

### Existing Implementation and Extension Points

| Area | Current implementation | Proposed addition |
| --- | --- | --- |
| UI | React/TypeScript, MUI, Zustand; `ActiveRobotModule` injects desktop/web adapters into shared views | Desktop-only Reception panel using the existing styling and layout |
| Panel navigation | `RightPanel.tsx`, `ControlButtons.tsx`, and `RightPanelView` select applications, controller, expressions, or an embedded app | Add a Reception view and visible run-status entry; make it accessible without Hugging Face login |
| Native actions | Tauri Rust commands registered in `src-tauri/src/lib.rs` | Small `reception` module with fixed status/start/stop/emergency actions calling the installed `reception-prod` launcher |
| Runtime state | Official app status comes from daemon `/api/apps/current-app-status` | Separate reception state from OPS; do not insert a fake daemon app into `currentApp` |
| Media | `WebRTCStreamProvider` automatically connects when robot status is READY or BUSY | Gate automatic connection, reconnect, and external-camera fallback on reception ownership |
| Packaging | Official bundle identity, deep link, and signed updater feed | Distinct custom build identity and explicit update policy; preserve installed official app |

The separate `WebApp.tsx` build has no native local-process bridge. Keep the new
adapter desktop-only; a remote browser controller would need a separately designed
authenticated service, not exposure of OPS or the robot daemon to the public network.

### Required Coexistence Guards

1. **Camera/audio:** `src/contexts/WebRTCStreamContext.tsx` derives `shouldConnect`
   from robot-awake status, not camera-panel visibility. Hiding the camera widget
   is insufficient. Resolve reception ownership before connecting, stop the app's
   stream before starting reception, and inhibit retries while the runner owns media.
   This source review establishes an extra connection path, not proof that every
   simultaneous connection will fail. First pass deliberately avoids that risk.
2. **Window close:** `src/components/App.tsx` registers a Wi-Fi close handler that
   POSTs `/api/daemon/stop?goto_sleep=false`. It must skip that action while reception
   owns the robot, including startup/stop transitions and unresolved ownership after
   an established run. Closing the UI should not invoke End Reception. Rust also has
   local-daemon cleanup hooks; verify those remain separate from the external runner
   and managed S2S processes. Keep ordinary idle/USB behavior unchanged.
3. **Other controls:** controller motion uses `/api/move/ws/set_target` with an HTTP
   fallback. Sleep/power, expressions, app launching, and daemon update/reset can also
   conflict with a live runner. Add a narrow ownership check at action boundaries,
   not only disabled button styling. Require reception to end before these actions;
   keep read-only status/3D pose display available. No live manual-motion arbitration
   or broker-fed video preview in the first pass.
4. **Repeated Start:** current OPS `start_session_with_options` first calls
   `stop_runner(..., include_unmanaged=True)`. It is a clean-start action, not an
   idempotent ensure-running action. Do not connect it directly to an unguarded Start
   button. Add an app-facing ensure-start operation in OPS that returns the existing
   run when appropriate, rejects conflicting ownership, and stops on failed startup
   stages. Preserve current CLI clean-start semantics.
5. **Concurrency:** serialize lifecycle transitions in the operations layer, including
   repeated clicks and multiple windows. Emergency Stop must remain usable during
   startup; a late startup continuation must not wake or relaunch after Stop. Keep
   operations asynchronous so status and cancellation are not blocked by model loading.
6. **Robot identity:** bind reception actions to the configured production robot.
   Selecting a different discovered robot in the official UI must not silently start
   reception on the clinic robot. Compare configured/selected identity before writes.

### OPS Contract and Monitoring

- Reuse `reception-prod` for validated release/configuration loading, private profile
  selection, and physical authorization. Execute a fixed path with separate argv
  values; do not add an arbitrary shell field or reuse the desktop sidecar Python venv.
- Add first-class unlimited duration for app sessions. Preserve existing timed CLI
  runs; do not implement unlimited operation as an extremely large timeout.
- Return a bounded, non-secret operator summary: run ID, phase, heartbeat ages,
  terminal reason, elapsed time, duration mode, recording flags, production robot,
  profile ID, and release/model/voice identifiers. Do not send profile contents,
  credentials, or raw conversations to the desktop telemetry pipeline.
- Keep monitoring alive outside the Reception panel so navigating away does not
  release ownership guards. Reconcile from OPS on app launch/reopen, including runs
  started by CLI; stale or unreachable status is not evidence of an idle robot.
- Use lightweight local run/heartbeat status polling with bounded calls and no
  overlapping polls. Full `aggregate_status` currently performs provider health
  requests and a retention scan; do not execute it every second. Run heavier checks
  before Start, on explicit refresh, or on a slower cached schedule.

### Proposed Implementation and Acceptance Order

1. Confirm this scope and the fork packaging/update policy. Create a feature branch
   from the pinned tag; leave the original application installed for rollback.
2. Add and unit-test the OPS ensure-start/unlimited/status contract, including
   concurrent Start/Stop and Emergency Stop during startup. No S2S/model changes.
3. Add the narrow Rust adapter and mock-backed Reception panel. Unavailable OPS
   should leave the normal app usable on machines without reception configured.
4. Wire resource/close guards, startup reconciliation, and robot-identity checks.
   Reception must not require Hugging Face login or occupy the official app-store
   current-app slot. Ordinary functions retain their existing behavior when idle.
5. Offline checks: TypeScript, lint, build, component/native/OPS tests; verify normal
   navigation and the web build still work. Test opening/closing/reopening around
   a mocked active run, media reconnect suppression, double Start, and startup Stop.
6. Install a separately named test build on m1max. Confirm ordinary controls while
   idle, then Start/End/Emergency Stop, closing/reopening during a run, and returning
   to normal controls afterward. Observe voice/media continuity while the app stays
   open. These are controlled live acceptance steps, not part of this source review.

Upstream development helpers include cleanup/kill scripts; do not use
`tauri:dev:fresh`, `clean`, or daemon-kill helpers against an active clinic runtime.
Likewise, the custom desktop updater must not silently replace the fork with an
official binary or change the separately managed receptionist/backend environments.

## Installed-App Feasibility: September 9

**Disposition: rejected by the user.** Retained as investigation history, including
the SDK `no_media` correction. Do not implement this controller/remote-runner topology.

### Conclusion And Evidence Boundary

A robot-installed Reception controller with an embedded UI is feasible from the
source review. It can leave inference, private profiles, artifacts, and the existing
reception runner on m1max. It does not automatically share the desktop's media
connection or satisfy the requirement that closing the desktop leaves reception running.

Reviewed the pinned desktop source above, local SDK `1.10.0`, upstream `v1.10.0`
source, and the active m1max release's installed SDK source. Read-only robot HTTP
status confirmed daemon `1.10.0`. Robot SSH key authentication was unavailable;
the robot's installed Python source and apps environment were not inspected.
No package was installed, SDK robot connection constructed, or physical action run.
This is static feasibility, not an installation or media-coexistence acceptance test.

### Confirmed Findings

| Area | Finding | Consequence |
| --- | --- | --- |
| Installation and launch | `reachy_mini_apps` entry-point metadata identifies the module; the daemon launches it with `python -u -m` in the wireless shared apps venv | Package a small controller, not our MLX/vision environment |
| Embedded UI | `custom_app_url` is discovered from the app; desktop `EmbeddedAppView` loads its page in an iframe | Reception controls can appear without adding a desktop panel |
| App status | The manager reports `running` when the subprocess starts, before application readiness | Embedded UI must distinguish controller running from reception ready on m1max |
| Normal controls | Desktop app status activates its existing app-running locks | Better integration than the invisible external runner, but not a hardware-wide ownership guarantee |
| Stop | Manager sends SIGINT, waits up to 20 seconds, then force-kills; afterward it can return the robot to its zero pose | Coordinate cessation of remote audio/motion before local app exit/reset |
| OPS cleanup | `stop_runner` permits 60 seconds for supervised cleanup, 30 seconds otherwise | Cannot assume a blocking remote stop always fits the official app stop budget |
| Desktop close | Wi-Fi `App.tsx` posts `/api/daemon/stop?goto_sleep=false` on window close without an active-app exemption | Installed packaging alone does not meet close-and-continue operation |
| Media preview | Desktop WebRTC auto-connects for an awake robot independently of app-running status | Preview remains a separate connection; coexistence needs measured acceptance |

**Correction to the initial proposal:** do not use `request_media_backend =
"no_media"` for this controller. In SDK `1.10.0`,
`ReachyMini._configure_mediamanager` calls `release_media()` for explicit `no_media`.
That asks the daemon to release camera/audio hardware; it is not a passive
"do not subscribe" option. This behavior was also confirmed in m1max's active
release SDK source. Creating an ordinary SDK instance additionally sets automatic
body yaw, so even a media-free controller should avoid constructing that instance.

The proposed alternative is an app-owned controller entry point/lifecycle that
serves the UI and communicates with OPS without calling the inherited
`wrapped_run()` path that constructs `ReachyMini`. Retain the app class/metadata
expected by discovery and validation; implement signal handling and web-server
shutdown in our own app. The manager's module-launch design permits this in source;
it is not a documented controller-only template and still needs a packaging test.
No patch to the SDK or daemon is proposed for this.

The SDK's central signaling relay has robot-app lock handling. Our LAN Python
WebRTC client instead connects directly to signaling port `8443`. Do not assume
the central relay's ownership behavior arbitrates our existing LAN runner or the
desktop preview. No connection was opened to test this during the investigation.

### Candidate Design

```text
Official desktop app: installed app controls + embedded page
  -> Reception controller on the robot (no SDK hardware/media instance)
    -> private authenticated operations bridge on m1max
      -> existing release-pinned OPS + supervised reception runner
        -> existing robot media/motion and S2S connections
```

The bridge is new infrastructure, unlike the earlier local Tauri adapter. Keep it
limited to named configuration/status/start/stop operations, bound to the intended
robot and private network, with authenticated requests and no arbitrary shell or
path parameters. Browser requests should go to the controller's own origin; keep
bridge credentials server-side, not in page assets. This does not secure the
official daemon's other LAN endpoints; network access remains an administrative
trust boundary. Profiles, provider keys, and recordings stay on m1max.

Required lifecycle work in our code:

- Idempotent ensure-start, serialized transitions, lightweight status, and explicit
  unlimited duration as described in the OPS contract above. Do not blindly call
  the current restart-style `start_session` on repeat requests.
- Track the specific app-owned run and operation IDs. A delayed Stop must not kill
  a replacement run or silently adopt/stop an independently started CLI run.
- Make stop initiation durable on m1max, independent of the robot controller's
  survival. Define how remote robot output becomes quiescent within the daemon's
  stop budget while slower artifact cleanup finishes. Acknowledging receipt of
  Stop is not proof that remote motion/audio stopped.
- Define bounded reconciliation for controller crash, robot restart, and loss of
  control connectivity while media remains live. Existing media liveness checks
  do not necessarily detect loss of this new control channel. A renewable
  app-owned lease is a candidate, not an implemented or approved policy.
- Preserve the managed S2S service and its Interactive process configuration;
  stopping the installed Reception app should end its run, not routinely restart
  or terminate the separate backend service.

### Decision And Prototype Gates

1. Offline packaging/controller prototype with a mock OPS bridge: discovery,
   embedded URL, readiness, signal shutdown, repeated actions, and fault reporting.
   Assert that constructing/running the controller makes no SDK media or motion calls.
2. Resolve stop ordering/budget against the existing supervisor, including a bridge
   outage or controller kill. Do not claim safe app switching before this is handled.
3. Controlled robot acceptance: install/start/stop through the official app; verify
   the existing LAN runner and desktop preview coexist without interrupting audio.
4. Resolve window-close behavior separately: for a prototype keep the official
   desktop open; for close-and-continue production use, verify an upstream fix or
   approve a narrowly scoped desktop change. Do not silently weaken that requirement.

This remains an attractive route for reusing the official app UI and avoiding a
large desktop fork, but it cannot yet be described as a zero-modification production
solution. Full inference migration onto the robot is not part of this proposal.

Primary sources:

- [SDK v1.10.0 app base](https://github.com/pollen-robotics/reachy_mini/blob/v1.10.0/src/reachy_mini/apps/app.py)
- [SDK v1.10.0 app manager](https://github.com/pollen-robotics/reachy_mini/blob/v1.10.0/src/reachy_mini/apps/manager.py)
- [SDK v1.10.0 media selection](https://github.com/pollen-robotics/reachy_mini/blob/v1.10.0/src/reachy_mini/reachy_mini.py)
- [Pinned desktop window lifecycle](https://github.com/pollen-robotics/reachy-mini-desktop-app/blob/467ad30e00855cd5051c8483fed00b4e00b57d1a/src/components/App.tsx)
