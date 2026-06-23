"""Legacy alert engine — the SEPARATE old-daemon "check & react" process.

Status: legacy/fallback. The accepted product path handles reception policies in
``reachy_mini_brain.official_runtime``. Keep this module runnable for
regression/reference until legacy removal is explicitly approved.

Tails the perception event log (events.jsonl) and maps each event type to a robot
action — `approach` -> greet, `depart` -> goodbye — applying a per-type cooldown.
Decoupled from perception on purpose: perception only observes & emits; this
process decides what to do.

Run alongside the reception daemon:
    python -m reachy_mini_brain.alert_engine [--events PATH] [--cooldown SEC]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from reachy_mini_brain import reception
from reachy_mini_brain.perception import DEFAULT_EVENTS_PATH


def _tail(path: Path, from_end: bool = True):
    """Yield JSON objects from lines appended to `path` (like `tail -f`)."""
    while not path.exists():
        time.sleep(0.5)
    with path.open() as f:
        if from_end:
            f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)  # poll fast — this is on the wave->reaction critical path
                continue
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def run(events_path: Path, cooldown: float = 15.0, types: set[str] | None = None) -> None:
    import sys
    try:
        sys.stdout.reconfigure(line_buffering=True)  # flush each line (long-running process)
    except Exception:
        pass
    # event type -> daemon command. Independent per-type cooldown so a greet and a
    # goodbye don't suppress each other. `types` optionally restricts which fire.
    actions = {"approach": "react", "depart": "farewell", "wave": "start_conversation"}
    if types:
        actions = {k: v for k, v in actions.items() if k in types}
    print(f"alert engine: watching {events_path} (cooldown {cooldown}s, acting on {sorted(actions)})")
    last: dict[str, float] = {}
    for ev in _tail(Path(events_path)):
        etype = ev.get("type")
        cmd = actions.get(etype)
        if cmd is None:
            continue
        now = time.time()
        if now - last.get(etype, 0.0) < cooldown:
            print(f"  {etype} id={ev.get('id')} — within cooldown, skip")
            continue
        last[etype] = now
        print(f"  {etype} id={ev.get('id')} -> {cmd}")
        try:
            res = reception._client(cmd)
            print("    daemon:", res.get("result") if res.get("ok") else f"ERROR {res.get('error')}")
        except (FileNotFoundError, ConnectionRefusedError):
            print(f"    {cmd} failed: reception daemon not running")
        except Exception as e:  # noqa: BLE001
            print(f"    {cmd} failed: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=str(DEFAULT_EVENTS_PATH), help="event log to tail")
    ap.add_argument("--cooldown", type=float, default=15.0, help="min seconds between reactions")
    ap.add_argument("--types", default=None, help="comma-separated event types to act on (default: all)")
    args = ap.parse_args()
    types = set(args.types.split(",")) if args.types else None
    run(Path(args.events), args.cooldown, types)
