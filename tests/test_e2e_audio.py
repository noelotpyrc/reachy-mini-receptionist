"""End-to-end audio/video tests for Reachy Mini.

Run with: .venv/bin/python -m pytest tests/test_e2e_audio.py -v -s

These tests require:
  - Robot powered on and connected over WiFi
  - Audio deps installed: uv pip install -e ".[audio]"
  - A human observer to confirm audio behavior
  - The -s flag (interactive confirm prompts)

Tests:
  1. listen — records from robot mic, runs STT, prints transcript
  2. speak — synthesizes text and plays through robot speaker
  3. play-sound — plays a TTS-generated WAV through robot speaker
  4. video record — records a short video clip

Note: DoA test removed — ReSpeaker USB is on the RPi, not accessible over WiFi/WebRTC.
"""

import json
import os
import shutil
import subprocess
import time

import pytest

PYTHON = ".venv/bin/python"
CWD = "/Users/lliao/work/reachy_mini"
ARTIFACTS = os.path.join(CWD, "artifacts", "audio_test")


def run_cli(*args, timeout=180):
    """Run a CLI command. Generous timeout for WebRTC warmup (~30-60s)."""
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


@pytest.fixture(scope="module", autouse=True)
def setup():
    """Wake robot and create test artifacts dir."""
    os.makedirs(ARTIFACTS, exist_ok=True)

    print("\n🎤 Starting audio/video e2e tests — waking up robot...")
    rc, out, err = run_cli("-m", "reachy_mini_brain.motion", "wake-up", timeout=60)
    assert rc == 0, f"wake-up failed: {err}"

    yield

    # Cleanup
    print("\n🧹 Cleaning up...")
    run_cli("-m", "reachy_mini_brain.motion", "sleep", timeout=30)
    if os.path.exists(ARTIFACTS):
        shutil.rmtree(ARTIFACTS)


# ──────────────────────────────────────────────
# Test 1: Listen — mic recording + STT
# ──────────────────────────────────────────────

class TestListen:
    def test_listen_captures_audio(self):
        """Record from robot mic for 5 seconds, save WAV, and transcribe."""
        print("\n--- Test: Listen (5 seconds) ---")
        wav_path = os.path.join(ARTIFACTS, "listen_test.wav")

        print("ACTION: Speak clearly near the robot for ~5 seconds.")
        print("  (WebRTC warmup may take 30-60 seconds before recording starts)")
        input("  🎙️ Press Enter when ready to start...")

        rc, stdout, stderr = run_cli(
            "-m", "reachy_mini_brain.audio", "listen",
            "--duration", "5",
            "--save-wav", wav_path,
        )
        print(f"  Exit code: {rc}")
        print(f"  Transcript: '{stdout}'")
        # Show just the tail of stderr (skip GStreamer noise)
        stderr_lines = stderr.split("\n")
        print(f"  Stderr (tail): {chr(10).join(stderr_lines[-5:])}")

        assert rc == 0, f"listen failed: {stderr}"

        if os.path.exists(wav_path):
            size = os.path.getsize(wav_path)
            print(f"  WAV saved: {size:,} bytes")
            assert size > 1000, f"WAV too small ({size} bytes)"

        if stdout:
            confirm(f"Transcript '{stdout}' roughly matches what you said")
        else:
            confirm("Transcript was empty — was the room silent? (Enter=ok, f=fail)")


# ──────────────────────────────────────────────
# Test 2: Speak — TTS through robot speaker
# ──────────────────────────────────────────────

class TestSpeak:
    def test_speak_plays_audio(self):
        """Synthesize text and play through robot speaker."""
        print("\n--- Test: Speak ---")
        print("ACTION: Listen to the robot — it should say 'Hello, I am Reachy Mini'")
        print("  (WebRTC warmup may take 30-60 seconds...)")

        rc, stdout, stderr = run_cli(
            "-m", "reachy_mini_brain.audio", "speak",
            "Hello, I am Reachy Mini",
        )
        print(f"  Exit code: {rc}")
        print(f"  Stdout: {stdout}")
        stderr_lines = stderr.split("\n")
        print(f"  Stderr (tail): {chr(10).join(stderr_lines[-5:])}")

        assert rc == 0, f"speak failed: {stderr}"
        confirm("Robot spoke 'Hello, I am Reachy Mini' through its speaker")


# ──────────────────────────────────────────────
# Test 3: Play Sound — WAV through robot speaker
# ──────────────────────────────────────────────

class TestPlaySound:
    def test_play_generated_wav(self):
        """Generate a WAV with TTS, then play it through robot speaker."""
        print("\n--- Test: Play Sound ---")

        # Generate a WAV file locally
        wav_path = os.path.join(ARTIFACTS, "play_test.wav")
        rc, _, err = run_cli(
            "-c",
            f"from reachy_mini_brain.tts import synthesize; synthesize('Testing one two three', '{wav_path}')",
            timeout=30,
        )
        assert os.path.exists(wav_path), f"Failed to generate test WAV: {err}"
        print(f"  Generated WAV: {os.path.getsize(wav_path):,} bytes")

        print("ACTION: Listen to the robot — it should say 'Testing one two three'")
        print("  (WebRTC warmup may take 30-60 seconds...)")

        rc, stdout, stderr = run_cli(
            "-m", "reachy_mini_brain.audio", "play-sound",
            wav_path,
        )
        print(f"  Exit code: {rc}")
        stderr_lines = stderr.split("\n")
        print(f"  Stderr (tail): {chr(10).join(stderr_lines[-5:])}")

        assert rc == 0, f"play-sound failed: {stderr}"
        confirm("Robot played audio saying 'Testing one two three'")


# ──────────────────────────────────────────────
# Test 4: Video Record
# ──────────────────────────────────────────────

class TestVideo:
    def test_record_video(self):
        """Record a short video clip."""
        print("\n--- Test: Video Record (5 seconds) ---")
        video_path = os.path.join(ARTIFACTS, "test_video.mp4")

        print("ACTION: Recording 5 seconds of video from robot camera...")
        print("  (WebRTC warmup may take 30-60 seconds...)")

        rc, stdout, stderr = run_cli(
            "-m", "reachy_mini_brain.video", "record",
            "--duration", "5",
            "--out", video_path,
        )
        print(f"  Exit code: {rc}")
        print(f"  Output: {stdout}")
        stderr_lines = stderr.split("\n")
        print(f"  Stderr (tail): {chr(10).join(stderr_lines[-5:])}")

        assert rc == 0, f"record failed: {stderr}"
        assert os.path.exists(video_path), "Video file not created"

        size = os.path.getsize(video_path)
        print(f"  Video file: {size:,} bytes")
        assert size > 10000, f"Video too small ({size} bytes)"

        confirm("Video was recorded (check artifacts/audio_test/test_video.mp4)")
