"""
src/utils/scrape_utils.py

Shared utilities for primary_wire's scraper scripts
(scrape_q4_ir.py, scrape_investorroom.py, and any future scrapers).

Public API
----------
DATE_PATTERNS        : list of (compiled re, list[str]) -- date regex + strptime formats
NewsItem             : base dataclass for a scraped press-release item
parse_date(text)     -> (date | None, str)   -- first parseable date in text + raw match
extract_date_from_detail_html(html) -> (date | None, str) -- date heuristics for detail pages
parse_year_args(args)-> set[int] | None       -- resolve --year/--start-year/--end-year
filter_items(...)    -> list[NewsItem]
fetch_missing_dates_via_http(...)             -- fill in missing dates via detail-page fetches
write_json(...)
print_preview(...)
add_common_args(parser)                       -- attach shared CLI args to an ArgumentParser
add_network_and_debug_args(parser, ...)       -- attach --polite-delay/--timeout/--debug-dump-html/-v
configure_logging(verbose)                    -- shared logging.basicConfig() for HTTP scrapers
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # extract_date_from_detail_html() raises a clear error if actually called

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


# Common date CSS selectors seen across IR platforms. Used by
# extract_date_from_detail_html() below.
_DETAIL_PAGE_DATE_SELECTORS = [
    "span.date", "p.date", "div.date",
    ".press-release-date", ".release-date", ".article-date",
    ".news-date", ".pr-date", ".date-label",
    "[class*='date']",
]


def extract_date_from_detail_html(html: str) -> tuple[Optional[date], str]:
    """Extract a publish date from a press-release detail page's HTML.

    Tries, in priority order:
      1. Any <time> element's ``datetime`` attribute or text.
      2. Common date CSS selectors used across IR platforms
         (see _DETAIL_PAGE_DATE_SELECTORS).
      3. A scan of the first ~2000 characters of the <article>/<main>/<body>
         text, whichever is found first.

    Shared by scrape_investorroom.py and scrape_notified.py, whose detail
    pages happen to follow this same layout convention despite being
    different IR platforms. scrape_q4_ir.py's detail pages don't, and use
    their own heuristic (a fixed-size scan anchored on the headline).
    """
    if BeautifulSoup is None:
        raise ImportError("Missing dependency. Install with: pip install beautifulsoup4 lxml")

    soup = BeautifulSoup(html, "lxml")

    for time_tag in soup.find_all("time"):
        dt_attr = time_tag.get("datetime", "")
        if dt_attr:
            d, raw = parse_date(dt_attr)
            if d:
                return d, raw
        d, raw = parse_date(time_tag.get_text(strip=True))
        if d:
            return d, raw

    for sel in _DETAIL_PAGE_DATE_SELECTORS:
        el = soup.select_one(sel)
        if el:
            d, raw = parse_date(el.get_text(strip=True))
            if d:
                return d, raw

    article = soup.find("article") or soup.find("main") or soup.find("body")
    if article:
        d, raw = parse_date(article.get_text(separator=" ", strip=True)[:2000])
        if d:
            return d, raw

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
# Detail-page date fallback (HTTP-based scrapers)
# ---------------------------------------------------------------------------

def fetch_missing_dates_via_http(
    items: Iterable[NewsItem],
    fetch_date_from_detail_page,
    polite_delay: float,
    timeout: int,
) -> None:
    """Mutate ``items`` in place: fetch a detail page for each item with no
    ``publish_date`` and fill it in from there.

    ``fetch_date_from_detail_page`` is a ``(url, timeout=...) -> (date | None, str)``
    callable supplied by the caller (each platform fetches over a different
    HTTP stack -- plain ``requests`` for scrape_investorroom.py, ``curl_cffi``
    for scrape_notified.py). This function only owns the shared looping,
    pacing, and logging.

    Shared by scrape_investorroom.py and scrape_notified.py. scrape_q4_ir.py
    needs a live Playwright browser session per fetch instead and keeps its
    own implementation.
    """
    items = list(items)
    missing = [item for item in items if item.publish_date is None]
    if not missing:
        return

    logger.info("Fetching detail pages to resolve dates for %d item(s)...", len(missing))
    for i, item in enumerate(missing):
        if i > 0:
            time.sleep(polite_delay)
        d, raw = fetch_date_from_detail_page(item.url, timeout=timeout)
        if d:
            item.publish_date = d
            item.raw_date_text = raw
            logger.debug("Resolved date %s for: %s", d, item.title)
        else:
            logger.warning("Could not resolve date for: %s | %s", item.title, item.url)

    still_missing = sum(1 for item in items if item.publish_date is None)
    if still_missing:
        logger.warning(
            "%d item(s) still have no date after detail-page fetch; "
            "they will be skipped in CSV output.",
            still_missing,
        )


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


def add_network_and_debug_args(
    parser: argparse.ArgumentParser,
    *,
    default_polite_delay: float = 15.0,
) -> argparse._ArgumentGroup:
    """Attach the "network" and "debug" argument groups shared by the
    HTTP-based scrapers (scrape_investorroom.py, scrape_notified.py).

    Covers --polite-delay, --timeout, --debug-dump-html, and --verbose.
    Returns the "network" group so callers can add their own extra options
    to it (e.g. scrape_investorroom.py's --page-limit).
    """
    network = parser.add_argument_group("network")
    network.add_argument(
        "--polite-delay", type=float, default=default_polite_delay, metavar="SECONDS",
        help=f"Seconds between requests (default: {default_polite_delay:g}).",
    )
    network.add_argument("--timeout", type=int, default=30, metavar="SECONDS")

    debug = parser.add_argument_group("debug")
    debug.add_argument(
        "--debug-dump-html", type=Path, default=None, metavar="PATH",
        help="Save the first fetched listing page HTML to PATH.",
    )
    debug.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG-level logging.",
    )

    return network


def configure_logging(verbose: bool) -> None:
    """Configure root logging with the timestamped format shared by the
    HTTP-based scrapers (scrape_investorroom.py, scrape_notified.py).

    scrape_q4_ir.py uses a different scheme (-v/-vv counted verbosity, no
    timestamp) and configures logging itself instead of calling this.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )