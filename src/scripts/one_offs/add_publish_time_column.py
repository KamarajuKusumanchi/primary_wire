#!/usr/bin/env python3
"""
src/scripts/add_publish_time_column.py

One-time migration: add the ``publish_time`` column to every existing
data/YYYY/YYYY-MM-DD.csv file that doesn't already have it, backfilling ""
for all existing rows (their real publish time was never captured, so there
is nothing truthful to put there -- see README/task notes on the
publish_time column added to scrape_notified.py et al.).

This is a one-time backfill. Going forward, csv_utils.write_csv() already
writes the publish_time column for every file it touches (via CSV_FIELDS),
so any daily file that gets re-merged by a scraper will pick up the column
automatically even without this script. This script exists only to bring
*untouched* historical files in line immediately, rather than waiting for
each one to be naturally re-scraped.

Usage:
    python src/scripts/add_publish_time_column.py            # dry-run (default)
    python src/scripts/add_publish_time_column.py --write    # actually rewrite files
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = REPO_ROOT / "data"

CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_date", "publish_time"]


def migrate_file(path: Path, write: bool) -> tuple[bool, int]:
    """Return (changed, row_count). Adds publish_time="" to every row if the
    file doesn't already have that column; leaves untouched files alone.
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    if "publish_time" in fieldnames:
        return False, len(rows)

    for row in rows:
        row.setdefault("publish_time", "")

    if write:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    return True, len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true",
        help="Actually rewrite files. Without this flag, only prints what would change.",
    )
    args = parser.parse_args()

    csv_paths = sorted(DATA_DIR.glob("*/*.csv"))
    changed_count = 0
    for path in csv_paths:
        changed, row_count = migrate_file(path, write=args.write)
        rel = path.relative_to(REPO_ROOT)
        if changed:
            changed_count += 1
            action = "Added" if args.write else "[dry-run] Would add"
            print(f"{action} publish_time column to {rel} ({row_count} row(s))")

    print(f"\n{'Migrated' if args.write else '[dry-run] Would migrate'} "
          f"{changed_count} of {len(csv_paths)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())