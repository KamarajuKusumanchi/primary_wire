"""
csv_utils.py

Shared helpers for reading, merging, and writing primary_wire's daily
data/YYYY/YYYY-MM-DD.csv files.

Used by scrape_q4_ir.py, scrape_investorroom.py, and any future scraper that
follows the same per-date CSV layout.

Public API
----------
CSV_FIELDS       : list[str]  -- canonical column order
SORT_FIELDS      : list[str]  -- sort key for every CSV write
csv_path_for_date(data_dir, d) -> Path
load_csv(path)                 -> list[dict]
write_csv(path, rows)          -- sorts then writes
merge_into_daily_csvs(rows_by_date, data_dir, dry_run) -> dict
"""

from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_datetime"]
SORT_FIELDS = ["publish_datetime", "slug", "ticker", "title", "url"]


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------

def csv_path_for_date(data_dir: Path, d: date) -> Path:
    return data_dir / str(d.year) / f"{d.isoformat()}.csv"


# ---------------------------------------------------------------------------
# Low-level I/O
# ---------------------------------------------------------------------------

def load_csv(path: Path) -> list[dict]:
    """Return the rows of an existing daily CSV, or [] if it does not exist."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict]) -> None:
    """Sort *rows* by SORT_FIELDS and write them to *path*, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_sorted = sorted(rows, key=lambda r: tuple(r.get(k, "") for k in SORT_FIELDS))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows_sorted)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merge_into_daily_csvs(
    rows_by_date: dict[date, Iterable[dict]],
    data_dir: Path,
    dry_run: bool,
) -> dict:
    """Merge scraped rows into per-date CSV files under *data_dir*.

    Parameters
    ----------
    rows_by_date:
        Mapping of publish_date -> iterable of CSV-row dicts (keys = CSV_FIELDS).
        Only dated items should appear here; callers must filter out undated ones
        before calling this function.
    data_dir:
        Root of the data/ tree, e.g. ``repo_root / "data"``.
    dry_run:
        When True, log what would be written but do not touch the filesystem.

    Returns
    -------
    A summary dict::

        {
            "files_written": int,   # 0 on dry-run
            "rows_added":   int,
            "rows_updated": int,
        }
    """
    summary = {"files_written": 0, "rows_added": 0, "rows_updated": 0}

    for d, new_rows in sorted(rows_by_date.items()):
        new_rows = list(new_rows)
        path = csv_path_for_date(data_dir, d)
        existing_rows = load_csv(path)
        existing_by_url = {r["url"]: r for r in existing_rows}

        new_count, updated_count = 0, 0
        for r in new_rows:
            if r["url"] not in existing_by_url:
                new_count += 1
            elif r != existing_by_url[r["url"]]:
                # Same URL but at least one field differs -- a real update.
                updated_count += 1
            # else: identical row already on disk, count nothing.

        # Skip the write entirely when there is nothing to change.
        if new_count == 0 and updated_count == 0:
            logger.debug("Skipping %s -- no new or updated rows.", path)
            continue

        # Upsert: drop stale copies of any URL we are about to write, then append.
        new_urls = {r["url"] for r in new_rows}
        merged = [r for r in existing_rows if r["url"] not in new_urls] + new_rows

        summary["rows_added"] += new_count
        summary["rows_updated"] += updated_count

        rel_path = (
            path.relative_to(data_dir.parent)
            if data_dir.parent in path.parents
            else path
        )
        if dry_run:
            logger.info(
                "[dry-run] Would write %s (%d new, %d updated, %d total rows)",
                rel_path, new_count, updated_count, len(merged),
            )
            continue

        write_csv(path, merged)
        summary["files_written"] += 1
        logger.info(
            "Wrote %s (%d new, %d updated, %d total rows)",
            rel_path, new_count, updated_count, len(merged),
        )

    return summary