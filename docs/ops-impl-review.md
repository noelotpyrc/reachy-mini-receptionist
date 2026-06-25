# Review — OPS management implementation (#5)

**Reviewed:** `src/reachy_mini_brain/official_runtime/ops_core.py` (library),
`ops_cli.py` (CLI), `tests/test_ops_management.py`, `pyproject.toml` (`reception-ops`
entry point), and the `live_app.py` change (scripted-policy-flow support).
**Against:** `docs/ops-design.md` (settled design).
**Method:** full read + offline test run (`61 passed`: ops + official-runtime +
audio-pacing) + CLI smoke (`reception-ops --help` loads). No robot-touching commands were
executed.

## Verdict

**Strong, faithful implementation.** It matches the settled design closely — 3 resources ×
phases, the three-part safety model, the runner state file, a transport-agnostic library with a
thin CLI, and the minimal latest-run handoff to #6. Offline tests pass and the `live_app.py`
change didn't regress the suite. **One operational risk should be fixed before this replaces
`live_ops.sh` as the source of truth** (launched processes don't survive SSH / don't keep the Mac
awake), plus a handful of smaller gaps. None are architectural — the bones are right.

## Post-review status

The blocking operational risk and first-pass gaps called out below have been addressed for the
accepted OPS v1:

- Backend/runner launch now detaches with process tracking and a `caffeinate -w <pid>` watcher.
- Runner primitives are exposed through the CLI.
- Workflow/status/backend paths are covered by focused tests.
- The m1max clean deployment path and uv-managed venv are the default.
- Onsite robot/human gates and the offline backend churn test are accepted in
  `docs/ops-test-todos.md`.

The detailed notes below are retained as the original implementation review record.

## 🟢 What's done well (matches the design)

- **Library / CLI split is exactly the decided shape.** `ops_core` actions return `ActionResult`
  dataclasses and never print (`ops_core.py:1-6`); `ops_cli` is a thin Click wrapper. Transport-
  agnostic action layer, as decided — a future service can wrap the same functions.
- **Three-part safety model implemented, not collapsed into one "confirm" flag** (the design's
  explicit ask): authorization (`_require_physical_authorization` `ops_core.py:885`), machine
  verification (`Verification` `:129`), human quality gate (`HumanQualityGate` `:139`).
- **Authorization is enforced *before* any robot call** — proven by test: zero `_robot_post`
  calls when unauthorized (`test_ops_management.py:39-51`). This is the most important safety
  property and it's covered.
- **3 resources present.** Backend (status/start/stop/restart), Robot (status/wake/sleep/
  stop_running_moves, **proxying the official `:8000` daemon** — `:309-401`, matches "orchestrate,
  don't reinvent"), Runner (status/start/stop).
- **Runner state file** with PID + run_id + manifest, validated against reality →
  `stale_state` / `unmanaged_running` detection (`:404-434`). Matches the design's runner-state
  spec precisely.
- **Idempotent / reconciling:** `start_backend` and `start_runner` short-circuit when already
  running (`:235`, `:453`); status derives from live checks.
- **Phases + scoped shutdown.** `start_session` / `stop_session` / `full_preflight` (`:663-688`);
  `shutdown` = stop runner + sleep + motors off and **does not** stop the backend or robot daemon
  (`:540-545`) — exactly the design's scoped definition.
- **Structured JSON output** (`--json-output`) → future UI state model; **latest-run pointer**
  (not full collection) → matches the revised #6 boundary (ops writes the pointer; #6 owns
  collection/`.rrd`/review).

## 🔴 Should fix before relying on it

**R1 — Launched processes don't detach or keep the Mac awake.** Both the backend (`:257`) and the
runner (`:482`) start via plain `subprocess.Popen` — **no `start_new_session`/setsid, no `nohup`,
no `caffeinate`** — and `run_s2s_backend.sh` just `exec`s the backend in the foreground. The
`live_ops.sh` flow this replaces used `nohup caffeinate -dimsu`. On m1max driven over SSH/Tailscale
(the primary use case), this means:
- the runner/backend can take **SIGHUP and die when the SSH session closes**, and
- the Mac can **sleep mid-session**.

Fix: launch with `start_new_session=True` and wrap (or shell out to) `caffeinate`, or document that
`reception-ops` must run inside `tmux` + `caffeinate`. Until then a `start-session` over a closing
SSH shell is unreliable.

## 🟡 Gaps worth addressing

**G1 — Runner primitives aren't exposed in the CLI.** `start_runner` / `stop_runner` /
`runner_status` exist in core but there's no `runner start|stop|status` command — only reachable via
`start-session` / `stop-session` / `status`. The design's build-order #1 lists Runner start/stop/
status as resource primitives (parity with the `backend` group). Add a `runner` command group so you
can restart just the runner or start one against an already-warm robot without the full session.

**G2 — `status` excludes the robot by default.** `--include-robot` is opt-in (`ops_cli.py:41`,
`aggregate_status(..., include_robot=False)` `:691`), so plain `status` reports only Backend +
Runner. Design says status is the aggregate of all 3 resources. Defensible (robot may be unreachable
on battery → avoids always-degraded), but either include robot by default with graceful degradation,
or document that robot needs `--include-robot` and update `ops-design.md` to match.

**G3 — Orchestration paths are under-tested.** Tests cover the safety-critical and state logic (auth-
before-robot, stale-state, command-building, CLI-block, latest-run roundtrip) — good. But the
composition workflows (`start_session` / `stop_session` / `shutdown` / `full_preflight`), backend
start/stop, and `aggregate_status` have **no unit tests**; these are the most operationally complex
sequences. Add tests mocking `_robot_post` / `_port_open` / `subprocess`.

**G4 — m1max-specific hardcoded defaults.** `OpsConfig.from_env` defaults `official_app_repo` to
`/Users/leon/projects/reachy_mini_conversation_app` (`:72`) and a specific `DEFAULT_PREFLIGHT_WAV`
(`:29-32`); `_default_python_bin` prefers the official app's venv (`:836`). All env-overridable, and
fine for an m1max tool, but they won't work off-m1max without overrides — worth a clear error when a
required path is missing, and a one-line note in `ops-design.md`.

## Minor

- **Backend process management is pattern-based** (`pgrep -f "speech-to-speech --mode realtime"`,
  `:28`/`:218`) with no state file — fragile vs the runner's state-file approach (could miss/over-
  match). Mirrors `live_ops.sh`, so not a regression; consider extending the state-file pattern to
  the backend later.
- **No concurrency guard** on the runner state file / `start_runner`; two concurrent invocations can
  race (status-check narrows but doesn't close the window). Fine for a single operator; revisit for
  the future multi-client app.
- **`stop_running_moves` is `safety="physical"` but not authorization-gated** (`:379`). Only reached
  internally (inside `sleep_robot` / preflight), never exposed in the CLI, so there's no external
  unauthorized path — but if it's ever exposed, gate it.
- **`wake_robot` acquires media without pre-checking the one-session constraint** (`:339`); could
  fail if the official Control app holds media. Robustness nicety.

## Note on the bundled runtime change

`live_app.py` changed (+37/−34) to add **scripted-policy-flow** support (`goodbye` / `greet` /
`goodbye-greet`) that `preflight_policy` drives. The offline suite (`test_official_runtime.py`)
passes, so no regression detected — but it *is* a runtime change bundled into #5, and the scripted-
policy flow drives the robot, so its live behavior should be validated on-robot before the preflight
workflow is trusted as a gate.

## Recommendation

Accept the implementation as the #5 foundation — it's faithful and tested. Before it replaces
`live_ops.sh` as the source of truth: fix **R1** (detachment + caffeinate) and decide **G2**
(robot-in-status default). G1/G3/G4 are good follow-ups. The scripted-policy `live_app.py` change
wants one live validation pass.
