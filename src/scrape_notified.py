#!/usr/bin/env python3
"""
scrape_notified.py

Scrape press-release listings from any IR site powered by the Notified
(formerly Business Wire / Nasdaq IR) platform built on Drupal 10 and merge
them into primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

Platform fingerprints
---------------------
You can identify a Notified/Drupal IR site by any of:

  * The listing URL ends with /financial-releases, /news-releases, or similar
    (no .aspx extension)
  * <meta name="Generator" content="Drupal 10 ..."> in the page <head>
  * Pagination uses ?page=0, ?page=1, ... (0-based page index)
  * Detail pages use paths like:
      /news-releases/news-release-details/<slug>
  * Dates in listing table are in M/D/YY format (e.g. "6/26/26", "11/24/25")
  * Year filter available via a dropdown but NOT reflected in the URL --
    the page reloads via a form POST, so year-filtering through the URL is
    NOT supported; instead filter by year client-side after scraping.

URL structure
-------------
Listing page (paginated by 0-based page index):
  {base_url}/{news_releases_path}                 (same as ?page=0, the first page)
  {base_url}/{news_releases_path}?page=0          first page (explicit)
  {base_url}/{news_releases_path}?page=1          second page
  {base_url}/{news_releases_path}?page=N          N+1-th page

The last page index can be read from the "last »" pagination link.

There is NO server-side ?year= or ?l= parameter; page size is fixed
server-side (10 items/page for AbbVie).

Press release detail pages:
  {base_url}/news-releases/news-release-details/<slug>

Dates appear in the listing table's first column in M/D/YY format
(e.g. "6/26/26" = June 26, 2026).  Two-digit years are assumed to be
in the 2000s (i.e. "26" -> 2026).  Dates are also present verbatim in
each row's summary text (e.g. "NORTH CHICAGO, Ill., June 26, 2026")
which is used as a fallback.

Some sites (e.g. AMD's card-based listing, which has no <tr> at all) show a
long-form date immediately followed by a publish time and timezone in the
same row/card text, e.g. "Jun 8, 2026 4:30 am EDT". When present, that raw
time-with-timezone substring (e.g. "4:30 am EDT") is captured verbatim into
the publish_time CSV column -- see parse_time() in utils/scrape_utils.py.
It is NOT converted to any other timezone or format. Sites that don't
publish a time leave this column blank.

Usage
-----
  # Default: scrape AbbVie, dry-run (no files written)
  python src/scrape_notified.py --dry-run

  # Write real data for AbbVie
  python src/scrape_notified.py

  # Scrape any Notified/Drupal IR site by URL
  python src/scrape_notified.py --url https://investors.abbvie.com --dry-run

  # Scrape by slug or ticker (looked up in sources.yaml)
  python src/scrape_notified.py --slug abbvie --dry-run
  python src/scrape_notified.py --ticker ABBV --dry-run

  # Override the news-releases listing path and/or starting page number
  # (most sites use /news-releases starting at page 0; some, e.g. Teradyne,
  # use a different path and start at page 1 instead). Normally set once in
  # sources.yaml's news_releases_path / first_page_index fields instead of
  # passing these every time.
  python src/scrape_notified.py --slug teradyne --news-releases-path news-events/press-releases --first-page-index 1 --dry-run

  # Restrict to a year or range
  python src/scrape_notified.py --year 2025 --dry-run
  python src/scrape_notified.py --start-year 2023 --end-year 2025 --dry-run

  # Date range
  python src/scrape_notified.py --since 2024-01-01 --until 2024-12-31 --dry-run

  # Output as JSON
  python src/scrape_notified.py --format json --output out.json --dry-run

  # Save raw HTML of the first page for debugging
  python src/scrape_notified.py --debug-dump-html /tmp/abbvie_p0.html --dry-run

Requires
--------
  pip install curl_cffi beautifulsoup4 lxml ruamel.yaml

  curl_cffi is *required* (not optional).  This site enforces TLS
  fingerprinting and silently drops connections from the standard Python
  requests stack.  curl_cffi impersonates Chrome's JA3/JA4 fingerprint,
  which is the only way to get through.  The script will exit immediately
  with a clear error if curl_cffi is not installed.

Run at most once per day. Requests are spaced by --polite-delay (default 15 s).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

from utils.sources_utils import join_url_path
from utils.scrape_utils import (
    NewsItem as _BaseNewsItem,
    add_common_args,
    add_network_and_debug_args,
    configure_logging,
    dedupe_by_url,
    extract_date_from_detail_html,
    fetch_missing_dates_via_http,
    finalize_and_output,
    parse_time,
    parse_year_args,
)
from utils.scrape_notified_utils import (
    MAX_PAGES,
    extract_date_and_time_from_row,
    fetch_html,
    find_last_page,
    parse_listing_page as _shared_parse_listing_page,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_SLUG = "abbvie"
DEFAULT_TICKER = "ABBV"
DEFAULT_BASE_URL = "https://investors.abbvie.com"

DEFAULT_NEWS_RELEASES_PATH = "news-releases"
# Actual path used for a given source resolves as (highest wins):
#   --news-releases-path CLI flag
#   > sources.yaml "news_releases_path" field for the matched source
#   > DEFAULT_NEWS_RELEASES_PATH
# See resolve_source(). Most Notified/Drupal sites use the default; some
# (e.g. Teradyne, at news-events/press-releases) use a different path.

DEFAULT_FIRST_PAGE_INDEX = 0
# Index of this site's first pagination page (the value in its own ?page=
# scheme, not necessarily 0). Resolves the same way as news_releases_path
# (highest wins):
#   --first-page-index CLI flag
#   > sources.yaml "first_page_index" field for the matched source
#   > DEFAULT_FIRST_PAGE_INDEX
# See resolve_source(). Most Notified/Drupal sites are 0-indexed (their
# first page is ?page=0); some (e.g. Teradyne) are 1-indexed instead
# (?page=0 404s; the first page is ?page=1).

# Regex to identify detail-page hrefs on Notified/Drupal IR sites.
# Matches paths like /news-releases/news-release-details/<slug>
# or /press-releases/<slug> etc.  Deliberately broad: any multi-segment
# path that does NOT look like a bare section landing page.
DETAIL_URL_RE = re.compile(
    r"/(?:news-releases|press-releases|financial-releases)/[^/#?]+/[^/#?]+",
    re.IGNORECASE,
)

logger = logging.getLogger("scrape_notified")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem(_BaseNewsItem):
    """Notified/Drupal IR press-release item.

    Inherits slug, ticker, title, url, publish_date, raw_date_text, and
    publish_date_str from scrape_utils.NewsItem.
    """


# ---------------------------------------------------------------------------
# Date helpers, HTTP session, and pagination helpers
# ---------------------------------------------------------------------------
#
# parse_short_date(), extract_date_and_time_from_row(), get_session()/
# fetch_html(), find_last_page(), and parse_listing_page() (including its
# _row_container()/_find_title_in_container() title-fallback helpers) are
# shared with scrape_notified_gated.py and now live in
# utils/scrape_notified_utils.py (imported above). This script calls
# extract_date_and_time_from_row() with its original behavior (both
# try_long_date_in_cell and try_short_date_in_row left at their default of
# False) -- see that function's docstring for why. It calls the shared
# parse_listing_page() with use_title_fallback=True -- see the thin wrapper
# below.

def is_detail_url(href: str) -> bool:
    """Return True if ``href`` looks like a Notified/Drupal press-release detail URL."""
    return bool(DETAIL_URL_RE.search(href))


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

def listing_page_url(
    base_url: str, page: int = 0, news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH
) -> str:
    """Build a paginated listing URL using Notified/Drupal's ?page= parameter.

    page=0 is the first page (also reachable without the parameter, but
    we always include it for explicitness).

    news_releases_path defaults to "news-releases" but some sites (e.g.
    Teradyne) use "news-events/press-releases" instead; callers resolve
    the right value via resolve_source() / sources.yaml before calling this.
    """
    base = join_url_path(base_url, news_releases_path)
    return base + "?" + urlencode({"page": page})


# ---------------------------------------------------------------------------
# Listing-page parsing
# ---------------------------------------------------------------------------

def parse_listing_page(
    html: str, base_url: str, slug: str, ticker: str
) -> list[NewsItem]:
    """Parse one listing page; return list of NewsItems found.

    Thin wrapper around the shared row-parsing core in
    utils/scrape_notified_utils.py (see that function's docstring for the
    full strategy), shared with scrape_notified_gated.py so a parsing bug
    fix only needs to be made once. use_title_fallback=True enables this
    script's original behavior of digging a real headline out of the
    row/card container (via the shared _row_container()/
    _find_title_in_container() helpers) when the anchor's own text is
    empty or just a generic "Read more" CTA (e.g. Paramount's IR site).
    """
    return _shared_parse_listing_page(
        html, base_url, slug, ticker,
        is_detail_url=is_detail_url,
        news_item_cls=NewsItem,
        use_title_fallback=True,
    )


# ---------------------------------------------------------------------------
# Detail-page date fallback
# ---------------------------------------------------------------------------

def fetch_date_from_detail_page(url: str, timeout: int = 30) -> tuple[Optional[date], str, str]:
    """Fetch a detail page and extract its publish date and time.

    Date-parsing heuristics live in scrape_utils.extract_date_from_detail_html(),
    shared with scrape_investorroom.py. Time extraction is Notified-specific
    (not shared, since it's not known whether other platforms lay out a
    time the same way) so it's a best-effort scan of the same
    article/main/body text region that the shared date heuristic falls back
    to, via scrape_utils.parse_time(). Returns (date, raw_date_text,
    publish_time); publish_time is "" if no time is found (e.g. site doesn't
    publish one).
    """
    try:
        html = fetch_html(url, timeout=timeout)
    except Exception as exc:
        logger.warning("Failed to fetch detail page %s: %s", url, exc)
        return None, "", ""
    d, raw = extract_date_from_detail_html(html)
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup.find("main") or soup.find("body")
    publish_time = parse_time(article.get_text(separator=" ", strip=True)[:2000]) if article else ""
    return d, raw, publish_time


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def page_year_range(
    base_url: str,
    page: int,
    slug: str,
    ticker: str,
    timeout: int,
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
) -> tuple[Optional[int], Optional[int]]:
    """Fetch page ``page`` and return (min_year, max_year) of items on it.

    Returns (None, None) if the page is empty or no dates can be parsed.
    """
    url = listing_page_url(base_url, page=page, news_releases_path=news_releases_path)
    try:
        html = fetch_html(url, timeout=timeout)
    except Exception as exc:
        logger.debug("Failed to probe page %d: %s", page, exc)
        return None, None
    items = parse_listing_page(html, base_url=base_url, slug=slug, ticker=ticker)
    years_on_page = [item.publish_date.year for item in items if item.publish_date]
    if not years_on_page:
        return None, None
    return min(years_on_page), max(years_on_page)


def find_start_page(
    base_url: str,
    slug: str,
    ticker: str,
    last_page: int,
    target_years: set[int],
    timeout: int,
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
    first_page_index: int = DEFAULT_FIRST_PAGE_INDEX,
) -> int:
    """Binary-search for the first page that might contain items from ``target_years``.

    Pages are in reverse-chronological order: page ``first_page_index`` has
    the newest items and page ``last_page`` has the oldest.  We want the
    lowest-numbered page whose date range overlaps the target year range
    (i.e. whose *oldest* item is not yet older than min(target_years)).

    Returns first_page_index if the search fails or the answer is ambiguous.
    """
    min_target = min(target_years)
    max_target = max(target_years)

    lo, hi = first_page_index, last_page
    result = first_page_index  # conservative default: start from the beginning

    # Quick sanity probe: if the first page already only has items older
    # than max_target, there is nothing to fetch at all.
    min_yr, max_yr = page_year_range(
        base_url, first_page_index, slug, ticker, timeout, news_releases_path=news_releases_path
    )
    if max_yr is not None and max_yr < min_target:
        logger.info(
            "Page %d newest item is %d, which is older than target year %d -- nothing to fetch.",
            first_page_index, max_yr, min_target,
        )
        return last_page + 1  # sentinel: nothing to fetch

    while lo < hi:
        mid = (lo + hi) // 2
        min_yr, max_yr = page_year_range(
            base_url, mid, slug, ticker, timeout, news_releases_path=news_releases_path
        )
        logger.debug(
            "Binary search: page %d year range %s–%s (target %d–%d)",
            mid, min_yr, max_yr, min_target, max_target,
        )
        if min_yr is None:
            # Empty / unreadable page -- treat it like we're past the end.
            hi = mid
            continue

        if min_yr > max_target:
            # Entire page is newer than our range; go deeper (higher page numbers).
            lo = mid + 1
            result = mid + 1
        elif max_yr < min_target:
            # Entire page is older than our range; go shallower (lower page numbers).
            hi = mid
            result = mid
        else:
            # Page overlaps our range; it's a candidate -- try to find an even
            # later start by looking shallower.
            result = mid
            hi = mid

    logger.info(
        "Binary search complete: starting scrape from page %d (of %d total, first=%d).",
        result, last_page + 1, first_page_index,
    )
    return result


def scrape_one_pass(
    base_url: str,
    slug: str,
    ticker: str,
    start_page: int,
    polite_delay: float,
    timeout: int,
    debug_dump_html: Optional[Path] = None,
    end_page: Optional[int] = None,
    target_years: Optional[set[int]] = None,
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
) -> list[NewsItem]:
    """Fetch listing pages from ``start_page`` through ``end_page``.

    When ``target_years`` is provided, stops as soon as all items on a page are
    older than the earliest target year (pages are reverse-chronological, so
    once we've gone past our window there is nothing left to find).

    Reads the 'last »' link on the first page to know the total page count.
    Falls back to stopping when a page yields no new items.

    Returns a deduplicated list of NewsItems.
    """
    all_items: list[NewsItem] = []
    seen_urls: set[str] = set()
    last_page: Optional[int] = None  # discovered from first page

    min_target_year = min(target_years) if target_years else None

    stop_at = end_page  # may be refined once we know last_page

    for page_num_offset in range(MAX_PAGES):
        page_idx = start_page + page_num_offset
        if stop_at is not None and page_idx > stop_at:
            logger.info("Reached end page %d. Done.", stop_at)
            break

        url = listing_page_url(base_url, page=page_idx, news_releases_path=news_releases_path)

        logger.info("Fetching listing page %d (page=%d): %s", page_num_offset + 1, page_idx, url)

        try:
            html = fetch_html(url, timeout=timeout)
        except Exception as exc:
            logger.error("Failed to fetch listing page %s: %s", url, exc)
            break

        if debug_dump_html and page_num_offset == 0:
            debug_dump_html.write_text(html, encoding="utf-8")
            logger.info("Saved HTML to %s", debug_dump_html)

        soup = BeautifulSoup(html, "lxml")

        # Discover last page on the first request.
        if last_page is None:
            last_page = find_last_page(soup)
            if last_page is not None:
                logger.info("Last page index: %d (%d total pages)", last_page, last_page + 1)
                if stop_at is None:
                    stop_at = last_page
            else:
                logger.warning("Could not determine last page; will stop on first empty page.")

        page_items = parse_listing_page(html, base_url=base_url, slug=slug, ticker=ticker)

        new_items = [item for item in page_items if item.url.rstrip("/") not in seen_urls]
        for item in new_items:
            seen_urls.add(item.url.rstrip("/"))
        all_items.extend(new_items)

        logger.info(
            "Page %d (page=%d): %d item(s) found, %d new",
            page_num_offset + 1, page_idx, len(page_items), len(new_items),
        )

        # Early exit: if we have a year filter and every dated item on this page
        # is older than the earliest target year, we've passed our window.
        if min_target_year is not None and page_items:
            dated_years = [item.publish_date.year for item in page_items if item.publish_date]
            if dated_years and max(dated_years) < min_target_year:
                logger.info(
                    "Page %d newest item year (%d) is older than target year %d -- stopping early.",
                    page_idx, max(dated_years), min_target_year,
                )
                break

        # Stop conditions.
        if stop_at is not None and page_idx >= stop_at:
            logger.info("Reached last page (page=%d). Done.", stop_at)
            break

        if not page_items:
            logger.info("Empty page at page=%d. Done.", page_idx)
            break

        if not new_items and page_items:
            logger.warning(
                "Page %d (page=%d): all %d item(s) already seen -- stopping to avoid loop.",
                page_num_offset + 1, page_idx, len(page_items),
            )
            break

        time.sleep(polite_delay)

    return all_items




def scrape(
    base_url: str,
    slug: str,
    ticker: str,
    years: Optional[set[int]],
    polite_delay: float,
    timeout: int,
    debug_dump_html: Optional[Path],
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
    first_page_index: int = DEFAULT_FIRST_PAGE_INDEX,
) -> list[NewsItem]:
    """Scrape listing pages, using binary search when a year filter is active.

    Without a year filter, scrapes all pages (pages are in reverse-chronological
    order; the site provides no server-side year parameter).

    With a year filter, binary-searches across page numbers to find the first
    page that overlaps the target years, then walks forward from there and stops
    as soon as a page's newest item is older than the target range.  This avoids
    fetching the entire archive when only recent years are needed.

    Note: the binary search itself fetches O(log N) pages for probing.  Those
    probes are lightweight (parse only, no delay between them) but do count
    against the server.  Total pages fetched = O(log N) probes + K data pages,
    where K is the number of pages that actually contain the target years.

    first_page_index is the site's own starting page number (0 for most
    Notified/Drupal sites; 1 for e.g. Teradyne). All page-number math below
    is relative to it -- nothing assumes the first page is literally 0.
    """
    if years:
        # Step 1: fetch the first page to learn last_page.
        url0 = listing_page_url(
            base_url, page=first_page_index, news_releases_path=news_releases_path
        )
        logger.info("Fetching first page (page=%d) to determine pagination: %s", first_page_index, url0)
        try:
            html0 = fetch_html(url0, timeout=timeout)
        except Exception as exc:
            logger.error("Failed to fetch first page: %s", exc)
            return []

        if debug_dump_html:
            debug_dump_html.write_text(html0, encoding="utf-8")
            logger.info("Saved HTML to %s", debug_dump_html)

        soup0 = BeautifulSoup(html0, "lxml")
        last_page = find_last_page(soup0)

        if last_page is None or last_page == first_page_index:
            logger.warning(
                "Could not determine last page index; falling back to full scan."
            )
            # Fall through to full scan below.
            years = None
        else:
            logger.info(
                "Last page index: %d (%d total pages, first=%d)",
                last_page, last_page - first_page_index + 1, first_page_index,
            )

            # Step 2: binary search for the start page.
            start_page = find_start_page(
                base_url=base_url,
                slug=slug,
                ticker=ticker,
                last_page=last_page,
                target_years=years,
                timeout=timeout,
                news_releases_path=news_releases_path,
                first_page_index=first_page_index,
            )

            if start_page > last_page:
                logger.info("Binary search determined no pages contain target years. Done.")
                return []

            # Step 3: walk forward from start_page, stopping when we pass the window.
            # The first page was already fetched above; if start_page ==
            # first_page_index, scrape_one_pass will simply re-fetch it.
            items = scrape_one_pass(
                base_url=base_url,
                slug=slug,
                ticker=ticker,
                start_page=start_page,
                polite_delay=polite_delay,
                timeout=timeout,
                debug_dump_html=None,  # already dumped above
                end_page=last_page,
                target_years=years,
                news_releases_path=news_releases_path,
            )

            # Global dedup.
            return dedupe_by_url(items)

    # No year filter (or fallback): scrape everything.
    items = scrape_one_pass(
        base_url=base_url,
        slug=slug,
        ticker=ticker,
        start_page=first_page_index,
        polite_delay=polite_delay,
        timeout=timeout,
        debug_dump_html=debug_dump_html,
        news_releases_path=news_releases_path,
    )

    # Global dedup (should already be clean from scrape_one_pass, but be safe).
    return dedupe_by_url(items)


# ---------------------------------------------------------------------------
# Output: daily CSVs
# ---------------------------------------------------------------------------

# CSV/JSON writing and the "Wrote N new + M updated ..." summary line are
# handled by scrape_utils.finalize_and_output(), shared with scrape_q4_ir.py
# and scrape_investorroom.py. Called directly from main() below.


# ---------------------------------------------------------------------------
# Source resolution (sources.yaml integration)
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
    news_releases_path: Optional[str] = None,
    first_page_index: Optional[int] = None,
) -> tuple[str, str, str, str, int]:
    """Resolve (base_url, slug, ticker, news_releases_path, first_page_index)
    from CLI args and sources.yaml.

    base_url is the IR site root (e.g. https://investors.abbvie.com), NOT the
    news-releases listing URL.  Callers append news_releases_path themselves
    via listing_page_url().

    When --url is provided with a path (e.g. https://investors.abbvie.com/news-releases),
    the path is stripped so only the site root is retained, matching the
    convention used by scrape_investorroom.py.

    news_releases_path precedence (highest wins):
      1. the news_releases_path argument (i.e. --news-releases-path on the CLI)
      2. the "news_releases_path" field on the matched sources.yaml record
      3. DEFAULT_NEWS_RELEASES_PATH ("news-releases")

    first_page_index precedence (highest wins). Note 0 is a valid,
    meaningful value here (most sites), so this is resolved with explicit
    "is not None" checks rather than truthiness:
      1. the first_page_index argument (i.e. --first-page-index on the CLI)
      2. the "first_page_index" field on the matched sources.yaml record
      3. DEFAULT_FIRST_PAGE_INDEX (0)
    """
    from utils.sources_utils import resolve_field_precedence, resolve_source_identity

    url, slug, ticker, record = resolve_source_identity(
        url, slug, ticker,
        default_slug=DEFAULT_SLUG, default_ticker=DEFAULT_TICKER, default_url=DEFAULT_BASE_URL,
        strip_url_to_root=True, logger=logger,
    )

    news_releases_path = resolve_field_precedence(
        news_releases_path, record, "news_releases_path", DEFAULT_NEWS_RELEASES_PATH
    )

    # first_page_index precedence: explicit CLI arg > sources.yaml field > default.
    # 0 is a meaningful value, so use "is not None" checks throughout, not truthiness.
    if first_page_index is None:
        record_value = record.get("first_page_index") if record else None
        first_page_index = record_value if record_value is not None else DEFAULT_FIRST_PAGE_INDEX
    first_page_index = int(first_page_index)

    return url, slug, ticker, news_releases_path, first_page_index


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Shared: --url/--slug/--ticker, year/date filters, --format/--output/--dry-run
    add_common_args(parser)

    source = parser.add_argument_group("source")
    source.add_argument(
        "--news-releases-path", default=None, metavar="PATH",
        help=(
            "Listing path appended to the IR site root, e.g. press-releases "
            "(default: news-releases). Overrides sources.yaml's "
            "news_releases_path field for this run; most sites don't need this."
        ),
    )
    source.add_argument(
        "--first-page-index", type=int, default=None, metavar="N",
        help=(
            "Index of this site's first pagination page, i.e. the value used "
            "in its own ?page= parameter (default: 0). Most Notified/Drupal "
            "sites are 0-indexed; some (e.g. Teradyne) are 1-indexed. "
            "Overrides sources.yaml's first_page_index field for this run; "
            "most sites don't need this."
        ),
    )

    detail = parser.add_argument_group("detail-page fetch")
    detail.add_argument(
        "--fetch-detail-pages", action="store_true",
        help=(
            "For items with no date found on the listing page, fetch each "
            "detail page to extract the date."
        ),
    )

    # Shared: --polite-delay/--timeout/--debug-dump-html/--verbose
    add_network_and_debug_args(parser, default_polite_delay=15.0)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    # On Windows, python.exe's console typically defaults to a legacy codepage
    # (e.g. cp1252) rather than UTF-8. Any non-ASCII character reaching stdout/
    # stderr -- e.g. an arrow or em-dash in --help text or a log message --
    # then raises UnicodeEncodeError and crashes before anything is printed.
    # Reconfigure both streams to replace unencodable characters instead of
    # raising, so output degrades gracefully rather than crashing outright.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(errors="replace")
            except Exception:
                pass

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    base_url, slug, ticker, news_releases_path, first_page_index = resolve_source(
        args.url, args.slug, args.ticker, args.news_releases_path, args.first_page_index
    )
    logger.info(
        "Scraping %s (%s) from %s (first page index=%d)",
        slug, ticker, join_url_path(base_url, news_releases_path), first_page_index,
    )

    years = parse_year_args(args)

    all_items = scrape(
        base_url=base_url,
        slug=slug,
        ticker=ticker,
        years=years,
        polite_delay=args.polite_delay,
        timeout=args.timeout,
        debug_dump_html=args.debug_dump_html,
        news_releases_path=news_releases_path,
        first_page_index=first_page_index,
    )
    logger.info("Scraped %d item(s) total (before filtering).", len(all_items))

    if args.fetch_detail_pages:
        fetch_missing_dates_via_http(
            all_items, fetch_date_from_detail_page, args.polite_delay, args.timeout
        )

    # Filters, always previews, and writes CSV/JSON per --format; see
    # finalize_and_output()'s docstring for the three behaviors this
    # standardizes across scrape_notified.py/scrape_investorroom.py/
    # scrape_q4_ir.py (preview-always, --format both, --output default path).
    finalize_and_output(
        all_items,
        years=years, since=args.since, until=args.until, limit=None,
        format=args.format, output=args.output, dry_run=args.dry_run,
        data_dir=DATA_DIR,
        default_json_path=REPO_ROOT / "notified_news.json",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())