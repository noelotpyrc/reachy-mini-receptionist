"""Legacy meeting transcription + trigger detection.

Status: legacy/fallback. This predates the accepted official-runtime reception
path. Keep this module runnable for reference until legacy removal is explicitly
approved.

Runs as a background process alongside the session server.
Continuously transcribes audio and appends timestamped entries
to a transcript file. When a trigger word is detected, spawns
a cla agent to handle the question.

Usage:
    # Requires session server running:
    python -m reachy_mini_brain.transcribe
    python -m reachy_mini_brain.transcribe --interval 5 --trigger "reachy"
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import click

log = logging.getLogger("transcribe")

from reachy_mini_brain.session import send_command

TRANSCRIPT_FILE = os.path.join(os.getcwd(), "reachy_transcript.txt")
TRIGGER_WORD = "reachy"
TRIGGER_COOLDOWN = 60  # seconds to ignore triggers after spawning a cla agent


# ---------------------------------------------------------------------------
# Trigger matchers — modular system for detecting trigger words in text.
#
# Each matcher implements `match(text) -> str | None`.
# Returns the matched variant string on hit, None on miss.
# To add a new matching algorithm, subclass TriggerMatcher.
# ---------------------------------------------------------------------------


class TriggerMatcher(ABC):
    """Base class for trigger word matchers."""

    @abstractmethod
    def match(self, text: str) -> str | None:
        """Check if text contains a trigger word.

        Returns the matched variant string on hit, None on miss.
        """
        ...


class VariantsMatcher(TriggerMatcher):
    """Match against a hardcoded set of known variants.

    Whisper frequently mishears "Reachy" as similar-sounding words.
    This matcher checks each word in the text against a known set.
    """

    # Known Whisper transcriptions of "Reachy" — extend as needed
    REACHY_VARIANTS: set[str] = {
        "reachy",
        "richie",
        "richy",
        "richi",
        "ritchie",
        "ritchy",
        "reechy",
        "reechi",
        "ricci",
        "reachi",
        "reggie",       # most common Whisper mishearing (confirmed live)
        "reggy",
        "regina",       # confirmed live
        "rachel",       # confirmed live
        "reachy's",     # possessive forms
        "richie's",
        "reggie's",
    }

    def __init__(self, variants: set[str] | None = None):
        self.variants = {v.lower() for v in (variants or self.REACHY_VARIANTS)}

    def match(self, text: str) -> str | None:
        """Check each word in text against the variants set."""
        # Split on non-alpha to handle punctuation (e.g. "Richie," or "Reachy!")
        words = re.findall(r"[a-zA-Z']+", text.lower())
        for word in words:
            if word in self.variants:
                return word
        return None


def get_matcher(trigger: str) -> TriggerMatcher:
    """Factory: return the appropriate matcher for a trigger word.

    Currently only supports VariantsMatcher for "reachy".
    For other trigger words, falls back to a simple exact-word matcher.
    """
    if trigger.lower() == "reachy":
        return VariantsMatcher()

    # Fallback: exact match for custom trigger words
    class ExactMatcher(TriggerMatcher):
        def __init__(self, word: str):
            self.word = word.lower()

        def match(self, text: str) -> str | None:
            words = re.findall(r"[a-zA-Z']+", text.lower())
            return self.word if self.word in words else None

    return ExactMatcher(trigger)

CLA_SYSTEM_PROMPT = """\
You are Reachy, a robot meeting assistant. You were triggered because someone \
in a meeting said your name. Your job:

1. Read the transcript file to verify the trigger is real — someone must be \
genuinely asking you a question or requesting something. If it's just a casual \
mention of your name (e.g. "have you seen that Reachy robot?"), exit silently.

2. If the trigger is real:
   a. Respond through the robot speaker: call speak "I'm working on it, give me a moment"
   b. Sleep 10 seconds to let more transcript accumulate for full question context
   c. Read the transcript file again for the complete question and meeting context
   d. Research the answer (use web search if needed)
   e. Respond through the robot speaker: call speak "<your answer>"

3. Keep answers concise — this is a meeting, don't ramble.

The transcript file is: {transcript_file}
To speak: .venv/bin/python -m reachy_mini_brain.session call speak "<text>"
To read transcript: use the Read tool on {transcript_file}
"""


def _spawn_cla_agent(transcript_file: str) -> None:
    """Spawn a cla agent to handle a triggered question (non-blocking)."""
    prompt = CLA_SYSTEM_PROMPT.format(transcript_file=transcript_file)

    cmd = [
        "cla",
        "--system-prompt", prompt,
        "--permission-mode", "acceptEdits",
        "--allowed-tools",
        'Bash(.venv/bin/python -m reachy_mini_brain.session call:*)',
        'Bash(sleep:*)',
        'WebSearch',
        'WebFetch',
        "--max-turns", "15",
        "--output-format", "json",
        "A meeting participant just said your name. Read the transcript and respond.",
    ]

    # Clear CLAUDECODE env var so cla doesn't think it's nested inside
    # another Claude Code session (transcribe.py may be launched from one)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    ts = int(time.time())
    cla_stdout = f"/tmp/cla_agent_{ts}.stdout.log"
    cla_stderr = f"/tmp/cla_agent_{ts}.stderr.log"
    fh_out = open(cla_stdout, "w")
    fh_err = open(cla_stderr, "w")
    log.info(f"cla cmd: {' '.join(cmd[:6])}...")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=fh_out,
        stderr=fh_err,
    )
    log.info(f"cla agent spawned (PID {proc.pid})")
    log.info(f"  stdout → {cla_stdout}")
    log.info(f"  stderr → {cla_stderr}")

    # Monitor completion in background thread
    def _wait():
        code = proc.wait()
        fh_out.close()
        fh_err.close()
        # Log tail of output for quick debugging
        try:
            with open(cla_stderr) as f:
                err_text = f.read().strip()
            if err_text:
                # Show last few lines of stderr
                lines = err_text.splitlines()[-5:]
                for line in lines:
                    log.info(f"  cla stderr: {line[:200]}")
        except Exception:
            pass
        try:
            with open(cla_stdout) as f:
                import json as _json
                data = _json.loads(f.read())
                result_text = data.get("result", "")[:200]
                denials = data.get("permission_denials", [])
                log.info(f"  cla result: {result_text}")
                if denials:
                    log.warning(f"  cla permission denials: {denials}")
        except Exception:
            pass
        log.info(f"cla agent (PID {proc.pid}) exited with code {code}")

    threading.Thread(target=_wait, daemon=True).start()


@click.command()
@click.option("--interval", default=5, help="Seconds between transcription cycles (default: 5)")
@click.option("--trigger", default=TRIGGER_WORD, help=f"Trigger word (default: {TRIGGER_WORD})")
@click.option(
    "--outfile",
    default=TRANSCRIPT_FILE,
    help=f"Transcript file path (default: {TRANSCRIPT_FILE})",
)
@click.option("--verbose", is_flag=True, help="Show empty reads in logs")
def main(interval: int, trigger: str, outfile: str, verbose: bool):
    """Continuously transcribe and detect trigger words."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    # Start the listener
    try:
        r = send_command("listen_start")
        if not r.get("ok"):
            click.echo(f"Error starting listener: {r.get('error')}", err=True)
            raise SystemExit(1)
    except FileNotFoundError:
        click.echo("Error: session server not running. Start with:", err=True)
        click.echo("  python -m reachy_mini_brain.session serve", err=True)
        raise SystemExit(1)
    except ConnectionRefusedError:
        click.echo("Error: session server not responding", err=True)
        raise SystemExit(1)

    # Clear transcript file
    with open(outfile, "w") as f:
        pass

    matcher = get_matcher(trigger)
    cycle = 0
    last_trigger_time = 0.0  # monotonic time of last trigger

    log.info(f"Transcribing (every {interval}s) → {outfile}")
    log.info(f"Trigger: \"{trigger}\" (matcher: {type(matcher).__name__}, cooldown: {TRIGGER_COOLDOWN}s)")
    if isinstance(matcher, VariantsMatcher):
        log.info(f"Variants: {sorted(matcher.variants)}")
    log.info("Ctrl+C to stop")

    fh = open(outfile, "a")
    try:
        while True:
            time.sleep(interval)
            cycle += 1
            try:
                r = send_command("listen_read")
            except (ConnectionRefusedError, BrokenPipeError):
                log.error("Server disconnected")
                break

            if not r.get("ok"):
                log.warning(f"Cycle {cycle}: listen_read error: {r.get('error', 'unknown')}")
                continue

            result = r.get("result", {})
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            buffer_duration = result.get("buffer_duration", 0.0) if isinstance(result, dict) else 0.0

            if not text.strip():
                log.debug(f"Cycle {cycle}: empty read ({buffer_duration}s buffer)")
                continue

            # Record timestamp and append to transcript
            grab_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
            line = f"{grab_time}\t{buffer_duration}\t{text.strip()}"
            fh.write(line + "\n")
            fh.flush()

            log.info(f"Cycle {cycle}: [{grab_time}] ({buffer_duration}s) {text.strip()}")

            # Check for trigger word
            matched = matcher.match(text)
            if matched:
                since_last = time.monotonic() - last_trigger_time
                if since_last < TRIGGER_COOLDOWN:
                    log.info(f"Cycle {cycle}: trigger \"{matched}\" suppressed (cooldown: {since_last:.0f}s < {TRIGGER_COOLDOWN}s)")
                else:
                    log.info(f"*** TRIGGER detected in cycle {cycle}! Matched variant: \"{matched}\" ***")
                    last_trigger_time = time.monotonic()
                    _spawn_cla_agent(outfile)

    except KeyboardInterrupt:
        pass
    finally:
        fh.close()
        try:
            send_command("listen_stop")
        except Exception:
            pass
        log.info(f"Stopped transcribing after {cycle} cycles.")


if __name__ == "__main__":
    main()
