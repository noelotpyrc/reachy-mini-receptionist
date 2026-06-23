# CLAUDE.md

- Python venv: `.venv/bin/python` (managed by `uv`)
- Install: `uv pip install -e .` (add `".[audio]"` for audio deps)
- Run CLI: `.venv/bin/python -m reachy_mini_brain.<module> <command>`
- Tests: `.venv/bin/python -m pytest tests/ -v` (e2e tests need `-s`)
- **Robot/hardware commands:** Claude may run them (tests, audio, vision, motion — typically over ssh on the brain computer `m1max`), but **must confirm with the user before executing anything that talks to the robot**. For steps that can't be done programmatically (speaking near the robot, power button, physical repositioning/inspection), ask the user to do them.
- **Live-test feedback is diagnostic input, not permission to patch.** When the user reports physical UX/audio/video/robot behavior from live testing, discuss evidence and options first. Do not implement behavior changes unless the user explicitly asks to fix, implement, change, or patch it.
- **No guessing physical root causes.** For physical/audio/video issues, do not present inferred root causes as fact. Any proposed root cause must use this diagnosis contract: `claim -> evidence (run_id, artifact path, timestamp range) -> confidence -> test to confirm/refute`. If the supporting artifact does not exist, the only valid next step is `instrument/reproduce first`, not a code fix.
- **Confirm before irreversible or unreplayable actions.** Ask before deletions, log/artifact overwrites, destructive git operations, and live tests that require a human physically present to validate the result.
- Full CLI reference: `docs/robot-guide.md`
