# Reception Control In Reachy Mini Control

**Status:** proposed first pass

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
