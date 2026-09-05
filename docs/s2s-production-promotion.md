# Client-Owned Agent Production Promotion

Status: production activated on 2026-09-05; managed non-hardware acceptance passed.

## Accepted Candidate

- Backend fork: `2e4449c345c305e4ee6b9761f86c1849bbf3cb08`, S2S `0.2.12`.
- Python on m1max: CPython `3.12.13`; application SDK `reachy-mini==1.10.0`.
- Backend: `mlx==0.31.1`, `mlx-audio==0.4.2`, `mlx-lm==0.31.1`, `mlx-metal==0.31.1`.
- Provider: direct OpenRouter `openai/gpt-5.6-luna`, matching the tested staging launcher.
- Smart Turn disabled; Qwen Sohee voice and existing sentence batching unchanged.
- launchd S2S `ProcessType=Interactive` retained. No physical runner is started automatically.
- Real private Acugenie profile: original Hermes personality/base/facts/capabilities,
  shared spoken guidance, organized tool guidance, and runtime NY date context.
- Tools default to `time-web`; explicit `none` remains supported. Test references
  are enabled only by `reference-test`.

The organized instruction passed seven text-only cases with hash
`fc822899903f985241556ae34cf25d00695edc4c96f61c845d13777cc1d735eb`.
The runtime date changes that hash on a new local date. Private profile files and
credentials stay outside Git and outside the frozen app's tracked source tree.

## Managed Wiring

The approved Hermes-source path is selected with
`RECEPTION_AGENT_PROFILE_FORMAT=hermes`. It requires the original private source
directory and does not load the public `instructions.txt` containing Lakeside facts.
`RECEPTION_AGENT_PROFILE_ID` is identity/provenance, not a selector for source contents.

`run_s2s_backend.sh` supports `S2S_CLI_MODE=serve` and keeps `legacy` for rollback.
Managed environment loading applies the shared credential environment first, then
`production.env`; the backend launcher does not reload old route settings afterward.
The configured fork SHA and dependency versions are verified before model startup.

Direct routing sets `S2S_RESPONSES_CONVERSATION=0`. Health does not call or require
Hermes in this mode, and installation does not start/restart its compatibility service.
Existing Hermes files/services are preserved. Stateful routing can still be selected
explicitly in a compatible rollback configuration.

Backend process matching includes the new `serve` syntax and its configured port,
so production stop/restart does not capture a separate staging listener.

## Retention

`S2S_EVENT_TRACE_DIR` points into the shared artifacts directory. The 30-day
`recording-retention` report includes that root, or defaults to the sibling
`s2s-backend-trace` directory. Age uses modification time; an actively written
trace is not considered idle based on its original creation time. The report
lists due files and sizes and never removes them. The existing m1max daily
report-only job is retained; no local reminder job is enabled.

## Freeze and Rollback

Before switching, retain a private snapshot of:

- old `active-release` and `production.env`;
- stable managed-service launchers and installed launchd plists;
- shared environment before credential provisioning;
- new source profile hashes, app revision, backend revision, and package inventory.

Build a new versioned frozen app directory and a separate backend directory.
Do not rsync over the dirty deploy checkout, upgrade its environment, or remove
the old backend. Preserve active production app `7840866` for rollback.

For rollback, stop only the new managed S2S service, restore the saved production
configuration and active-release pointer, reinstall the old release's stable
launchers with its installer, then verify old managed startup. The new private
profile and backend directories may remain on disk. No deletion is needed.

## Acceptance Sequence

1. Local unit suite and shell/lint validation.
2. Freeze and validate matching app/backend dependencies on m1max.
3. Check private live-profile composition matches the reviewed instruction,
   and confirm only the approved tools are advertised.
4. Switch managed startup while the physical runner is stopped.
5. Verify service PID, Interactive mode, pool readiness, and trace health.
6. Generate real-profile text/tool responses and policy-TTS audio without a robot sink.
7. Verify managed shutdown releases the process and port; restart and recheck health.
8. Run report-only retention and retain the release manifest and acceptance results.

Stop on acceptance failures and record whether rollback was necessary. None of
these checks requires the user to be at the clinic, but they do not establish
physical speaker quality or replace the later live acceptance of profile/tools.

## Promotion Attempt: 2026-09-04

- App commit `d55159ea37b6a1974863e8bfb1a1987017e5592a` committed and pushed.
- Local suite: 375 passed, 1 skipped, 36 deselected.
- New m1max app: `/Users/leon/projects/reachy_mini_receptionist_release_d55159e_frozen`.
- New backend: `/Users/leon/projects/speech_to_speech_backend_2e4449c_frozen`.
- New app environment built from `uv.lock`; backend environment reproduced from
  the tested staging package inventory. Both package consistency checks passed;
  backend fork/MLX verification passed.
- Private source profile copied to
  `/Users/leon/.config/reachy-reception/profiles/reachyclinic-v1`, owner-only.
- Existing shared Firecrawl key provisioned in m1max's backed-up deployment `.env`.
- Private rollback snapshot and inventories:
  `/Users/leon/.local/state/reachy-reception/promotions/20260904-d55159e`.

The m1max suite produced 374 passes, 1 failure, 1 skip, and 36 deselections.
`test_s2s_setup_bootstraps_unseeded_uv_venv_without_python_pip` invokes the staging
setup script in dry-run mode but does not pass `--skip-running-check`. The real
staging listener on port 8766 therefore causes the script to exit with code 3
before the dry-run assertions. The test passed locally because no backend was
listening on the local 8766 port.

Stopped before activation. Production `active-release` remains `7840866` and
production.env was not switched. No robot run was started; staging was left
running. Managed startup/generation/shutdown, private live-profile validation,
and the deployed retention report remain pending.

Next correction: make this mocked dry-run test independent of the real staging
listener using its existing `--skip-running-check` option, then freeze the
corrected app revision and repeat m1max validation before activation. There is
no reason to stop the real staging server merely to satisfy this unit test.

## Retry: 2026-09-05

The mocked dry-run test now passes `--skip-running-check`, matching the other
isolated setup test. Production setup guards are unchanged. The corrected app
is frozen at `37c7042ae79a5f367bd2637df266e09661c21846`; its local and m1max
suites both passed: 375 passed, 1 skipped, 36 deselected. The frozen backend
and original rollback snapshot were reused without modifying the old runtimes.

### Activated Combination

- App: `/Users/leon/projects/reachy_mini_receptionist_release_37c7042_frozen`.
- Backend: `/Users/leon/projects/speech_to_speech_backend_2e4449c_frozen`.
- Production endpoint: `ws://127.0.0.1:8765/v1/realtime`.
- Direct OpenRouter, `openai/gpt-5.6-luna`; Smart Turn disabled.
- Verified backend package pins and fork SHA as listed above; Interactive scheduling.
- Private profile: `reachyclinic-v1`, Hermes-source composition, `time-web` tools.
- Profile with tool guidance, before runtime date: SHA-256
  `b09a8ed2346247a2490d522f48484fa78a3cfe83c7d2744958387d7f55ad2d4b`.
- September 5 date-stamped instruction: 6,797 characters, SHA-256
  `33da4e1f0acc996d06aff04d9df1ceedbb26a480fa8a905797555fe5dcb45fc5`.
- Existing vision, recording, and shift settings preserved. Backend startup wait
  configured to 180 seconds for cold model initialization.

### Managed Acceptance Results

| Check | Result |
| --- | --- |
| Private composition | Matches the approved local source exactly before date stamping; no Lakeside content; only `time_now` and `web_search` advertised |
| Clinic facts / small talk | Correct clinic opening hours, then short conversational response; zero tools; 1.235 s / 1.126 s |
| Local time | One `time_now` call with America/New_York; Saturday and clock time returned; 2.053 s |
| Web search | One real Firecrawl call, three bounded results, 34-word MVC renewal answer; 3.107 s |
| Fixed policy TTS | Greet and goodbye both completed with exact transcripts and nonempty WAVs; first audio 208.053 ms / 150.830 ms |
| Shutdown | Managed job unloaded, process exited, port 8765 closed |
| Restart | New backend instance ready; Interactive; aggregate status healthy |
| Trace | Writer alive, queue empty, zero dropped events and write errors before stop and after restart |
| Health routing | Direct route; OpenRouter authentication HTTP 200; Hermes API and service explicitly not required |
| Retention | Deployed CLI and daily report include backend trace root; 287 files / 443,192,696 bytes due; no deletion |
| Robot | Runner remained stopped throughout; no physical speaker playback or robot run |

Text-only output may retain Markdown emphasis; existing backend speech normalization
handles the audio path. The short MVC answer supplied navigation labels rather than
a long spoken URL. These checks validate deployment, not a new physical listening test.

Private acceptance evidence, configuration hashes, source hashes, package inventory,
WAVs, and release manifest are retained on m1max under:
`/Users/leon/.local/state/reachy-reception/promotions/20260905-37c7042`.
The original rollback snapshot remains at `20260904-d55159e` in the same parent.
Restore app `7840866` and its saved configuration together for the previous Hermes
production route. The staging listener and Hermes service were left intact.
