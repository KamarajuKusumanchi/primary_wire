"""
src/utils/csv_utils.py

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
merge_items_into_daily_csvs(items, data_dir, dry_run)  -> dict
print_merge_summary(summary, dry_run, filtered, data_dir=None)
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_date", "publish_time"]
SORT_FIELDS = ["publish_date", "slug", "ticker", "publish_time", "title", "url"]

# publish_time is stored verbatim as scraped, e.g. "4:30 am EDT" -- see
# parse_time() in scrape_utils.py. It is deliberately NOT zero-padded or
# timezone-normalized there, so it cannot be sorted as a plain string: e.g.
# "10:31 pm EST" < "9:15 pm EST" lexicographically, which is backwards.
# _publish_time_sort_key() below fixes that for the common "H:MM am/pm TZ"
# shape without attempting real timezone math (no offset table is applied;
# same-timezone comparisons are exact, cross-timezone comparisons are only
# as good as clock time, same as before this change).
_TIME_SORT_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([APap])\.?[Mm]\.?\s+[A-Z]{2,5}\b")


def _publish_time_sort_key(value: str) -> tuple[int, int, int]:
    """Return a zero-padded, chronologically-sortable key for one row's
    raw publish_time string.

    Rows whose publish_time is empty or doesn't match the expected
    "H:MM am/pm TZ" shape sort *after* every row with a recognizable time --
    "no time" is less specific than a real one, not earlier in the day.
    """
    if value:
        m = _TIME_SORT_RE.match(value.strip())
        if m:
            hour, minute, meridiem = m.groups()
            hour = int(hour) % 12
            if meridiem.lower() == "p":
                hour += 12
            return (0, hour, int(minute))
    return (1, 0, 0)


def _sort_key(row: dict) -> tuple:
    """Build the SORT_FIELDS sort key for one row.

    Every field sorts on its raw string value except publish_time, which is
    routed through _publish_time_sort_key() so that it sorts chronologically
    instead of lexicographically (see the note above SORT_FIELDS).
    """
    key: list = []
    for field in SORT_FIELDS:
        value = row.get(field, "")
        if field == "publish_time":
            key.append(_publish_time_sort_key(value))
        else:
            key.append(value)
    return tuple(key)


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
    rows_sorted = sorted(rows, key=_sort_key)
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


# ---------------------------------------------------------------------------
# NewsItem-level convenience wrappers
# ---------------------------------------------------------------------------
# The two helpers below sit on top of merge_into_daily_csvs() and are shared
# by scrape_q4_ir.py, scrape_investorroom.py, and scrape_notified.py, whose
# per-scraper merge_into_daily_csvs()/print output were otherwise near-identical
# copies of each other.

def merge_items_into_daily_csvs(
    items: Iterable,
    data_dir: Path,
    dry_run: bool,
) -> dict:
    """Group scraped items by publish date and merge them into daily CSVs.

    *items* must expose ``.publish_date`` (date | None), ``.to_row()``,
    ``.title``, and ``.url`` -- i.e. anything satisfying scrape_utils.NewsItem.
    Items with no resolvable date are skipped (and logged individually)
    rather than passed to merge_into_daily_csvs().

    Returns the same summary dict as merge_into_daily_csvs(), with an added
    "undated" key: the number of items skipped for lack of a date.
    """
    items = list(items)
    dated = [item for item in items if item.publish_date is not None]
    undated = [item for item in items if item.publish_date is None]

    rows_by_date: dict[date, list[dict]] = {}
    for item in dated:
        rows_by_date.setdefault(item.publish_date, []).append(item.to_row())

    summary = merge_into_daily_csvs(rows_by_date, data_dir, dry_run)
    summary["undated"] = len(undated)

    if undated:
        logger.warning(
            "%d item(s) had no resolvable publish date and were NOT written to any "
            "daily CSV. Re-run with --fetch-detail-pages to attempt resolution "
            "(or --debug-dump-html / --verbose to diagnose):",
            len(undated),
        )
        for item in undated:
            logger.warning("  UNDATED: %s | %s", item.title, item.url)

    return summary


def print_merge_summary(
    summary: dict,
    dry_run: bool,
    filtered: Iterable,
    data_dir: Optional[Path] = None,
) -> None:
    """Print the one-line "Wrote N new + M updated row(s) ..." summary.

    *filtered* is the item list that was passed to the merge; on a dry run
    (when nothing is actually written, so summary["files_written"] stays 0)
    it is used to count how many distinct dated files *would* have been
    written. Pass *data_dir* to include "under <data_dir>" in the message.
    """
    filtered = list(filtered)
    action = "Would write" if dry_run else "Wrote"
    dated_file_count = (
        summary["files_written"] if not dry_run
        else len({i.publish_date for i in filtered if i.publish_date})
    )
    skipped = f" ({summary['undated']} undated item(s) skipped)" if summary.get("undated") else ""
    location = f" under {data_dir}" if data_dir is not None else ""
    print(
        f"{action} {summary['rows_added']} new + {summary['rows_updated']} updated row(s) "
        f"across {dated_file_count} daily CSV file(s){location}{skipped}"
    )