#!/usr/bin/env python3
"""
get_source.py

Show the sources.yaml record(s) for one or more slugs or tickers.

Usage:
    python get_source.py costco
    python get_source.py FDX
    python get_source.py costco FDX NVDA
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources_utils import SOURCES_PATH, find_source, load_sources


def format_record(record: dict) -> str:
    lines = []
    for key in ("slug", "name", "ticker", "ir_url", "notes"):
        value = record.get(key)
        if value is not None:
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Show sources.yaml record(s) for slug(s) or ticker(s).",
        epilog="Example: %(prog)s costco FDX",
    )
    parser.add_argument(
        "queries",
        nargs="+",
        metavar="SLUG_OR_TICKER",
        help="One or more slugs or ticker symbols",
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=SOURCES_PATH,
        help="Path to sources.yaml (default: sources/sources.yaml relative to this script)",
    )
    args = parser.parse_args()

    sources = load_sources(args.sources)

    # Deduplicate queries, treating them as case-insensitive and stripping
    # whitespace. Preserve the original string for display purposes.
    seen = {}
    for q in args.queries:
        key = q.strip().lower()
        if key not in seen:
            seen[key] = q

    exit_code = 0
    for normalized, original in seen.items():
        record = find_source(sources, normalized)
        if record is None:
            print(f"Not found: {original}", file=sys.stderr)
            exit_code = 1
        else:
            print(f"{original}:")
            print(format_record(record))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()