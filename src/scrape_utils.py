"""
scrape_utils.py

Shared utilities for primary_wire's scraper scripts
(scrape_q4_ir.py, scrape_investorroom.py, and any future scrapers).

Public API
----------
DATE_PATTERNS        : list of (compiled re, list[str]) -- date regex + strptime formats
NewsItem             : base dataclass for a scraped press-release item
parse_date(text)     -> (date | None, str)   -- first parseable date in text + raw match
parse_year_args(args)-> set[int] | None       -- resolve --year/--start-year/--end-year
filter_items(...)    -> list[NewsItem]
write_json(...)
print_preview(...)
add_common_args(parser)                       -- attach shared CLI args to an ArgumentParser
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_MONTH_NAMES = (
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)

# Each entry is (compiled pattern, list of strptime format strings).
# Tried in order; the first match that parses successfully wins.
DATE_PATTERNS: list[tuple[re.Pattern, list[str]]] = [
    # "Jun 18, 2026" / "June 18, 2026"
    (re.compile(rf"\b(?:{_MONTH_NAMES})\.?\s+\d{{1,2}},\s*\d{{4}}\b"), ["%b %d, %Y", "%B %d, %Y"]),
    # "06/18/2026"
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"), ["%m/%d/%Y"]),
    # "2026-06-18"
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), ["%Y-%m-%d"]),
]


def parse_date(text: str) -> tuple[Optional[date], str]:
    """Return the first parseable date found in *text* and its raw matched string.

    Returns (None, "") if no date is recognised.
    """
    for pattern, formats in DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(0).strip()
        cleaned = re.sub(r"\s+", " ", raw)
        for fmt in formats:
            try:
                return datetime.strptime(cleaned, fmt).date(), raw
            except ValueError:
                continue
    return None, ""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
    """Minimal press-release record shared across scrapers.

    Scraper-specific subclasses may add extra fields (e.g. ``category`` in
    scrape_q4_ir.py) without breaking shared helpers that only touch the
    fields defined here.
    """

    slug: str
    ticker: str
    title: str
    url: str
    publish_date: Optional[date]
    raw_date_text: str = ""

    @property
    def publish_datetime(self) -> str:
        return self.publish_date.isoformat() if self.publish_date else ""

    def to_row(self) -> dict:
        """Return a CSV-row dict using the canonical csv_utils.CSV_FIELDS keys."""
        return {
            "slug": self.slug,
            "ticker": self.ticker,
            "title": self.title,
            "url": self.url,
            "publish_datetime": self.publish_datetime,
        }


# ---------------------------------------------------------------------------
# Year argument parsing
# ---------------------------------------------------------------------------

def parse_year_args(args: argparse.Namespace) -> Optional[set[int]]:
    """Resolve ``--year`` / ``--start-year`` / ``--end-year`` into a year set.

    Handles half-specified ranges (only --start-year or only --end-year given)
    by treating the missing bound as equal to the present one.  Returns None
    when no year filter was requested, meaning "all years".

    Mutates *args* in place to normalise start_year / end_year, matching the
    behaviour of the original per-scraper implementations.
    """
    if args.start_year and args.end_year is None:
        args.end_year = args.start_year
    if args.end_year and args.start_year is None:
        args.start_year = args.end_year

    years: set[int] = set()
    if args.year:
        years.update(args.year)
    if args.start_year or args.end_year:
        start = args.start_year or args.end_year
        end = args.end_year or args.start_year
        if start > end:
            start, end = end, start
        years.update(range(start, end + 1))
    return years or None


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_items(
    items: Iterable[NewsItem],
    years: Optional[set[int]],
    since: Optional[date],
    until: Optional[date],
    limit: Optional[int],
) -> list[NewsItem]:
    """Filter, sort, and optionally cap a sequence of NewsItems.

    Items with no ``publish_date`` are excluded whenever any date-based filter
    (years, since, until) is active.  When all three are None the dateless
    items pass through (callers that always want them dropped should pre-filter).
    """
    result = []
    for item in items:
        if years is not None and (item.publish_date is None or item.publish_date.year not in years):
            continue
        if since is not None and (item.publish_date is None or item.publish_date < since):
            continue
        if until is not None and (item.publish_date is None or item.publish_date > until):
            continue
        result.append(item)
    result.sort(key=lambda i: (i.publish_date or date.min, i.title))
    if limit is not None:
        result = result[:limit]
    return result


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_json(items: Iterable[NewsItem], path: Path, dry_run: bool) -> None:
    """Serialise *items* to a JSON file at *path*.

    Each item is serialised via its ``to_row()`` method so the output matches
    the CSV column layout.  Skipped (with a log message) when *dry_run* is True.
    """
    payload = [item.to_row() for item in items]
    if dry_run:
        logger.info("[dry-run] Would write %d item(s) to %s", len(payload), path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info("Wrote %d item(s) to %s", len(payload), path)


def print_preview(items: Iterable[NewsItem], *, show_category: bool = False) -> None:
    """Print a human-readable summary of *items* to stdout.

    Pass ``show_category=True`` for scrapers that populate the ``category``
    attribute (e.g. scrape_q4_ir.py) so that field appears in the output.
    """
    items = list(items)
    if not items:
        print("No items to preview.")
        return
    print(f"\n{len(items)} item(s):\n")
    for item in items:
        d = item.publish_datetime or "????-??-??"
        cat = ""
        if show_category:
            category = getattr(item, "category", "")
            if category:
                cat = f" [{category}]"
        print(f"  {d}  {item.title}{cat}")
        print(f"             {item.url}")
    print()


# ---------------------------------------------------------------------------
# Shared CLI argument groups
# ---------------------------------------------------------------------------

def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Attach the argument groups that are identical across all scrapers.

    Covers: --slug / --ticker / --url (source), year/date filtering
    (--year, --start-year, --end-year, --since, --until), and the
    output trinity (--format, --output, --dry-run).

    Scraper-specific groups (browser options, network options, etc.) are
    added by each scraper's own ``build_arg_parser()``.
    """
    source = parser.add_argument_group("source")
    source.add_argument(
        "--url", default=None,
        help="IR site URL. If omitted, derived from sources.yaml via --slug or --ticker.",
    )
    source.add_argument(
        "--slug", default=None,
        help="sources.yaml slug.",
    )
    source.add_argument(
        "--ticker", default=None,
        help="Ticker symbol.",
    )

    filt = parser.add_argument_group("filtering")
    filt.add_argument(
        "--year", type=int, action="append", metavar="YYYY",
        help="Year to scrape. Repeatable: --year 2024 --year 2025",
    )
    filt.add_argument("--start-year", type=int, metavar="YYYY",
                      help="Start of an inclusive year range.")
    filt.add_argument("--end-year", type=int, metavar="YYYY",
                      help="End of an inclusive year range.")
    filt.add_argument(
        "--since", type=lambda s: date.fromisoformat(s), metavar="YYYY-MM-DD",
        help="Only keep items on/after this date.",
    )
    filt.add_argument(
        "--until", type=lambda s: date.fromisoformat(s), metavar="YYYY-MM-DD",
        help="Only keep items on/before this date.",
    )

    out = parser.add_argument_group("output")
    out.add_argument(
        "--format", choices=["csv", "json", "both"], default="csv",
        help=(
            "csv = merge into data/YYYY/YYYY-MM-DD.csv files (default); "
            "json = single combined file; both = write both."
        ),
    )
    out.add_argument(
        "--output", type=Path, default=None, metavar="PATH",
        help="Output path for --format json (or both).",
    )
    out.add_argument(
        "--dry-run", action="store_true",
        help="Parse/scrape and show what would be written, but write nothing.",
    )