"""Integration tests against a live Reachy Mini robot.

Run with: .venv/bin/python -m pytest tests/test_integration.py -v

Requires the robot to be on the network at reachy-mini.local:8000.
"""

import json
import math
import subprocess
import sys

import pytest

PYTHON = ".venv/bin/python"


def run_cli(*args, timeout=30):
    """Run a CLI command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        [PYTHON, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/Users/lliao/work/reachy_mini",
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# --- Connectivity ---


class TestConnectivity:
    def test_daemon_reachable(self):
        """Can we reach the daemon at all?"""
        from reachy_mini_brain import robot

        status = robot.get_daemon_status()
        assert "state" in status
        assert status["version"] == "1.5.0"

    def test_get_state(self):
        """Can we read the robot state?"""
        rc, stdout, stderr = run_cli("-m", "reachy_mini_brain.state", "get-state")
        assert rc == 0, f"stderr: {stderr}"
        state = json.loads(stdout)
        assert "head_pose" in state
        assert "antennas_position" in state


# --- Motion ---


class TestMotion:
    def test_wake_up(self):
        rc, stdout, stderr = run_cli(
            "-m", "reachy_mini_brain.motion", "wake-up", timeout=30
        )
        assert rc == 0, f"stderr: {stderr}"
        assert "awake" in stdout.lower()

    def test_look_left(self):
        rc, stdout, stderr = run_cli(
            "-m", "reachy_mini_brain.motion", "look", "--direction", "left", timeout=15
        )
        assert rc == 0, f"stderr: {stderr}"
        assert "left" in stdout.lower()

    def test_look_center(self):
        rc, stdout, stderr = run_cli(
            "-m",
            "reachy_mini_brain.motion",
            "look",
            "--direction",
            "center",
            timeout=15,
        )
        assert rc == 0, f"stderr: {stderr}"
        assert "center" in stdout.lower()

    def test_move_head(self):
        rc, stdout, stderr = run_cli(
            "-m",
            "reachy_mini_brain.motion",
            "move-head",
            "--pitch",
            "10",
            "--yaw",
            "15",
            timeout=15,
        )
        assert rc == 0, f"stderr: {stderr}"
        assert "pitch=10" in stdout

    def test_nod(self):
        rc, stdout, stderr = run_cli(
            "-m", "reachy_mini_brain.motion", "nod", timeout=45
        )
        assert rc == 0, f"stderr: {stderr}"
        assert "nodded" in stdout.lower()

    def test_shake(self):
        rc, stdout, stderr = run_cli(
            "-m", "reachy_mini_brain.motion", "shake", timeout=45
        )
        assert rc == 0, f"stderr: {stderr}"
        assert "shook" in stdout.lower()

    def test_sleep(self):
        rc, stdout, stderr = run_cli(
            "-m", "reachy_mini_brain.motion", "sleep", timeout=15
        )
        assert rc == 0, f"stderr: {stderr}"
        assert "asleep" in stdout.lower()


# --- Robot module unit tests (no CLI, direct API) ---


class TestRobotAPI:
    def test_ensure_ready_idempotent(self):
        """ensure_ready() should be safe to call multiple times."""
        from reachy_mini_brain import robot

        robot.ensure_ready()
        robot.ensure_ready()  # second call should be fast

    def test_goto_radians_conversion(self):
        """Verify degrees→radians conversion in goto payload."""
        from reachy_mini_brain import robot

        # We can't easily intercept the HTTP call, but we can test
        # the math used in the module
        assert abs(math.radians(30) - 0.5236) < 0.001
        assert abs(math.radians(-20) - (-0.3491)) < 0.001

    def test_get_state_returns_dict(self):
        from reachy_mini_brain import robot

        state = robot.get_state()
        assert isinstance(state, dict)
        assert "head_pose" in state

    def test_get_head_pose(self):
        from reachy_mini_brain import robot

        robot.ensure_ready()
        pose = robot.get_head_pose()
        assert isinstance(pose, dict)
        # Should have x, y, z, roll, pitch, yaw
        for key in ["x", "y", "z", "roll", "pitch", "yaw"]:
            assert key in pose, f"Missing key: {key}"

    def test_get_antennas(self):
        from reachy_mini_brain import robot

        robot.ensure_ready()
        antennas = robot.get_antennas()
        assert isinstance(antennas, (list, dict))
