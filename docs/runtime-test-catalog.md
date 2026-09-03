# Runtime Test Catalog

This document maps the existing runtime tests to their purpose and expected
use. It is intentionally a catalog, not a test-suite entry point. Run each
harness directly so model experiments, S2S integration tests, and physical
robot tests remain independently controlled.

## Operating Rules

- Use `reachyclinic-test` on port 8643 for development, profile behavior, and
  model comparison. Use `reachyclinic` on port 8642 only for the small
  post-deployment smoke test and approved live operation.
- A Hermes request's `model` field is cosmetic. To benchmark a different model,
  change the staging profile's effective model configuration, restart that
  gateway, and record the effective configuration with the result.
- Keep generated reports under ignored `artifacts/` directories. The
  conversation benchmark includes prompts and response text; treat reports
  from a real clinic profile as private.
- Do not delete profile sessions, response database entries, reports, or other
  test state without explicit approval. Production smoke conversations may
  remain in the response database.
- A latency pass is not a quality pass. Review response text wherever the
  harness does not implement semantic assertions.

## 1. Harness And Configuration Tests

These tests exercise local parsing, report generation, command construction,
profile synchronization guards, replay lifecycle handling, and OPS behavior.
They do not call Hermes, OpenRouter, S2S, or the robot.

```bash
.venv/bin/python -m pytest \
  tests/test_hermes_text_benchmark.py \
  tests/test_hermes_conversation_benchmark.py \
  tests/test_direct_policy_benchmark.py \
  tests/test_s2s_replay.py \
  tests/test_s2s_replay_fixture.py \
  tests/test_ops_management.py
```

Sources:

- [Hermes text benchmark tests](../tests/test_hermes_text_benchmark.py)
- [Conversation benchmark tests](../tests/test_hermes_conversation_benchmark.py)
- [Direct-policy benchmark tests](../tests/test_direct_policy_benchmark.py)
- [S2S replay tests](../tests/test_s2s_replay.py)
- [Replay fixture tests](../tests/test_s2s_replay_fixture.py)
- [OPS and profile-management tests](../tests/test_ops_management.py)

Run the profile sync tool with `--dry-run` to validate a candidate bundle and
review its managed-file/config changes without writing the target profile:

```bash
scripts/m1max/sync_hermes_profile.sh \
  --profile reachyclinic-test \
  --source-dir <profile-source-dir> \
  --dry-run
```

Source: [Hermes profile sync](../scripts/m1max/sync_hermes_profile.sh).

## 2. Profile Text Behavior

### S2S text-only profile and history smoke

[test_s2s_text_profile.py](../scripts/m1max/test_s2s_text_profile.py) composes the
tracked fictional profile, sends two text-only turns over one Realtime
WebSocket, and checks profile facts, backend-local conversation continuity, and
the absence of audio events. With migration staging bound to m1max localhost
port `8766`, expose it temporarily to this development machine:

```bash
ssh -N -L 18766:127.0.0.1:8766 leon@100.127.86.67

.venv/bin/python scripts/m1max/test_s2s_text_profile.py \
  --url ws://127.0.0.1:18766/v1/realtime \
  --profile-dir profiles/clinic_receptionist \
  --profile-id lakeside-test \
  --output artifacts/s2s-main-migration/<run-id>.json
```

Run the harness from the product environment, not the backend-only venv. The
report contains the synthetic prompts and answers and records only safe profile
provenance, not the composed instruction text.

### Staging behavior suite

Use profile-specific text cases to check approved facts, supported and
unsupported actions, medical and emergency boundaries, unknown information,
staff awareness, conversation isolation, and memory behavior. Real-clinic
inputs, expected facts, and review results live in the ignored private profile
review rather than tracked fixtures:

- `private/profiles/clinic_receptionist/REVIEW.md`

The current seven-area staging review is recorded there. It was run against
`reachyclinic-test`, not the production gateway.

### Fixed synthetic checks

[benchmark_hermes_text.py](../scripts/m1max/benchmark_hermes_text.py) runs three
fixed fictional scenarios: clinic facts, unsupported appointment action, and
continued-conversation recall. It automatically checks required response
phrases, rejects tool calls, and reports TTFT, total latency, and token usage.
Its clinic-fact expectations belong to the tracked fictional profile. Run it
only when the selected staging profile contains those matching facts; the
current `reachyclinic-test` profile may instead contain a private candidate
bundle. Do not use this fixed scenario set as a real-clinic production smoke
test.

```bash
.venv/bin/python scripts/m1max/benchmark_hermes_text.py \
  --target hermes \
  --hermes-url http://127.0.0.1:8643/v1/responses \
  --model openai/gpt-5.6-luna \
  --runs 10 \
  --warmups 1 \
  --output artifacts/hermes-text-benchmarks/<run-id>.json
```

The selected profile's API credential must already be exported as
`HERMES_API_KEY` or `API_SERVER_KEY`.

### Production smoke

After an approved profile sync and gateway restart, run the two non-personal
turns in the ignored private `production_smoke.json` against port 8642. This
checks that production loaded both clinic facts and capability boundaries. It
does not involve S2S, audio, or the robot.

```bash
.venv/bin/python scripts/m1max/benchmark_hermes_conversation.py \
  --manifest private/profiles/clinic_receptionist/production_smoke.json \
  --hermes-url http://127.0.0.1:8642/v1/responses \
  --runs 1 \
  --warmups 0 \
  --output artifacts/hermes-production-smoke/<run-id>.json
```

Review both responses and the recorded tool-call count manually. The
conversation benchmark preserves `semantic_check` as a review instruction but
does not evaluate it automatically.

## 3. Model Quality And Latency

Use [benchmark_hermes_conversation.py](../scripts/m1max/benchmark_hermes_conversation.py)
for repeated, stateful text conversations. The stable 12-turn S2S fixture's
manifest also supplies the expected transcripts and per-turn semantic review
notes, allowing the same conversation to be tested without STT or TTS.

```bash
.venv/bin/python scripts/m1max/benchmark_hermes_conversation.py \
  --manifest artifacts/hermes-s2s-e2e/official-live-20260625-133754/stable-v2/manifest.json \
  --hermes-url http://127.0.0.1:8643/v1/responses \
  --runs 3 \
  --warmups 1 \
  --output artifacts/hermes-conversation-benchmarks/<model>-<run-id>.json
```

The report records complete prompts and responses, TTFT, completion latency,
tokens, tool calls, and per-run distributions. Response quality is a manual
review against each turn's `semantic_check`. Use one fresh conversation per
run and the same manifest/runs/warmups when comparing candidates.

For provider-vs-Hermes latency isolation, use the same
[fixed text benchmark](../scripts/m1max/benchmark_hermes_text.py) with
`--target both`; the direct target additionally requires
`--direct-instructions-file`. The interpretation and observer limitations are
documented in [Hermes S2S fork spec section 6b](hermes-s2s-fork-spec.md#6b-text-only-latency-benchmark)
and [section 6c](hermes-s2s-fork-spec.md#6c-hermes-latency-observer).

## 4. Direct Policy Benchmark

[benchmark_direct_policy.py](../scripts/m1max/benchmark_direct_policy.py)
measures the direct OpenRouter greet/goodbye lane. It records exact output
matches, TTFT, completion latency, token usage, resolved model, and provider.
It does not use Hermes or the S2S audio path.

```bash
.venv/bin/python scripts/m1max/benchmark_direct_policy.py \
  --runs 30 \
  --warmups 1 \
  --output artifacts/direct-policy-benchmarks/<run-id>.json
```

`OPENROUTER_API_KEY` must already be exported. The default request shape
matches the runtime lane by omitting explicit reasoning and provider-routing
overrides.

## 5. S2S Integration Replay

[s2s_replay.py](../src/reachy_mini_brain/official_runtime/s2s_replay.py)
streams selected reviewed WAV turns through one S2S WebSocket conversation. It
checks input transcript completion, assistant transcript availability, first
audio, audio completion, response completion, and realtime error events. It
writes each output WAV and records its sample count for result and audio-quality
review.

```bash
.venv/bin/python -m reachy_mini_brain.official_runtime.s2s_replay \
  --turns 1-12 \
  --output-dir artifacts/hermes-s2s-e2e-runs/<run-id>
```

Prepare or verify the stable fixture with
[s2s_replay_fixture.py](../src/reachy_mini_brain/official_runtime/s2s_replay_fixture.py).
The accepted fixture, known STT limitation, and prior replay results are
documented in [Hermes S2S fork spec section 8](hermes-s2s-fork-spec.md#8-acceptance-checks-staging-profile-first-live-last).

This replay validates integration plumbing and conversation continuity. It
does not replace the text-only model-quality review because STT and TTS add
their own variability.

## 6. Physical Runtime Validation

Use the canonical OPS commands in the [live runbook](runbook.md#start-a-live-test).
Preflight and live testing cover robot state, speaker playback, policy speech,
vision policy, room acoustics, recordings, barge-in, and visitor-session
lifecycle that offline tests cannot establish.

Run physical tests only after the selected profile/model has passed the
appropriate text and S2S checks. Record significant runs and findings in the
[live test log](live-test-log.md).

## Practical Gates

| Change | Minimum checks before proceeding |
| --- | --- |
| Benchmark/replay code | Harness and configuration tests |
| Profile wording or facts | Sync dry run, staging profile behavior suite |
| Production profile sync | Production smoke after gateway restart |
| Model/provider candidate | Fixed text latency checks plus repeated 12-turn text quality review |
| S2S/fork/session behavior | 12-turn WAV integration replay |
| Robot/live behavior | OPS preflight, then supervised live scenarios |
