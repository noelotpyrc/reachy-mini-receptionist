# Legacy Cleanup Plan

Status: approved removal completed 2026-08-06. Recovery tag: `legacy-daemon-last`.

This document records removal of the old reception-daemon stack after the accepted pivot to
`reachy_mini_brain.official_runtime` plus the m1max local S2S backend.

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

These old-daemon or old-harness modules were removed after explicit approval. Their final source is
available from Git tag `legacy-daemon-last`.

| File | Former role | Disposition |
| --- | --- | --- |
| `src/reachy_mini_brain/reception.py` | Old resident daemon and socket control plane | Deleted |
| `src/reachy_mini_brain/session.py` | Old persistent SDK session and Unix-socket server | Deleted |
| `src/reachy_mini_brain/perception.py` | Old daemon person/wave event pipeline | Deleted |
| `src/reachy_mini_brain/detector.py` | Old RF-DETR wrapper used by old perception | Deleted |
| `src/reachy_mini_brain/approach.py` | Old approach/depart state machine | Deleted |
| `src/reachy_mini_brain/gesture.py` | Old MediaPipe gesture wrapper | Deleted |
| `src/reachy_mini_brain/alert_engine.py` | Old separate event-to-action process | Deleted |
| `src/reachy_mini_brain/brain.py` | Old `claude -p` / Pydantic receptionist brain | Deleted |
| `src/reachy_mini_brain/replay.py` | Old video replay harness for daemon perception | Deleted |
| `src/reachy_mini_brain/review_audio.py` | Old daemon-run audio review tool | Deleted |
| `src/reachy_mini_brain/transcribe.py` | Older meeting transcription trigger process | Deleted |
| `src/reachy_mini_brain/stt_worker.py` | Old daemon STT worker | Deleted |
| `tests/test_reception_manifest.py` | Tested old daemon manifest/audio-record behavior | Deleted |

## Conditional Keepers

Do not delete these as part of the first legacy removal unless the manual audio CLI decision is made
at the same time:

| File | Why it still matters | Decision needed |
| --- | --- | --- |
| `src/reachy_mini_brain/stt.py` | Imported by `audio listen` and old `session.py` | Keep if manual `audio listen` remains |
| `src/reachy_mini_brain/tts.py` | Imported by `audio speak` and old `session.py`; used by `tests/test_e2e_audio.py` | Keep if manual `audio speak` remains |
| `src/reachy_mini_brain/audio.py` | Current manual speaker/playback debugging plus helper used by official runtime | Keep |

## Entry Points

The `reception` and `review-audio` console scripts were removed with their modules.

Keep:

- `official-runtime-live`
- `backend-benchmark`
- `livekit-replay`
- `livekit-agent`
- `reception-vision-replay`

## Dependency Notes

- Keep `vision` and `gesture` optional extras. They are used by official-runtime perception.
- The legacy-only `brain` optional extra was removed with `brain.py`.
- `audio` optional extra still supports current manual audio debugging and should remain for now.

## Completed Removal

The approved sequence was:

1. Tag the last commit containing the runnable legacy daemon as `legacy-daemon-last`.
2. Delete the modules and test listed above.
3. Remove their console scripts and the legacy-only dependency extra.
4. Keep `stt.py`, `tts.py`, and `audio.py` until the manual audio CLI is explicitly retired or
   replaced.
5. Run:
   - `.venv/bin/python -m pytest tests/test_official_runtime.py tests/test_audio_pacing.py -v`
   - `.venv/bin/python -m py_compile src/reachy_mini_brain/official_runtime/live_app.py`
   - `.venv/bin/python -m reachy_mini_brain.official_runtime.live_app --help`

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

- The approved legacy modules, test, entrypoints, and dependency extra are removed.
- OPS library/CLI and the clean release are the canonical product path; `live_ops.sh` is
  compatibility-only.
- Current production and diagnosis code has no import dependency on the removed group.
- Deferred historical experiments still reference `brain.py` or `session.py`; they are not part of
  the package runtime or automatic test suite and remain a separate disposition decision.
- The removed implementation is recoverable from `legacy-daemon-last`.
