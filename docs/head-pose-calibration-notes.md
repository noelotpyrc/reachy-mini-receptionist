# Head pose & calibration notes

Reference for future calibration / pose work on Reachy Mini. Findings from 2026-06-09, after a robot
recalibration via the Reachy app.

## Official head API (SDK `reachy_mini` 1.8.0) — use the 4×4 MATRIX, not euler
The head is a 6-DOF Stewart platform; its canonical pose is a **4×4 homogeneous transform matrix**.
- **Read:** `ReachyMini.get_current_head_pose() -> np.ndarray  # 4×4`
- **Set:** `set_target_head_pose(pose_4x4)` · `goto_target(head=pose_4x4, antennas=, duration=, body_yaw=)`
- **Aim:** `look_at_world(x, y, z)` (meters) · `look_at_image(u, v)` (pixels)

**Caveat — our code uses euler:** `robot.py` drives the head via the daemon's **euler REST** API
(`/api/move/goto` with `head_pose:{x,y,z,roll,pitch,yaw}`; `/api/state/present_head_pose`). It moves the
robot fine, but **euler is a lossy, cross-coupled decomposition** of the 4×4 pose — reading/commanding
single euler angles fabricates apparent axis-coupling and sub-unity "gain". **For any pose analysis use
the matrix API** via a standalone SDK script (the daemon owns the session, so stop it first).

## Findings (2026-06-09)
- **Recalibration (Reachy app) leveled the head:** baseline head roll went **7.5° → ~0°**. `reset` was
  always correct (it commands 0,0,0); the tilt was the robot's *zero*, fixed by recalibration — not a
  bug in our reset.
- **`reset` semantics:** commands **true 0** on every axis (head roll/pitch/yaw=0, body_yaw=0,
  antennas=0). It drives to a **fixed home**, NOT "back to the previous pose."
- **Body yaw:** resets **exactly** — ±0.2° from any approach direction. The clean, repeatable joint.
- **Head (measured in matrix space, geodesic angle):**
  - command identity → actual sits **~3° off identity** (small, consistent home offset).
  - **return-to-home is NOT exactly repeatable:** returning to the same identity command from 4
    perturbation directions landed **0.8°–7.6° apart** in orientation (translation tight, ≤1.8 mm).
  - a commanded **20°** rotation *reached* **13.9–21.3°** (±~6°).
  - ⇒ head orientation precision/repeatability ≈ **±6–7°**; translation is solid (sub-2 mm).
- **Open question — settling vs. hysteresis:** not yet separated (one run; 3/4 directions ~7°, one
  0.8°). To pin down: read at +1.6 s **and** again at +3 s (does it converge?), and repeat each
  direction 3× (is ~7° consistent per direction?).

## Guidance
- **At startup / the beginning of an interaction, move antennas only** — avoid head/body moves there.
  The head is non-repeatable (±~7°) and the camera rides on it, so head moves disturb detection framing.
  *(Current code already aligns: `_express` (greet/farewell/opener) is antenna-only; the only head/body
  mover is the manual `reset`; the daemon does not auto-move the head at startup.)*
- For pose measurement/analysis, always use the **4×4 matrix API**, never euler readbacks.

## Method note
First over-asserted "hysteresis / ~0.84 gain" from **euler** readbacks, then over-corrected to "it's all
euler artifact." The **matrix-space** test is the arbiter: a real ~7° head return-spread *does* exist,
but euler garbled the per-axis specifics. Lesson: measure pose in the canonical representation (matrix)
before concluding anything about mechanics.
