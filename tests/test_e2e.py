"""End-to-end tests for Reachy Mini — requires human observation.

Run with: .venv/bin/python -m pytest tests/test_e2e.py -v -s
  -s is REQUIRED so you can see prompts and confirm behavior.

Each test tells you what to expect, does the action, then asks you
to confirm what you saw. Press Enter to pass, type 'f' to fail.
"""

import json
import subprocess
import time

import pytest

from reachy_mini_brain import robot

PYTHON = ".venv/bin/python"
CWD = "/Users/lliao/work/reachy_mini"


def run_cli(*args, timeout=60):
    result = subprocess.run(
        [PYTHON, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=CWD,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def confirm(description: str):
    """Ask the human observer to confirm the robot's behavior."""
    print(f"\n  👀 EXPECTED: {description}")
    response = input("  ✅ Press Enter if correct, or type 'f' to fail: ").strip().lower()
    if response == "f":
        pytest.fail(f"Human observer rejected: {description}")


# --- Setup / Teardown ---


@pytest.fixture(scope="module", autouse=True)
def robot_lifecycle():
    """Wake up at start, sleep at end."""
    print("\n\n🤖 Starting e2e tests — waking up robot...")
    robot.wake_up()
    # Reset to center
    robot.goto(pitch=0, yaw=0, roll=0, duration=1.0)
    confirm("Robot woke up and head is centered")
    yield
    print("\n🤖 Tests done — putting robot to sleep...")
    robot.go_to_sleep()


# --- Tests ---


class TestMotionE2E:

    def test_look_left(self):
        print("\n--- Test: Look Left ---")
        print("  ACTION: Robot will turn head to the LEFT")
        run_cli("-m", "reachy_mini_brain.motion", "look", "--direction", "left")
        confirm("Robot is looking to its LEFT")

    def test_look_right(self):
        print("\n--- Test: Look Right ---")
        print("  ACTION: Robot will turn head to the RIGHT")
        run_cli("-m", "reachy_mini_brain.motion", "look", "--direction", "right")
        confirm("Robot is looking to its RIGHT")

    def test_look_up(self):
        print("\n--- Test: Look Up ---")
        print("  ACTION: Robot will tilt head UP")
        run_cli("-m", "reachy_mini_brain.motion", "look", "--direction", "up")
        confirm("Robot is looking UP")

    def test_look_down(self):
        print("\n--- Test: Look Down ---")
        print("  ACTION: Robot will tilt head DOWN")
        run_cli("-m", "reachy_mini_brain.motion", "look", "--direction", "down")
        confirm("Robot is looking DOWN")

    def test_look_center(self):
        print("\n--- Test: Look Center ---")
        print("  ACTION: Robot will return head to CENTER")
        run_cli("-m", "reachy_mini_brain.motion", "look", "--direction", "center")
        confirm("Robot head is centered (facing forward)")

    def test_nod(self):
        print("\n--- Test: Nod ---")
        print("  ACTION: Robot will NOD its head (yes gesture)")
        run_cli("-m", "reachy_mini_brain.motion", "nod")
        confirm("Robot nodded (moved head up and down)")

    def test_shake(self):
        print("\n--- Test: Shake ---")
        print("  ACTION: Robot will SHAKE its head (no gesture)")
        run_cli("-m", "reachy_mini_brain.motion", "shake")
        confirm("Robot shook head (moved head side to side)")

    def test_move_head_custom(self):
        print("\n--- Test: Custom Head Pose ---")
        print("  ACTION: Robot will pitch down 15° and yaw left 20°")
        run_cli(
            "-m", "reachy_mini_brain.motion", "move-head",
            "--pitch", "15", "--yaw", "20", "--duration", "1.5",
        )
        confirm("Robot is looking slightly down and to its left")
        # Reset
        run_cli("-m", "reachy_mini_brain.motion", "look", "--direction", "center")

    def test_antennas(self):
        print("\n--- Test: Antennas ---")
        print("  ACTION: Left antenna up 30°, right antenna down -30°")
        print("  CLI: antennas --left 30 --right -30  (positive=up)")
        run_cli(
            "-m", "reachy_mini_brain.motion", "antennas",
            "--left", "30", "--right", "-30",
        )
        confirm("Left antenna is UP, right antenna is DOWN")
        # Reset
        run_cli("-m", "reachy_mini_brain.motion", "antennas", "--left", "0", "--right", "0")


class TestStateE2E:

    def test_get_state(self):
        print("\n--- Test: Get State ---")
        print("  ACTION: Reading robot state (no movement)")
        rc, stdout, stderr = run_cli("-m", "reachy_mini_brain.state", "get-state")
        assert rc == 0, f"get-state failed: {stderr}"
        state = json.loads(stdout)
        print(f"  STATE: {json.dumps(state, indent=2)}")
        confirm("State JSON printed above looks reasonable (has head_pose, antennas, etc.)")


class TestLifecycleE2E:

    def test_sleep_and_wake(self):
        print("\n--- Test: Sleep then Wake ---")
        print("  ACTION: Robot will go to sleep (head drops, motors off)")
        run_cli("-m", "reachy_mini_brain.motion", "sleep")
        confirm("Robot is asleep (head lowered, limp)")

        print("  ACTION: Robot will wake up (head rises, alert)")
        run_cli("-m", "reachy_mini_brain.motion", "wake-up", timeout=45)
        confirm("Robot woke up (head up, looking forward)")
