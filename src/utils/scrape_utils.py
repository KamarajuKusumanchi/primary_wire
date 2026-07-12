"""
src/utils/scrape_utils.py

Shared utilities for primary_wire's scraper scripts
(scrape_q4_ir.py, scrape_investorroom.py, and any future scrapers).

Public API
----------
DATE_PATTERNS        : list of (compiled re, list[str]) -- date regex + strptime formats
TIME_RE               : compiled re matching a clock-time-with-timezone string
NewsItem             : base dataclass for a scraped press-release item
parse_date(text)     -> (date | None, str)   -- first parseable date in text + raw match
parse_time(text)     -> str                  -- first raw clock-time-with-timezone in text
extract_date_from_detail_html(html) -> (date | None, str) -- date heuristics for detail pages
parse_year_args(args)-> set[int] | None       -- resolve --year/--start-year/--end-year
filter_items(...)    -> list[NewsItem]
dedupe_by_url(...)   -> list[NewsItem]        -- drop repeat items by normalized URL
fetch_missing_dates_via_http(...)             -- fill in missing dates via detail-page fetches
write_json(...)
print_preview(...)
add_common_args(parser)                       -- attach shared CLI args to an ArgumentParser
add_network_and_debug_args(parser, ...)       -- attach --polite-delay/--timeout/--debug-dump-html/-v
configure_logging(verbose)                    -- shared logging.basicConfig() for HTTP scrapers
finalize_and_output(...)                      -- shared main() tail: filter, preview, write CSV/JSON
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

    Two things make this more robust than a single regex-then-strptime call:

    1. Tries every regex match in *text* (via finditer), not just the first
       one found. Press-release listing rows/cards routinely contain more
       than one date-like substring -- e.g. a headline that mentions a
       future event date ("... Results on February 10, 2026") plus the
       row's own separate publish-date field ("February 10, 2026") -- and
       the first one encountered isn't always the one that happens to be
       parseable (see point 2). Without this, a single bad match early in
       the text would make the whole call give up and return None even
       though a perfectly good date exists later in the same string.

    2. Tolerates AP-style abbreviated months with a trailing period, e.g.
       "Jan. 02, 2026" or "Feb. 19, 2026" -- the standard GLOBE NEWSWIRE /
       PR Newswire dateline format for Jan./Feb./Aug./Sept./Oct./Nov./Dec.
       DATE_PATTERNS' regex already matches these fine (the "." is
       optional in the pattern), but Python's %b strptime directive
       rejects the trailing period outright (`strptime("Feb. 10, 2026",
       "%b %d, %Y")` raises ValueError -- %b wants "Feb", not "Feb."). Confirmed
       via a live fetch of Robinhood's press-releases listing: several
       January/February 2026 releases use exactly this dateline style and
       were silently dropped by the year filter downstream because their
       date extraction returned None. Stripping a lone trailing period
       directly after the leading month token before the strptime attempt
       fixes this without weakening the regex match itself.
    """
    for pattern, formats in DATE_PATTERNS:
        for m in pattern.finditer(text):
            raw = m.group(0).strip()
            cleaned = re.sub(r"\s+", " ", raw)
            # Also try a period-stripped variant so "Jan. 02, 2026" parses
            # via %b just as well as "Jan 02, 2026" does.
            no_period = re.sub(r"^([A-Za-z]+)\.", r"\1", cleaned)
            for candidate in dict.fromkeys([cleaned, no_period]):  # dedupe, preserve order
                for fmt in formats:
                    try:
                        return datetime.strptime(candidate, fmt).date(), raw
                    except ValueError:
                        continue
    return None, ""


# Clock time immediately followed by a timezone abbreviation, e.g.:
#   "4:30 am EDT", "4:30 a.m. EDT", "12:15 pm PT", "9:00 AM UTC"
# Deliberately permissive about am/pm punctuation/case since IR sites are
# inconsistent about it, but requires an uppercase timezone abbreviation
# (2-5 letters) right after, so it doesn't fire on random "H:MM am/pm"
# mentions elsewhere in body text that lack a timezone.
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?\s+[A-Z]{2,5}\b")


def parse_time(text: str) -> str:
    """Return the first raw clock-time-with-timezone substring in *text*.

    e.g. parse_time("Jun 8, 2026 4:30 am EDT") -> "4:30 am EDT"

    This is intentionally NOT converted/normalized to any other timezone or
    24-hour format -- callers want the exact string as published, since that's
    the only thing that can be stated with confidence (the scraper doesn't
    know each company's local timezone conventions). Returns "" if no
    match is found (e.g. the company doesn't publish a time).
    """
    m = TIME_RE.search(text)
    return m.group(0).strip() if m else ""


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
    publish_time: str = ""
    # Raw "as published" clock time (e.g. "4:30 am EDT"), NOT converted to any
    # other timezone/format -- see parse_time() above. Left as "" for sources
    # that don't publish a time; not every scraper subclass populates this
    # (only those whose listing/detail pages actually expose a time do).

    @property
    def publish_date_str(self) -> str:
        return self.publish_date.isoformat() if self.publish_date else ""

    def to_row(self) -> dict:
        """Return a CSV-row dict using the canonical csv_utils.CSV_FIELDS keys."""
        return {
            "slug": self.slug,
            "ticker": self.ticker,
            "title": self.title,
            "url": self.url,
            "publish_date": self.publish_date_str,
            "publish_time": self.publish_time,
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


def dedupe_by_url(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Return *items* with repeat URLs removed, keeping the first occurrence.

    URLs are compared with a trailing slash stripped, matching the
    normalization each scraper already applies when first collecting items
    off a listing page (so a trailing-slash-only difference doesn't count as
    a distinct item here either).

    This is the "global dedup across year passes" step every scraper does
    once per invocation, previously reimplemented inline in
    scrape_investorroom.scrape() and (twice) in scrape_notified.scrape().
    Per-page incremental dedup while paginating (deciding whether a page
    yielded anything *new*, so a scraper knows when to stop) is a different
    job and stays local to each scraper's pagination loop.
    """
    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in items:
        key = item.url.rstrip("/")
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


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

    Some callers (e.g. scrape_notified.py) return a 3-tuple instead --
    ``(date | None, str, str)``, where the third element is a raw publish-time
    string (see ``parse_time()``) -- in which case ``item.publish_time`` is
    also filled in. Callers returning the plain 2-tuple are unaffected and
    leave ``item.publish_time`` untouched (its dataclass default is "").

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
        result = fetch_date_from_detail_page(item.url, timeout=timeout)
        if len(result) == 3:
            d, raw, time_str = result
        else:
            d, raw = result
            time_str = None
        if d:
            item.publish_date = d
            item.raw_date_text = raw
            if time_str:
                item.publish_time = time_str
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

    Items are printed in the same order they end up on disk (see
    csv_utils.SORT_FIELDS / write_csv): grouped by publish_date, and within
    a date ordered chronologically by publish_time. Without this, items
    print in whatever order the scraper happened to encounter them
    (typically reverse-chronological page order), which can disagree with
    the CSV file's order for same-day items.
    """
    items = list(items)
    if not items:
        print("No items to preview.")
        return
    from utils.csv_utils import sort_key as _csv_sort_key
    items = sorted(items, key=lambda item: _csv_sort_key(item.to_row()))
    print(f"\n{len(items)} item(s):\n")
    # Only show the publish_time column at all if at least one item actually
    # has one -- most sources never populate it, and printing an empty
    # " ()" on every line for those would just be noise.
    any_time = any(getattr(item, "publish_time", "") for item in items)
    for item in items:
        d = item.publish_date_str or "????-??-??"
        if any_time:
            t = getattr(item, "publish_time", "")
            d = f"{d} ({t})" if t else d
        cat = ""
        if show_category:
            category = getattr(item, "category", "")
            if category:
                cat = f" [{category}]"
        print(f"  {d}  {item.title}{cat}")
        print(f"             {item.url}")
    print()


# ---------------------------------------------------------------------------
# Shared main() tail: filter, preview, write CSV/JSON
# ---------------------------------------------------------------------------

def finalize_and_output(
    items: Iterable[NewsItem],
    *,
    years: Optional[set[int]],
    since: Optional[date],
    until: Optional[date],
    limit: Optional[int],
    format: str,
    output: Optional[Path],
    dry_run: bool,
    data_dir: Path,
    default_json_path: Optional[Path] = None,
    preview_fn=print_preview,
) -> list[NewsItem]:
    """Filter, preview, and write out a scraper's collected items.

    This is the common tail every scraper's main() used to reimplement
    separately, with three real behavioral differences between them that
    are resolved here (rather than silently picking one and leaving the
    others undocumented):

    1. Preview: scrape_investorroom.py only called print_preview() on
       --dry-run; scrape_notified.py and scrape_q4_ir.py always did. This
       function always previews, matching the latter two -- seeing what was
       scraped is useful whether or not you're also writing it to disk, and
       that was already the majority behavior.

    2. --format both: scrape_investorroom.py and scrape_notified.py checked
       ``if args.format == "json": ... else: <csv>``, so "both" silently
       behaved like plain "csv" and JSON was never written -- even though
       "both" was (and still is) an advertised, valid --format choice in
       add_common_args(). scrape_q4_ir.py handled it correctly with two
       independent ``in ("csv", "both")`` / ``in ("json", "both")`` checks.
       This function uses scrape_q4_ir.py's (correct) version for everyone.

    3. --output with --format json: scrape_investorroom.py/scrape_notified.py
       made this a hard, fatal ``parser.error()`` if omitted;
       scrape_q4_ir.py instead fell back to a scraper-specific default path
       under REPO_ROOT. This function takes that default-path approach for
       all scrapers via *default_json_path*, and only raises if a caller
       doesn't supply one -- so a bare ``--format json`` no longer requires
       remembering to also pass --output.

    *limit* is scrape_q4_ir.py's post-sort item cap (its --limit flag);
    pass None for scrapers that don't have one.

    *preview_fn* defaults to this module's print_preview(); scrape_q4_ir.py
    passes its own wrapper (show_category=True) to keep the category column.

    Returns the filtered list, in case a caller wants it afterward (e.g. to
    decide an exit code).
    """
    filtered = filter_items(items, years=years, since=since, until=until, limit=limit)
    logger.info("%d item(s) after filtering.", len(filtered))

    preview_fn(filtered)

    if format in ("json", "both"):
        json_path = output or default_json_path
        if json_path is None:
            raise SystemExit(
                "--output PATH is required for --format json (or both) on this scraper "
                "(no default JSON path was configured)."
            )
        write_json(filtered, json_path, dry_run)

    if format in ("csv", "both"):
        from utils.csv_utils import merge_items_into_daily_csvs, print_merge_summary

        summary = merge_items_into_daily_csvs(filtered, data_dir, dry_run)
        print_merge_summary(summary, dry_run, filtered, data_dir=data_dir)

    return filtered


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