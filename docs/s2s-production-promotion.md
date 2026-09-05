# Client-Owned Agent Production Promotion

Status: implementation and local validation complete; managed m1max acceptance pending.

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
