# Documentation Index

This index identifies the current source of truth for each area. Documents under `archive/` are
historical evidence, not current operating instructions. When an active specification and an old
research document disagree, follow the active specification.

## Production And Operations

- [`production-readiness.md`](production-readiness.md) - promotion gates for an unattended clinic
  run and the current pass/block status.
- [`runbook.md`](runbook.md) - current m1max release, preflight, start, stop, backend lifecycle, and
  rollback procedure.
- [`ops-design.md`](ops-design.md) - Backend/Robot/Runner ownership, safety model, and the foundation
  for a future remote service and operator app.
- [`reception-control-app.md`](reception-control-app.md) - proposed local-first extension of Reachy
  Mini Control with administrative reception-run controls.
- [`runtime-test-catalog.md`](runtime-test-catalog.md) - available offline, integration, production
  smoke, and physical test harnesses.
- [`robot-runtime-debugging.md`](robot-runtime-debugging.md) - robot-side media and runtime diagnosis.
- [`live-test-log.md`](live-test-log.md) - chronological evidence from physical robot runs.

## Active Product Work

- [`todo-official-runtime.md`](todo-official-runtime.md) - engineering work queue. Production
  readiness now determines priority; backend feature experiments are paused.
- [`s2s-main-migration.md`](s2s-main-migration.md) - approved migration from the frozen S2S fork
  to a pinned upstream `main`, including client-side profile composition, tool execution, and the
  long-term-memory extension boundary.
- [`vision-visitor-state-proposal.md`](vision-visitor-state-proposal.md) - implemented door/person
  greet-goodbye policy and its remaining live acceptance.
- [`hermes-s2s-fork-spec.md`](hermes-s2s-fork-spec.md) - deployed Hermes session integration and
  deterministic policy-speech contract. This remains the production rollback and compatibility
  reference during the migration.
- [`two-profile-architecture.md`](two-profile-architecture.md) - proposed owner assistant and
  patient receptionist split, structured session store, and human-gated learning loop. Not
  implemented.
- [`legacy-cleanup-plan.md`](legacy-cleanup-plan.md) - non-destructive inventory and separately
  approved removal plan for the old daemon stack.

## Artifacts And Diagnosis

- [`data-harness.md`](data-harness.md) - current artifact taxonomy, recording behavior, and known
  evidence gaps.
- [`general-timeline-model.md`](general-timeline-model.md) - renderer-independent event/span model.
- [`conversation-audio-player.md`](conversation-audio-player.md) - aligned audio and semantic lane
  review tool.
- [`rerun-integration.md`](rerun-integration.md) - offline general-timeline renderer.
- [`rerun-vision-tracking-plan.md`](rerun-vision-tracking-plan.md) - implemented offline and live
  vision/detection views.
- [`head-pose-calibration-notes.md`](head-pose-calibration-notes.md) - retained calibration reference.

## Operator And Developer Reference

- [`robot-guide.md`](robot-guide.md) - manual robot utilities and current reception OPS entrypoint.
- [`archive/research/hf-s2s-m1max-backend.md`](archive/research/hf-s2s-m1max-backend.md) - historical
  backend feasibility and baseline measurements; use the runbook for current operation.

## Historical Material

[`archive/`](archive/) contains superseded plans, research, implementation reviews, and legacy
architecture. Archived documents provide traceability but are not current operating instructions.
