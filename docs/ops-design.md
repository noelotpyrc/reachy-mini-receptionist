# OPS design — backend / robot / runner

**Status:** first-pass built and accepted for TODO #5 (`docs/todo-official-runtime.md`;
acceptance record: `docs/archive/reviews/ops-test-todos.md`).
Reframes the original "ops management tools" item around **operational domains** instead of an
ad-hoc command list.

## Goal & scope

Remove the ops confusion seen during live tests with a small, clear command set — shaped so it can
later grow into an operator app, **without building that app now**.

- **Near-term need:** a reliable dev-ops surface for bring-up → live test → teardown on m1max
  (the thing that kept tripping us up: which process to start, in what order, what's actually up).
- **Vision (deferred):** an app for non-tech users to control the robot (start/stop/shutdown, …).
  We design the API so it *can* grow into that, including from another machine controlling m1max,
  but build the library + dev CLI first.

## Core idea — 3 resources × phases

Organize around **resources** (long-lived components, each with its own lifecycle + status) and
**phases** (thin, time-ordered workflows that compose the resources). Resources are the future
app's *panels*; phases are its *buttons*.

> **Naming:** avoid the word "daemon" — it's overloaded three ways (the robot's official `:8000`
> daemon, the legacy reception daemon, and the new runner). Use **Robot OPS** and **Runner OPS**.

A key property splits the resources:

- **Persistent** resources (brought up once, **left up across many runs**): **Backend**, **Robot**.
- **Ephemeral** resource (**one per run; its lifecycle *is* the run**): **Runner** — which is why
  "live-ops" lives inside Runner OPS rather than being a separate domain.

## Resources

### Backend OPS — the m1max S2S backend (`:8765`) — *persistent*
Local speech-to-speech (local STT/TTS + remote LLM via OpenRouter Responses API). Kept **warm**
across runs; only restart it to change model/voice/config or when wedged.
- Actions: `start` / `stop` / `restart`; `status`.
- Status: port live, warm, model + voice loaded, remote-LLM/OpenRouter reachable.
- Safety: **safe** (no robot).

### Robot OPS — the robot via its official `:8000` daemon — *persistent*
Mostly **proxies the official daemon** — don't reinvent robot status; aggregate what `:8000`
already exposes (`/api/daemon/*`, `/api/state/full`, media, motors).
- Actions: reachable check; media `acquire`/`release` (**one session only**); motors on/off;
  `wake`/`sleep`; `state` readout.
- Status: reachable (network/**battery** — battery drop ⇒ offline), awake/asleep, media held by
  whom, motors enabled, SDK/daemon version.
- Safety: **physical** (wake/sleep/motors/media) — confirm.

### Runner OPS — the m1max session-owning process — *ephemeral (one per run)*
The official-runtime live app. Born and dies with the run, so **live-ops is here**.
- Actions: `start` (launch runner for a run) → **live status / `mark`** → `stop`.
- Status: running?, `run_id`, current phase, wedged/desynced detection.
- Safety: **physical** while running (it drives the robot).

## Phases (workflows)

Thin compositions of the resources; idempotent / reconciling where possible ("ensure up" checks
state and does only what's needed — robust to button-mashing).

### Pre-run
clean slate (stop stale runners using Runner OPS state) → ensure **Backend** up → **Robot** acquire
media + wake → **preflight** (exposed substeps: backend health, robot state, speaker WAV, scripted
goodbye, scripted greet) → start **Runner**.

Preflight has two kinds of checks:
- **Machine verification:** process completion, backend health, robot daemon state, media/motor
  state where exposed.
- **Human quality gate:** only where the robot daemon cannot know the real UX, such as "did the
  known-good WAV sound smooth?" or "did goodbye/greet sound acceptable?"

### Post-run
stop **Runner** → **Robot** teardown (`sleep`, motors off; release media if held) → **leave Backend
warm** → write/update the latest-run pointer for #6. Deep artifact sync/review belongs to #6, not
OPS.

### Shutdown
For TODO #5, "shutdown" means the operationally safe end state for the current live-test loop:
**stop Runner + sleep robot + motors off**. It does **not** stop the backend by default, stop the
robot's official daemon, reboot the robot runtime, or power down the robot. Those can become
separate advanced actions later.

## Status model (cross-cutting)

Status is **not** a bucket — each resource publishes its own, and the aggregate is the dev CLI's
`status` readout and the future app's status panel.

- **Source of truth:** Robot ← official `:8000` daemon; Backend ← port/process + a health ping;
  Runner ← ops-owned state file + process validation; latest run / last preflight / errors ← the
  run manifest or latest-run pointer.
- **Derive on demand** wherever possible. Backend and Robot are mostly derived from live checks.
  Runner needs a small state file because OPS launches it and later must stop/status the exact
  process without broad process-killing.
- **Runner state file:** PID, `run_id`, log path, artifact root, started_at, requested config, and
  command. Each status call validates the file against reality: PID alive, run_id files present,
  log/manifest updating when expected. Mismatch becomes `stale_state` with a recovery action.
- Shape: structured/JSON (`{backend:{…}, robot:{…}, runner:{…}, latest_run, last_preflight,
  errors}`) so it doubles as the future UI state model.

## Safe-action model

Do not collapse safety into one vague "confirm" flag. Each action declares three separate things:

1. **Authorization before physical action.** Prevent accidental robot-affecting commands. The CLI
   can require `--confirm-physical` or an interactive prompt; a future app can show a confirmation
   button/dialog. Robot OPS and Runner OPS physical actions require authorization.
2. **Machine verification after action.** Prefer robot daemon state or process/port checks when
   available: wake/sleep/motors/media state, backend health, runner PID, log/manifest updates.
3. **Human quality gate when needed.** Only sensory outcomes that the daemon cannot know require
   human acceptance, such as playback smoothness or whether policy speech sounded acceptable.

Examples:
- `wake-robot`: physical authorization required; verify with daemon/motor state where exposed.
- `sleep-robot`: physical authorization required; verify daemon/motor state where exposed.
- `preflight-audio`: physical authorization required; verify command completion by machine; human
  must accept/reject audio quality.
- `preflight-policy-goodbye` / `preflight-policy-greet`: physical authorization required; verify
  backend/policy/runner completion by machine; human quality gate is optional but recommended
  before live testing.
- `backend start/stop/status`: safe/no robot; no physical authorization.
- `shutdown`: physical authorization required; verify Runner stopped + robot sleep/motors state.

## Architecture (decided)

- **Library + dev CLI first, not a service.** Core ops is a **Python library**: the module owns the
  actions and **returns structured data**. Build a dev CLI on top. No service is required while the
  app is deferred.
- **Replace `live_ops.sh` as the source of truth.** `scripts/m1max/live_ops.sh` should stop owning
  behavior. During migration it may remain as a compatibility shim that calls the Python CLI; after
  the Python path is accepted, it can be removed or kept as a thin alias.
- **Non-tech app deferred.** Build the library + dev CLI first.
- **Forward-compat constraint:** keep the action layer **transport-agnostic** — pure functions
  returning data, **no `print`/CLI logic inside the actions** — so a later remote app can add an
  HTTP/WS service that wraps the *same* library without a rewrite.
- **Orchestrate, don't reinvent.** The ops layer sits *above* three existing things: the official
  robot daemon (`:8000`), the S2S backend process, and the live runner. Today `scripts/m1max/
  live_ops.sh` does this imperatively; the library formalizes it.
- **m1max defaults, env-overridable.** Default paths target the current m1max deployment
  (`/Users/leon/projects/...` and the known-good preflight WAV). Off-m1max use should set
  `REACHY_REPO`, `OFFICIAL_APP_REPO`, `OFFICIAL_RUNTIME_PYTHON`, and `PREFLIGHT_WAV`; missing
  launch paths must fail clearly before starting processes.

## Operator workflow surface (today's CLI → tomorrow's buttons)

| Command | Maps to |
|---|---|
| `status` | safe Backend + Runner aggregate plus latest run; no robot network request by default |
| `status --include-robot` | full 3-resource aggregate, including read-only Robot OPS state |
| `preflight` | composed pre-run gate |
| `preflight backend-health` | backend readiness substep |
| `preflight robot-state` | robot daemon/media/motor state substep |
| `preflight audio-playback` | known-good WAV speaker substep; human quality gate |
| `preflight policy-goodbye` | scripted goodbye through backend/policy/speaker path |
| `preflight policy-greet` | scripted greet through backend/policy/speaker path |
| `start-session` | Pre-run (ensure Backend+Robot ready → start Runner) |
| `stop-session` | Post-run (stop Runner; robot teardown) |
| `shutdown` | stop Runner + sleep robot + motors off |
| `sleep-robot` / `wake-robot` | Robot OPS primitives |
| `backend {start,stop,restart,status}` | Backend OPS primitives |
| `latest-run` | prints latest run pointer for #6 |
| `review <run_id>` | launches #6 / Rerun (read-only) |

## Relationship to #6 (diagnosis)

Post-run writes a minimal latest-run pointer; #6 owns artifact collection, `.rrd` production, and
run-summary/review. Rerun is a read-only diagnosis surface, **not** the operator control app — keep
them as separate surfaces that share the manifest/artifacts as data. See `docs/rerun-integration.md`
→ "Relationship to OPS (#5)" for the three boundaries.

## Suggested build order

1. **Library + dev CLI — resource primitives:** Backend/Robot/Runner `start`/`stop`/`status` +
   Runner state file + the status aggregator (returns structured data). This alone removes most
   live-test confusion.
2. **Phase workflows:** `pre-run` / `post-run` composing the primitives, idempotent.
3. **Latest-run handoff:** write/read a minimal latest-run pointer for #6.
4. **Deferred:** HTTP/WS service + non-tech app (wrap the same library).
