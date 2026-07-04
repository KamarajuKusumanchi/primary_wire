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
  {base_url}{news_releases_path}                 (same as ?page=0, the first page)
  {base_url}{news_releases_path}?page=0          first page (explicit)
  {base_url}{news_releases_path}?page=1          second page
  {base_url}{news_releases_path}?page=N          N+1-th page

The last page index can be read from the "last »" pagination link.

There is NO server-side ?year= or ?l= parameter; page size is fixed
server-side (10 items/page for AbbVie).

Press release detail pages:
  {base_url}/news-releases/news-release-details/<slug>

Dates appear in the listing table's first column in M/D/YY format
(e.g. "6/26/26" = June 26, 2026).  Two-digit years are assumed to be
in the 2000s (i.e. "26" → 2026).  Dates are also present verbatim in
each row's summary text (e.g. "NORTH CHICAGO, Ill., June 26, 2026")
which is used as a fallback.

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
  python src/scrape_notified.py --slug teradyne --news-releases-path /news-events/press-releases --first-page-index 1 --dry-run

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
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

try:
    from curl_cffi import requests
    _HTTP_BACKEND = "curl_cffi"
except ImportError:
    sys.exit(
        "Missing dependency: curl_cffi is required (plain requests does not work -- "
        "the server enforces TLS fingerprinting and will reject connections from it).\n"
        "Install with: pip install curl_cffi"
    )

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

from csv_utils import merge_items_into_daily_csvs, print_merge_summary
from sources_utils import join_url_path
from scrape_utils import (
    NewsItem as _BaseNewsItem,
    add_common_args,
    add_network_and_debug_args,
    configure_logging,
    extract_date_from_detail_html,
    fetch_missing_dates_via_http,
    filter_items,
    parse_date,
    parse_year_args,
    print_preview,
    write_json,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_SLUG = "abbvie"
DEFAULT_TICKER = "ABBV"
DEFAULT_BASE_URL = "https://investors.abbvie.com"

DEFAULT_NEWS_RELEASES_PATH = "/news-releases"
# Actual path used for a given source resolves as (highest wins):
#   --news-releases-path CLI flag
#   > sources.yaml "news_releases_path" field for the matched source
#   > DEFAULT_NEWS_RELEASES_PATH
# See resolve_source(). Most Notified/Drupal sites use the default; some
# (e.g. Teradyne, at /news-events/press-releases) use a different path.

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

MAX_PAGES = 100  # safety cap on pagination loops

# Regex to identify detail-page hrefs on Notified/Drupal IR sites.
# Matches paths like /news-releases/news-release-details/<slug>
# or /press-releases/<slug> etc.  Deliberately broad: any multi-segment
# path that does NOT look like a bare section landing page.
DETAIL_URL_RE = re.compile(
    r"/(?:news-releases|press-releases|financial-releases)/[^/#?]+/[^/#?]+",
    re.IGNORECASE,
)

# M/D/YY date format used in the listing table (e.g. "6/26/26", "11/24/25").
# Two-digit years are in the 2000s.
SHORT_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b")

logger = logging.getLogger("scrape_notified")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem(_BaseNewsItem):
    """Notified/Drupal IR press-release item.

    Inherits slug, ticker, title, url, publish_date, raw_date_text, and
    publish_datetime from scrape_utils.NewsItem.
    """


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_short_date(text: str) -> tuple[Optional[date], str]:
    """Parse M/D/YY dates like '6/26/26' or '11/24/25' (2000s assumed).

    Returns (date, raw_match) or (None, '').
    """
    m = SHORT_DATE_RE.search(text)
    if m:
        month, day, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = 2000 + yy
        raw = m.group(0)
        try:
            return date(year, month, day), raw
        except ValueError:
            pass
    return None, ""


def extract_date_from_row(anchor) -> tuple[Optional[date], str]:
    """Extract the publish date for a press-release link on a Notified listing page.

    Strategy 1: The listing table has a Date column as the first <td> in the
    same <tr> as (or an ancestor of) the link.  Walk up to find the <tr> and
    read the first <td>'s text.

    Strategy 2: The row's summary text contains a long-form date like
    "June 26, 2026" -- handed off to scrape_utils.parse_date().

    Strategy 3: Walk up to 5 ancestors scanning all text (same as
    scrape_investorroom's extract_date_near_link).
    """
    # Strategy 1: find the enclosing <tr> and read first <td>
    node = anchor
    for _ in range(10):
        node = node.parent
        if node is None:
            break
        if node.name == "tr":
            first_td = node.find("td")
            if first_td:
                cell_text = first_td.get_text(separator=" ", strip=True)
                d, raw = parse_short_date(cell_text)
                if d:
                    return d, raw
            # Also scan the full row text for long-form dates (Strategy 2)
            row_text = node.get_text(separator=" ", strip=True)
            d, raw = parse_date(row_text)
            if d:
                return d, raw
            break

    # Strategy 3: walk ancestors
    node = anchor
    for _ in range(5):
        parent = node.parent
        if parent is None:
            break
        card_text = parent.get_text(separator=" ", strip=True)
        d, raw = parse_short_date(card_text)
        if d:
            return d, raw
        d, raw = parse_date(card_text)
        if d:
            return d, raw
        node = parent

    return None, ""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_SESSION = None


def get_session():
    """Return a persistent HTTP session.

    Uses curl_cffi to impersonate Chrome's TLS fingerprint (JA3/JA4), which
    is required for Notified/Drupal IR sites that reject the standard Python
    TLS stack.
    """
    global _SESSION
    if _SESSION is None:
        # impersonate="chrome124" sets the TLS fingerprint + HTTP/2 SETTINGS
        # to match a real Chrome 124 client, bypassing TLS-fingerprint blocks.
        _SESSION = requests.Session(impersonate="chrome124")
    return _SESSION


def fetch_html(url: str, timeout: int = 30) -> str:
    """Fetch a URL and return its HTML. Raises on HTTP errors."""
    resp = get_session().get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


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

    news_releases_path defaults to "/news-releases" but some sites (e.g.
    Teradyne) use "/press-releases" instead; callers resolve the right value
    via resolve_source() / sources.yaml before calling this.
    """
    base = join_url_path(base_url, news_releases_path)
    return base + "?" + urlencode({"page": page})


# ---------------------------------------------------------------------------
# Listing-page parsing
# ---------------------------------------------------------------------------

def find_last_page(soup: BeautifulSoup) -> Optional[int]:
    """Read the last page index from the 'last »' pagination link.

    Returns the 0-based page index, or None if not found.
    """
    for a in soup.find_all("a", href=True, title=True):
        title = a.get("title", "").lower()
        if "last page" in title or title == "go to last page":
            href = a["href"]
            m = re.search(r"[?&]page=(\d+)", href)
            if m:
                return int(m.group(1))
    # Fallback: scan all pagination links for the highest ?page= value
    max_page: Optional[int] = None
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m:
            val = int(m.group(1))
            if max_page is None or val > max_page:
                max_page = val
    return max_page


def parse_listing_page(
    html: str, base_url: str, slug: str, ticker: str
) -> list[NewsItem]:
    """Parse one listing page; return list of NewsItems found."""
    # Derive the site root from base_url for urljoin.
    parsed = urlparse(base_url)
    site_root = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href: str = anchor["href"].strip()
        if not is_detail_url(href):
            continue

        full_url = urljoin(site_root, href)
        norm_url = full_url.rstrip("/")
        if norm_url in seen_urls:
            continue

        title = anchor.get_text(separator=" ", strip=True)
        if not title:
            span = anchor.find("span")
            title = span.get_text(strip=True) if span else ""
        if not title:
            logger.debug("Skipping link with no title text: %s", full_url)
            continue

        seen_urls.add(norm_url)

        publish_date, raw_date_text = extract_date_from_row(anchor)

        items.append(NewsItem(
            slug=slug,
            ticker=ticker,
            title=title,
            url=full_url,
            publish_date=publish_date,
            raw_date_text=raw_date_text,
        ))

    return items


# ---------------------------------------------------------------------------
# Detail-page date fallback
# ---------------------------------------------------------------------------

def fetch_date_from_detail_page(url: str, timeout: int = 30) -> tuple[Optional[date], str]:
    """Fetch a detail page and extract its publish date.

    Parsing heuristics live in scrape_utils.extract_date_from_detail_html(),
    shared with scrape_investorroom.py; this function owns only the fetch.
    """
    try:
        html = fetch_html(url, timeout=timeout)
    except Exception as exc:
        logger.warning("Failed to fetch detail page %s: %s", url, exc)
        return None, ""
    return extract_date_from_detail_html(html)


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
            seen: set[str] = set()
            deduped: list[NewsItem] = []
            for item in items:
                k = item.url.rstrip("/")
                if k not in seen:
                    seen.add(k)
                    deduped.append(item)
            return deduped

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
    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in items:
        k = item.url.rstrip("/")
        if k not in seen:
            seen.add(k)
            deduped.append(item)
    return deduped


# ---------------------------------------------------------------------------
# Output: daily CSVs
# ---------------------------------------------------------------------------

# merge_into_daily_csvs() and the CSV-write summary line are handled by
# csv_utils.merge_items_into_daily_csvs() / print_merge_summary(), shared
# with scrape_q4_ir.py and scrape_investorroom.py. Called directly from main().


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
      3. DEFAULT_NEWS_RELEASES_PATH ("/news-releases")

    first_page_index precedence (highest wins). Note 0 is a valid,
    meaningful value here (most sites), so this is resolved with explicit
    "is not None" checks rather than truthiness:
      1. the first_page_index argument (i.e. --first-page-index on the CLI)
      2. the "first_page_index" field on the matched sources.yaml record
      3. DEFAULT_FIRST_PAGE_INDEX (0)
    """
    from sources_utils import resolve_source_identity

    url, slug, ticker, record = resolve_source_identity(
        url, slug, ticker,
        default_slug=DEFAULT_SLUG, default_ticker=DEFAULT_TICKER, default_url=DEFAULT_BASE_URL,
        strip_url_to_root=True, logger=logger,
    )

    # news_releases_path precedence: explicit CLI arg > sources.yaml field > default.
    if not news_releases_path:
        news_releases_path = (record.get("news_releases_path") if record else None) or (
            DEFAULT_NEWS_RELEASES_PATH
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
            "Listing path appended to the IR site root, e.g. /press-releases "
            "(default: /news-releases). Overrides sources.yaml's "
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
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    if args.format == "json" and args.output is None:
        parser.error("--output PATH is required when --format json")

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

    filtered = filter_items(
        all_items, years=years, since=args.since, until=args.until, limit=None
    )
    logger.info("%d item(s) after filtering.", len(filtered))

    if args.dry_run:
        print_preview(filtered)

    if args.format == "json":
        write_json(filtered, args.output, dry_run=args.dry_run)
    else:
        summary = merge_items_into_daily_csvs(filtered, DATA_DIR, args.dry_run)
        print_merge_summary(summary, args.dry_run, filtered)

    return 0


if __name__ == "__main__":
    sys.exit(main())