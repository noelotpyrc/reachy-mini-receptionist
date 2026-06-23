#!/usr/bin/env python3
"""Live feedback markers for a robot test run.

Turns in-the-moment subjective feedback into timestamps aligned to the run, so it
can be joined to events / audio / video later instead of recalled from memory.

Phase 1 - live: press Enter to stamp "now" as the next marker. Type a few words
first for a quick inline note; leave it blank to fill in later. Ctrl-D / Ctrl-C ends.
Phase 2 - annotate: for each blank marker, type your full feedback.

Run from project root, ON m1max (same clock as the daemon), in a second pane:

    .venv/bin/python scripts/m1max/mark.py

No run-id needed - it locks onto the active (open) run automatically. Writes
artifacts/markers-<run_id>.jsonl ({run_id, n, ts, clock, note}).
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

ARTIFACTS = Path("artifacts")


def find_run():
    """Return (run_id, is_open). Prefer the open run (manifest with no ended_ts);
    fall back to the newest manifest so a just-crashed run is still markable."""
    runs = list(ARTIFACTS.glob("**/runs/run-*.json"))
    if not runs:
        return None, False
    open_runs = [p for p in runs if _open(p)]
    newest = max(open_runs or runs, key=lambda p: p.stat().st_mtime)
    return newest.stem[len("run-"):], bool(open_runs)


def _open(path):
    try:
        return "ended_ts" not in json.loads(path.read_text())
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", help="override; defaults to the active (open) run")
    args = ap.parse_args()

    if args.run_id:
        run_id = args.run_id
    else:
        run_id, is_open = find_run()
        if not run_id:
            print("No run manifest under artifacts/ - start the run first, or pass --run-id.")
            return
        if not is_open:
            print(f"⚠  No open run found; newest ({run_id}) is already closed.")
            try:
                input("   Mark into it anyway? [Enter = yes, Ctrl-C = abort] ")
            except (EOFError, KeyboardInterrupt):
                print("\naborted.")
                return

    path = ARTIFACTS / f"markers-{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"run_id={run_id}  ->  {path}")
    print("Enter = mark now (type words first for a quick note).  Ctrl-D = done.\n")

    markers, n = [], 0
    while True:
        try:
            note = input(f"[{n + 1}] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        n += 1
        m = {"run_id": run_id, "n": n, "ts": time.time(),
             "clock": datetime.now().strftime("%H:%M:%S"), "note": note}
        markers.append(m)
        with path.open("a") as f:                 # append live = survives a crash / battery-off
            f.write(json.dumps(m) + "\n")
        print(f"   ● #{n} @ {m['clock']}")

    blanks = [m for m in markers if not m["note"]]
    if blanks:
        print(f"\nAnnotate {len(blanks)} marker(s):")
        for m in blanks:
            try:
                m["note"] = input(f"  #{m['n']} @ {m['clock']}: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
        with path.open("w") as f:                 # rewrite once, notes filled in
            for m in markers:
                f.write(json.dumps(m) + "\n")
    print(f"\n{n} marker(s) -> {path}")


if __name__ == "__main__":
    main()
