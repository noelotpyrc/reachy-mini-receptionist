#!/usr/bin/env python3
"""Preview S2S session instructions from private Hermes profile sources."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from reachy_mini_brain.official_runtime.agent_profile import (
    AgentProfileError,
    compose_hermes_agent_profile,
    with_session_date,
)
from reachy_mini_brain.official_runtime.reception_tools import (
    with_reception_tool_instructions,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--tools", choices=("none", "time-web"), default="time-web")
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Original sources with base, not generated, HERMES.md.",
    )
    parser.add_argument("--soul", type=Path, required=True)
    parser.add_argument("--spoken-instructions", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New private review file; existing files are never overwritten.",
    )
    args = parser.parse_args()
    try:
        profile = compose_hermes_agent_profile(
            profile_id=args.profile_id,
            source_dir=args.source_dir,
            soul_path=args.soul,
            session_instructions_path=args.spoken_instructions,
        )
        if args.tools == "time-web":
            profile = with_reception_tool_instructions(profile)
        profile = with_session_date(profile)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(profile.instructions)
    except (AgentProfileError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(profile.provenance(), indent=2))


if __name__ == "__main__":
    main()
