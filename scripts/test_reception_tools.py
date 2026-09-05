#!/usr/bin/env python3
"""Direct non-robot smoke for time_now or one Firecrawl web search."""

import argparse
import asyncio
import json

from reachy_mini_brain.official_runtime.env import load_project_env
from reachy_mini_brain.official_runtime.realtime_tools import ToolError
from reachy_mini_brain.official_runtime.reception_tools import current_time, search_web


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="tool", required=True)
    clock = sub.add_parser("time_now")
    clock.add_argument("--timezone", default="America/New_York")
    search = sub.add_parser("web_search")
    search.add_argument("query")
    search.add_argument("--source", choices=("web", "news", "both"), default="web")
    args = parser.parse_args()
    load_project_env()
    try:
        if args.tool == "time_now":
            result = current_time(args.timezone)
        else:
            result = asyncio.run(search_web(args.query, args.source))
    except ToolError as exc:
        parser.error(f"{exc.category}: {exc}")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
