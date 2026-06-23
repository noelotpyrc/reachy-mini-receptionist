"""End-to-end vision tests for Reachy Mini.

Run with: .venv/bin/python -m pytest tests/test_e2e_vision.py -v -s

Tests:
  1. take-photo — camera captures a frame and saves JPEG
  2. Photo is a valid image with reasonable dimensions
  3. Multiple captures return different frames (not frozen)
  4. Look + capture — move head, then verify photo perspective changes
  5. Photo path override — custom output path works
"""

import os
import subprocess
import time

import pytest

PYTHON = ".venv/bin/python"
CWD = "/Users/lliao/work/reachy_mini"
PHOTO_DIR = os.path.join(CWD, "artifacts", "vision_test")


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
    """Ask the human observer to confirm behavior."""
    print(f"\n  👀 EXPECTED: {description}")
    response = input("  ✅ Press Enter if correct, or type 'f' to fail: ").strip().lower()
    if response == "f":
        pytest.fail(f"Human observer rejected: {description}")


def take_photo(path, timeout=45):
    """Helper: capture a photo and return (rc, stdout, stderr, file_size)."""
    rc, stdout, stderr = run_cli(
        "-m", "reachy_mini_brain.vision", "take-photo",
        "--out", path,
        timeout=timeout,
    )
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return rc, stdout, stderr, size


# --- Setup / Teardown ---


@pytest.fixture(scope="module", autouse=True)
def setup():
    """Create temp dir, wake robot, warm up camera, clean up after."""
    os.makedirs(PHOTO_DIR, exist_ok=True)

    from reachy_mini_brain import robot
    print("\n\n📷 Starting vision e2e tests — waking up robot...")
    robot.wake_up()
    robot.goto(pitch=0, yaw=0, roll=0, duration=1.0)

    # Warm up the WebRTC camera pipeline (first connection is slow:
    # GStreamer plugin scan + WebRTC signalling + pipeline setup)
    print("📷 Warming up camera (first WebRTC connection, may take ~30-60s)...")
    warmup_path = f"{PHOTO_DIR}/warmup.jpg"
    rc, stdout, stderr = run_cli(
        "-m", "reachy_mini_brain.vision", "take-photo",
        "--out", warmup_path,
        timeout=120,
    )
    if rc == 0:
        print(f"  ✓ Camera ready")
    else:
        print(f"  ⚠ Camera warmup failed (rc={rc}): {stderr}")
        print("    Tests may still work if the pipeline started in background")

    yield

    print("\n📷 Vision tests done — putting robot to sleep...")
    robot.go_to_sleep()

    # Clean up test photos
    for f in os.listdir(PHOTO_DIR):
        os.remove(os.path.join(PHOTO_DIR, f))
    os.rmdir(PHOTO_DIR)


# --- Tests ---


class TestVisionE2E:

    def test_take_photo_basic(self):
        """Camera captures a frame and saves a JPEG."""
        print("\n--- Test: Basic Photo Capture ---")
        print("  ACTION: Capturing a camera frame...")
        path = f"{PHOTO_DIR}/basic.jpg"
        rc, stdout, stderr, size = take_photo(path)
        print(f"  stdout: {stdout}")
        if stderr:
            print(f"  stderr: {stderr}")
        assert rc == 0, f"take-photo failed (rc={rc}): {stderr}"
        assert os.path.exists(path), "Photo file not created"
        assert size > 1000, f"Photo too small ({size} bytes), likely corrupt"
        print(f"  Photo saved: {path} ({size:,} bytes)")
        confirm(f"Photo saved at {path}. Open it — does it show the robot's camera view?")

    def test_photo_is_valid_image(self):
        """Saved file is a valid JPEG with reasonable dimensions."""
        print("\n--- Test: Valid Image ---")
        path = f"{PHOTO_DIR}/validate.jpg"
        rc, _, stderr, size = take_photo(path)
        assert rc == 0, f"take-photo failed: {stderr}"

        import cv2
        img = cv2.imread(path)
        assert img is not None, "cv2 could not read the saved image"
        h, w = img.shape[:2]
        print(f"  Image: {w}x{h}, {size:,} bytes")
        assert w >= 320, f"Image too narrow: {w}px"
        assert h >= 240, f"Image too short: {h}px"
        print(f"  ✓ Valid JPEG, {w}x{h}")

    def test_two_captures_not_frozen(self):
        """Two captures taken a few seconds apart are not identical."""
        print("\n--- Test: Camera Not Frozen ---")
        print("  ACTION: Taking two photos 3 seconds apart...")
        path1 = f"{PHOTO_DIR}/frame1.jpg"
        path2 = f"{PHOTO_DIR}/frame2.jpg"

        rc1, _, stderr1, _ = take_photo(path1)
        assert rc1 == 0, f"First capture failed: {stderr1}"

        time.sleep(3)

        rc2, _, stderr2, _ = take_photo(path2)
        assert rc2 == 0, f"Second capture failed: {stderr2}"

        # Compare file contents — they should differ (different frames)
        with open(path1, "rb") as f1, open(path2, "rb") as f2:
            data1, data2 = f1.read(), f2.read()

        if data1 == data2:
            print("  ⚠ Both frames are byte-identical — camera may be frozen")
            confirm("Two photos were taken 3s apart. Open both — are they different?")
        else:
            print(f"  ✓ Frames differ ({len(data1):,} vs {len(data2):,} bytes)")

    def test_look_then_capture(self):
        """Moving the head changes the camera perspective."""
        print("\n--- Test: Look + Capture ---")
        from reachy_mini_brain import robot

        # Look left and capture
        print("  ACTION: Looking LEFT, then capturing...")
        robot.goto(yaw=30, duration=0.8)
        time.sleep(0.5)
        path_left = f"{PHOTO_DIR}/look_left.jpg"
        rc, _, stderr, _ = take_photo(path_left)
        assert rc == 0, f"Left capture failed: {stderr}"

        # Look right and capture
        print("  ACTION: Looking RIGHT, then capturing...")
        robot.goto(yaw=-30, duration=0.8)
        time.sleep(0.5)
        path_right = f"{PHOTO_DIR}/look_right.jpg"
        rc, _, stderr, _ = take_photo(path_right)
        assert rc == 0, f"Right capture failed: {stderr}"

        # Reset
        robot.goto(yaw=0, duration=0.8)

        print(f"  Left photo:  {path_left}")
        print(f"  Right photo: {path_right}")
        confirm("Open both photos — do they show different perspectives (left vs right)?")

    def test_custom_output_path(self):
        """--out flag saves to the specified path."""
        print("\n--- Test: Custom Output Path ---")
        custom_path = "/tmp/reachy_custom_test_photo.jpg"
        if os.path.exists(custom_path):
            os.remove(custom_path)

        rc, stdout, stderr, size = take_photo(custom_path)
        assert rc == 0, f"take-photo failed: {stderr}"
        assert custom_path in stdout, f"Output didn't print path: {stdout}"
        assert os.path.exists(custom_path), "File not at custom path"
        assert size > 1000, f"Photo too small ({size} bytes)"
        print(f"  ✓ Photo saved to custom path: {custom_path} ({size:,} bytes)")

        # Clean up
        os.remove(custom_path)
