"""Manual antenna diagnostic — run and observe the robot.

Determines the mapping between API indices and physical antennas,
and how positive/negative angles map to up/down.

Usage: .venv/bin/python tests/test_antenna_manual.py
"""

import math
import os
import time

if __name__ != "__main__" and os.environ.get("REACHY_MANUAL_TESTS") != "1":
    import pytest

    pytest.skip("manual robot antenna diagnostic; run as a script", allow_module_level=True)

from reachy_mini_brain import robot


def show_antennas():
    pos = robot.get_antennas()
    print(f"  Raw radians: [{pos[0]:.3f}, {pos[1]:.3f}]")
    print(f"  Degrees:     [{math.degrees(pos[0]):.1f}°, {math.degrees(pos[1]):.1f}°]")
    return pos


print("=== Antenna Calibration ===\n")

print("Waking up robot...")
robot.wake_up()

print("\n--- Current antenna state ---")
show_antennas()

# --- Step 1: Reset to neutral ---
print("\n--- Step 1: Reset both to [0, 0] ---")
robot.set_target(antennas=(0, 0))
time.sleep(1.5)
show_antennas()
input(">> Both antennas at neutral? [Enter to continue] ")

# --- Step 2: Identify which index is which physical antenna ---
print("\n--- Step 2: First value = 30, second = 0 ---")
print("  Sending set_target(antennas=(30, 0))")
print("  API receives: target_antennas = [rad(30), rad(0)]")
robot.set_target(antennas=(30, 0))
time.sleep(1.5)
show_antennas()
ans2 = input(">> Which antenna moved — LEFT or RIGHT? [type and Enter] ").strip().lower()

# --- Step 3: Other antenna ---
print("\n--- Step 3: First value = 0, second = 30 ---")
print("  Sending set_target(antennas=(0, 30))")
print("  API receives: target_antennas = [rad(0), rad(30)]")
robot.set_target(antennas=(0, 30))
time.sleep(1.5)
show_antennas()
ans3 = input(">> Which antenna moved — LEFT or RIGHT? [type and Enter] ").strip().lower()

# Reset before direction tests
robot.set_target(antennas=(0, 0))
time.sleep(1)

# --- Step 4: Determine which direction is "up" for index 0 ---
print("\n--- Step 4: First value = +30, second = 0 ---")
print("  Sending set_target(antennas=(30, 0))")
robot.set_target(antennas=(30, 0))
time.sleep(1.5)
show_antennas()
ans4 = input(">> Did the antenna go UP or DOWN? [type and Enter] ").strip().lower()

# --- Step 5: Negative on index 0 ---
print("\n--- Step 5: First value = -30, second = 0 ---")
print("  Sending set_target(antennas=(-30, 0))")
robot.set_target(antennas=(-30, 0))
time.sleep(1.5)
show_antennas()
ans5 = input(">> Did the antenna go UP or DOWN? [type and Enter] ").strip().lower()

# Reset
robot.set_target(antennas=(0, 0))
time.sleep(1)

# --- Step 6: Same-sign test (should give one up, one down) ---
print("\n--- Step 6: SAME sign — both = +30 ---")
print("  Sending set_target(antennas=(30, 30))")
print("  (Mirror-mount hypothesis: same sign → one up, one down)")
robot.set_target(antennas=(30, 30))
time.sleep(1.5)
show_antennas()
input(">> Are they in OPPOSITE positions (one up, one down)? [Enter to continue] ")

# --- Step 7: Opposite-sign test (should give both same direction) ---
print("\n--- Step 7: OPPOSITE sign — +30 and -30 ---")
print("  Sending set_target(antennas=(30, -30))")
print("  (Mirror-mount hypothesis: opposite sign → both same direction)")
robot.set_target(antennas=(30, -30))
time.sleep(1.5)
show_antennas()
input(">> Are they in the SAME position (both up or both down)? [Enter to continue] ")

# --- Summary ---
print("\n=== SUMMARY ===")
print(f"  Index 0 (first value)  → {ans2.upper()} antenna")
print(f"  Index 1 (second value) → {ans3.upper()} antenna")
print(f"  Positive on index 0    → antenna goes {ans4.upper()}")
print(f"  Negative on index 0    → antenna goes {ans5.upper()}")
print(f"  Same sign   (30, 30)   → opposite positions (mirror-mount)")
print(f"  Opposite sign (30,-30) → same direction (mirror-mount)")
print()
print("  Use these results to set the correct mapping in robot.py")

# Cleanup
print("\nResetting and sleeping...")
robot.set_target(antennas=(0, 0))
time.sleep(1)
robot.go_to_sleep()
print("Done.")
