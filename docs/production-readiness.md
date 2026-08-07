# Production Readiness

**Status:** not yet approved for unattended clinic production

**Current phase:** assisted production preparation

**Updated:** 2026-08-06

This document is the promotion checklist for running the receptionist in the clinic for an extended
period without a developer onsite. It owns pass/block status. Detailed implementation and operating
instructions remain in the linked specifications and [runbook](runbook.md).

Backend feature development is paused at the current deployed configuration. Production work may
make reliability, security, lifecycle, and observability fixes, but should not change the selected
STT/LLM/TTS behavior without reopening backend evaluation explicitly.

## Current Baseline

- Product release: clean m1max checkout at commit `612ea43`, with the previous dirty deployment
  preserved as a rollback copy.
- S2S backend: `speech-to-speech==0.2.10`, fork SHA `a963ca68b9aa3599b7ea5eeabb9505a68263fbff`,
  listening only on `127.0.0.1:8765`.
- Agent wrapper: production Hermes profile on `127.0.0.1:8642`; clinic context remains outside Git.
- Direct provider/model: OpenRouter with `openai/gpt-5.6-luna` as the configured direct model.
- Policy speech: fixed greet/goodbye text uses deterministic TTS and does not invoke an LLM.
- Visitor policy candidate: `door-v1-20260805`, combining semantic door movement with person-door
  interaction.
- Operational surface: `ops_core` plus `reception-ops`; structured status and physical-action
  authorization are implemented.
- Diagnosis: raw/derived artifacts, audio review, offline Rerun review, door-policy review, and
  latest-run pointers are available.

## Promotion Gates

| Area | Gate | Status | Evidence / remaining work |
| --- | --- | --- | --- |
| Release | Immutable product revision, reproducible venv, documented rollback | **Pass for assisted use** | Clean release and release-owned `.release-venv` exist; replace commit-named/manual activation with a stable production release mechanism before non-technical operation. |
| Backend | Reproducible pinned runtime and production smoke | **Pass / frozen** | Backend setup script, runtime metadata, Hermes text/integration tests, and deterministic policy-TTS benchmark completed. Add wrapper/provider checks to aggregate health. |
| Robot lifecycle | Remote start, stop, sleep, and machine verification | **Pass for assisted use** | OPS start/stop lifecycle works and leaves the backend warm. Physical runner restart must remain operator-authorized. |
| Visitor behavior | Greet, goodbye, and wave-chat accepted with real visitors | **Blocked** | Door policy passed captured offline evaluation. A controlled live door-entry, conversation, and exit sequence is still required. |
| Long-run behavior | Multi-hour run with conversations and no wedged subsystems | **Blocked** | `official-live-20260806-114813` ran for about 96 minutes but observed no people or conversations, so it tested idle stability only. |
| Startup | Bounded, observable transition to ready | **Needs work** | Fresh release startup took roughly two minutes while DINO/RF-DETR and media initialized. Expose progress and define a timeout/fault state. |
| Session duration | First-class run-until-stopped mode | **Blocked** | Current operation uses a very large numeric duration as an indefinite-run workaround. Implement explicit unlimited semantics. |
| Recording integrity | Audio/video/capture finalize and align for a long run | **Blocked** | The latest input loop covered about `5740 s`, while the finalized MKV reports about `3311 s`. Diagnose frame timestamps/container duration before relying on continuous video. |
| Crash recording | Artifacts remain finalized or explicitly interrupted after runner failure | **Blocked** | Recorder remains runner-owned. Implement or consciously defer TODO #10's recorder sidecar with a documented production fallback. |
| Privacy | Approved raw-data policy, access boundaries, and retention | **Decision required** | Continuous audio plus video is about `0.4 GB/hour` in the latest run and may contain sensitive clinic information. Decide production defaults and retention before unattended recording. |
| Monitoring | Backend, Hermes, provider, runner, media flow, artifacts, disk, and robot health | **Blocked** | Current aggregate status covers backend port/process, runner, and optional robot state. It does not yet prove wrapper/provider health or active media/artifact progress. |
| Supervision | Services recover safely after machine/process failure | **Blocked** | Backend and wrapper need managed service definitions. A physical runner must not auto-restart without an explicit safety policy. |
| Remote access | Authenticated, auditable, least-privilege control | **Blocked for non-technical users** | SSH/Tailscale plus OPS is acceptable for assisted production. No remote operations API or operator UI exists yet. |
| Emergency handling | Idempotent remote stop and documented local fallback | **Partial** | `stop-session` and `shutdown` exist. Define an operator-visible emergency stop, timeout behavior, and recovery instructions. |

## Latest Long-Run Evidence

Run `official-live-20260806-114813` used the clean release with raw audio, video, vision capture,
Grounding DINO door observations, and Rerun streaming disabled.

- Audio and video warmups passed; the runtime reached `software_pipeline_initialized`.
- Door semantic updates detected exactly one door in `4397 / 4397` samples over the analyzed
  50-minute window. The state remained `STABLE` after two startup `UNKNOWN` frames and emitted no
  false policy trigger.
- No person was observed during that analyzed window, so person-door interaction and visitor policy
  behavior were not exercised.
- Graceful OPS shutdown stopped the runner, slept the robot, disabled motors, released media, and
  finalized the artifacts.
- The input loop reported about `5740 s`, but `ffprobe` reports approximately `3311 s` for the MKV.
  This confirms the open frame-timestamp/container-duration issue recorded in
  `docs/archive/reviews/rerun-review-issues-20260626.md`.

## Assisted Production Sequence

Complete these in order. Stop and diagnose when a gate fails.

1. Correct active documentation and establish this checklist as the promotion source of truth.
2. Diagnose and fix or explicitly bound the long-run video duration/alignment problem.
3. Implement explicit unlimited session duration and startup progress/fault reporting.
4. Define the production configuration outside Git: release, visitor profile, recording mode,
   retention, provider, voice, and rollback target.
5. Add authenticated health checks for Hermes and the external provider plus media/artifact growth
   checks for an active run.
6. Add service supervision for non-physical persistent services. Keep physical runner recovery
   manual unless a separate safety review approves automatic restart.
7. Run one controlled visitor acceptance: door entry, greet, wave-chat, ordinary questions,
   goodbye, and door exit, with artifacts retained for review.
8. Run one assisted clinic shift with remote status checks and a tested emergency stop procedure.
9. Review evidence and explicitly promote or roll back.

## Remote Operations Roadmap

Remote operation is staged so a web app is not a prerequisite for the first assisted production
run.

1. **Assisted production:** use the existing OPS library/CLI over authenticated SSH/Tailscale.
2. **Operations service:** wrap `ops_core` directly with a local authenticated API, asynchronous
   jobs, one operation lock, idempotency keys, progress events, and an audit log. Do not expose an
   arbitrary command endpoint.
3. **Remote CLI:** add `status`, `start-shift`, `end-shift`, `emergency-stop`, and `mark` commands
   against the API; retain the local OPS CLI as the break-glass path.
4. **Operator web app:** expose a narrow Start Shift / End Shift / Emergency Stop workflow with
   Ready, Starting, Live, Stopping, and Fault states. Keep backend/model/profile controls
   administrator-only.

The robot daemon, Hermes port, and S2S backend port must remain private. A future operations service
should bind locally and be exposed only through an authenticated private-network or identity-aware
proxy.

## Explicit Non-Goals For This Pass

- No backend model, prompt, memory, STT, or TTS experiments.
- No public Internet exposure of robot or backend services.
- No automatic physical runner restart.
- No deletion of legacy code, documents, artifacts, profiles, or database entries without separate
  confirmation.
