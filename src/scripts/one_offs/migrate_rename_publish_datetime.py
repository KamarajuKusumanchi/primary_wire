#!/usr/bin/env python3
"""
migrate_rename_publish_datetime.py

One-off migration: rename the ``publish_datetime`` column to ``publish_date``
in every data/YYYY/YYYY-MM-DD.csv file, in place.

This only renames the header. It does NOT invent a publish_time column or
otherwise change any values -- if a value in the column is not a plain
YYYY-MM-DD date (e.g. it has a time or timezone appended), it is left as-is
and printed as a warning so you can decide by hand what to do with it later.

Usage:
    python src/migrate_rename_publish_datetime.py [--data-dir data] [--dry-run]
"""

import argparse
import csv
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
OLD_COL = "publish_datetime"
NEW_COL = "publish_date"
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def migrate_file(path: Path, dry_run: bool) -> tuple[bool, list[str]]:
    """Rename the column in *path*. Returns (changed, warnings)."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        return False, []

    header = rows[0]
    if OLD_COL not in header:
        return False, []

    col_idx = header.index(OLD_COL)
    new_header = list(header)
    new_header[col_idx] = NEW_COL

    warnings = []
    for row_num, row in enumerate(rows[1:], start=2):
        if col_idx < len(row) and row[col_idx] and not DATE_ONLY_RE.match(row[col_idx]):
            warnings.append(
                f"{path}: row {row_num} has non-date value {row[col_idx]!r} in "
                f"what is now the {NEW_COL!r} column -- left unchanged"
            )

    if dry_run:
        return True, warnings

    new_rows = [new_header] + rows[1:]
    with open(path, "w", newline="", encoding="utf-8") as f:
        # lineterminator="\n": the source files use plain LF, but csv.writer
        # defaults to "\r\n" (RFC 4180) which would rewrite every line ending
        # and blow up the git diff for no reason.
        writer = csv.writer(f, lineterminator="\n")
        writer.writerows(new_rows)

    return True, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "data",
        help="Root of the data/ tree to migrate (default: repo_root/data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing any files",
    )
    args = parser.parse_args()

    csv_paths = sorted(args.data_dir.glob("*/*.csv"))
    if not csv_paths:
        print(f"No CSV files found under {args.data_dir}")
        return

    changed_count = 0
    all_warnings = []
    for path in csv_paths:
        changed, warnings = migrate_file(path, args.dry_run)
        if changed:
            changed_count += 1
        all_warnings.extend(warnings)

    verb = "Would rename" if args.dry_run else "Renamed"
    print(f"{verb} '{OLD_COL}' -> '{NEW_COL}' in {changed_count} of {len(csv_paths)} file(s).")

    if all_warnings:
        print(f"\n{len(all_warnings)} row(s) with non-date values (unchanged, needs a manual look):")
        for w in all_warnings:
            print(f"  {w}")


if __name__ == "__main__":
    main()
