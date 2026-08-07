# Legacy Cleanup Plan

Status: inventory revalidated 2026-08-06; waiting for an explicit delete/quarantine decision.

This plan covers the old reception-daemon stack after the accepted pivot to
`reachy_mini_brain.official_runtime` plus the m1max local S2S backend. It is intentionally
non-destructive: no files are deleted or moved by this document.

## Current Canonical Path

Keep these as product path:

- `src/reachy_mini_brain/official_runtime/`
- `src/reachy_mini_brain/audio_pacing.py`
- `src/reachy_mini_brain/audio.py`
- `src/reachy_mini_brain/robot.py`
- `src/reachy_mini_brain/motion.py`
- `src/reachy_mini_brain/vision.py`
- `src/reachy_mini_brain/video.py`
- `src/reachy_mini_brain/state.py`
- `scripts/m1max/live_ops.sh` (compatibility wrapper only)
- `scripts/m1max/run_official_runtime_live.sh`
- `scripts/m1max/run_s2s_backend.sh`
- `profiles/clinic_receptionist/`
- `tests/test_official_runtime.py`
- `tests/test_audio_pacing.py`

## Legacy Files

These are old-daemon or old-harness modules. They now have module-level legacy/fallback status
notes and should not be used as the default live path.

| File | Current role | Proposed disposition after approval |
| --- | --- | --- |
| `src/reachy_mini_brain/reception.py` | Old resident daemon and socket control plane | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/session.py` | Old persistent SDK session and Unix-socket server | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/perception.py` | Old daemon person/wave event pipeline | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/detector.py` | Old RF-DETR wrapper used by old perception | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/approach.py` | Old approach/depart state machine | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/gesture.py` | Old MediaPipe gesture wrapper | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/alert_engine.py` | Old separate event-to-action process | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/brain.py` | Old `claude -p` / Pydantic receptionist brain | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/replay.py` | Old video replay harness for daemon perception | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/review_audio.py` | Old daemon-run audio review tool | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/transcribe.py` | Older meeting transcription trigger process | Delete after tagging, or move to `legacy/` |
| `src/reachy_mini_brain/stt_worker.py` | Old daemon STT worker | Delete after tagging, or move to `legacy/` |
| `tests/test_reception_manifest.py` | Tests old daemon manifest/audio-record behavior | Delete or move with old daemon |

## Conditional Keepers

Do not delete these as part of the first legacy removal unless the manual audio CLI decision is made
at the same time:

| File | Why it still matters | Decision needed |
| --- | --- | --- |
| `src/reachy_mini_brain/stt.py` | Imported by `audio listen` and old `session.py` | Keep if manual `audio listen` remains |
| `src/reachy_mini_brain/tts.py` | Imported by `audio speak` and old `session.py`; used by `tests/test_e2e_audio.py` | Keep if manual `audio speak` remains |
| `src/reachy_mini_brain/audio.py` | Current manual speaker/playback debugging plus helper used by official runtime | Keep |

## Entry Points

Remove only when the corresponding legacy modules are removed:

- `reception = "reachy_mini_brain.reception:cli"`
- `review-audio = "reachy_mini_brain.review_audio:cli"`

Keep:

- `official-runtime-live`
- `backend-benchmark`
- `livekit-replay`
- `livekit-agent`
- `reception-vision-replay`

## Dependency Notes

- Keep `vision` and `gesture` optional extras. They are used by official-runtime perception as well as
  the old daemon.
- `brain` optional extra appears legacy-only today. Remove it only after deleting `brain.py` and any
  legacy tests that import it.
- `audio` optional extra still supports current manual audio debugging and should remain for now.

## Recommended Deletion Strategy

Preferred next destructive step, if approved:

1. Create a tag at the last commit containing the runnable legacy daemon, for example
   `legacy-daemon-last`.
2. Delete the old-daemon modules listed in **Legacy Files**.
3. Delete or move `tests/test_reception_manifest.py`.
4. Remove `reception` and `review-audio` console scripts from `pyproject.toml`.
5. Keep `stt.py`, `tts.py`, and `audio.py` until the manual audio CLI is explicitly retired or
   replaced.
6. Run:
   - `.venv/bin/python -m pytest tests/test_official_runtime.py tests/test_audio_pacing.py -v`
   - `.venv/bin/python -m py_compile src/reachy_mini_brain/official_runtime/live_app.py`
   - `.venv/bin/python -m reachy_mini_brain.official_runtime.live_app --help`

Alternative: move legacy modules under a `legacy/` folder. This preserves source files in the tree but
adds import/path churn, so it is not the preferred path unless we expect to run old daemon code often.

## Additional Stale Test And Experiment Candidates

These are not included in the deletion list above until their replacement/disposition is reviewed:

- `tests/test_e2e.py`, `tests/test_e2e_audio.py`, `tests/test_e2e_vision.py`, and
  `tests/test_integration.py` hardcode `/Users/lliao/work/reachy_mini`; the integration test also
  expects robot SDK `1.5.0`. They are not current production acceptance tests.
- `tests/test_antenna_manual.py` is a useful manual diagnostic but belongs under a manual scripts
  surface rather than automatic pytest discovery.
- `experiments/brain_bench/*`, `experiments/stream_speak_test.py`, and
  `experiments/vad_endpoint_test.py` import legacy `brain.py` or `session.py`; archive or delete them
  with the legacy stack after explicit approval.
- `experiments/agentic_api/` is historical backend research. Its documentation is archived, but the
  executable experiment directory remains a separate archive-or-delete decision.

## Current State

As of 2026-08-06:

- Legacy modules are marked in docstrings.
- OPS library/CLI and the clean release are the canonical product path; `live_ops.sh` is
  compatibility-only.
- Current production and diagnosis code has no import dependency on the listed legacy daemon group;
  the group remains internally connected and is referenced by the stale tests/experiments above.
- No deletion or file move has been performed.
- Destructive cleanup remains blocked on an explicit file-list/disposition confirmation.
