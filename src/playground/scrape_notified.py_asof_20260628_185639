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
  {listing_url}                 (same as ?page=0, the first page)
  {listing_url}?page=0          first page (explicit)
  {listing_url}?page=1          second page
  {listing_url}?page=N          N+1-th page

The last page index can be read from the "last »" pagination link.

There is NO server-side ?year= or ?l= parameter; page size is fixed
server-side (10 items/page for AbbVie).

Press release detail pages:
  {ir_base}/news-releases/news-release-details/<slug>

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
  python src/scrape_notified.py --url https://investors.abbvie.com/news-releases --dry-run

  # Scrape by slug or ticker (looked up in sources.yaml)
  python src/scrape_notified.py --slug abbvie --dry-run
  python src/scrape_notified.py --ticker ABBV --dry-run

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

  curl_cffi is strongly preferred over plain requests for this platform:
  it impersonates Chrome's TLS fingerprint, which this site requires.
  If curl_cffi is unavailable the script falls back to requests, but
  connections will likely time out.

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
    try:
        import requests  # type: ignore[no-redef]
        _HTTP_BACKEND = "requests"
    except ImportError:
        sys.exit("Missing dependency. Install with: pip install curl_cffi")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

from csv_utils import merge_into_daily_csvs as _csv_merge_into_daily_csvs
from scrape_utils import (
    NewsItem as _BaseNewsItem,
    add_common_args,
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
DEFAULT_LISTING_URL = "https://investors.abbvie.com/news-releases"

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

    When curl_cffi is available the session impersonates Chrome at the TLS
    level (JA3/JA4 fingerprint), which is necessary for sites that silently
    drop connections from the standard Python TLS stack.  Falls back to a
    plain requests.Session with browser-like headers when curl_cffi is absent.
    """
    global _SESSION
    if _SESSION is None:
        if _HTTP_BACKEND == "curl_cffi":
            # impersonate="chrome124" sets the TLS fingerprint + HTTP/2 SETTINGS
            # to match a real Chrome 124 client, bypassing TLS-fingerprint blocks.
            _SESSION = requests.Session(impersonate="chrome124")
        else:
            _SESSION = requests.Session()
            _SESSION.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })
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

def listing_page_url(listing_url: str, page: int = 0) -> str:
    """Build a paginated listing URL using Notified/Drupal's ?page= parameter.

    page=0 is the first page (also reachable without the parameter, but
    we always include it for explicitness).
    """
    base = listing_url.rstrip("/")
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
    html: str, listing_url: str, slug: str, ticker: str
) -> list[NewsItem]:
    """Parse one listing page; return list of NewsItems found."""
    # Derive the site root from the listing URL for urljoin.
    parsed = urlparse(listing_url)
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

    Notified/Drupal detail pages typically have a <time> tag or a date in
    the article header.
    """
    try:
        html = fetch_html(url, timeout=timeout)
    except Exception as exc:
        logger.warning("Failed to fetch detail page %s: %s", url, exc)
        return None, ""

    soup = BeautifulSoup(html, "lxml")

    # Priority 1: <time datetime="...">
    for time_tag in soup.find_all("time"):
        dt_attr = time_tag.get("datetime", "")
        if dt_attr:
            d, raw = parse_date(dt_attr)
            if d:
                return d, raw
        d, raw = parse_date(time_tag.get_text(strip=True))
        if d:
            return d, raw

    # Priority 2: common date CSS selectors
    date_selectors = [
        "span.date", "p.date", "div.date",
        ".press-release-date", ".release-date", ".article-date",
        ".news-date", ".pr-date", ".date-label",
        "[class*='date']",
    ]
    for sel in date_selectors:
        el = soup.select_one(sel)
        if el:
            d, raw = parse_date(el.get_text(strip=True))
            if d:
                return d, raw

    # Priority 3: first 2000 chars of article/main body
    article = soup.find("article") or soup.find("main") or soup.find("body")
    if article:
        text = article.get_text(separator=" ", strip=True)[:2000]
        d, raw = parse_date(text)
        if d:
            return d, raw

    return None, ""


def fetch_missing_dates(items: list[NewsItem], polite_delay: float, timeout: int) -> None:
    """Mutate ``items`` in-place: fetch detail pages for items with no date."""
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
# Scraping
# ---------------------------------------------------------------------------

def scrape_one_pass(
    listing_url: str,
    slug: str,
    ticker: str,
    start_page: int,
    polite_delay: float,
    timeout: int,
    debug_dump_html: Optional[Path] = None,
) -> list[NewsItem]:
    """Fetch all listing pages starting from ``start_page``, walking ?page=N.

    Reads the 'last »' link on the first page to know the total page count,
    then iterates page-by-page.  Falls back to stopping when a page yields
    no new items.

    Returns a deduplicated list of NewsItems.
    """
    all_items: list[NewsItem] = []
    seen_urls: set[str] = set()
    last_page: Optional[int] = None  # discovered from first page

    for page_num_offset in range(MAX_PAGES):
        page_idx = start_page + page_num_offset
        url = listing_page_url(listing_url, page=page_idx)

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
            else:
                logger.warning("Could not determine last page; will stop on first empty page.")

        page_items = parse_listing_page(html, listing_url=listing_url, slug=slug, ticker=ticker)

        new_items = [item for item in page_items if item.url.rstrip("/") not in seen_urls]
        for item in new_items:
            seen_urls.add(item.url.rstrip("/"))
        all_items.extend(new_items)

        logger.info(
            "Page %d (page=%d): %d item(s) found, %d new",
            page_num_offset + 1, page_idx, len(page_items), len(new_items),
        )

        # Stop conditions.
        if last_page is not None and page_idx >= last_page:
            logger.info("Reached last page (page=%d). Done.", last_page)
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
    listing_url: str,
    slug: str,
    ticker: str,
    years: Optional[set[int]],
    polite_delay: float,
    timeout: int,
    debug_dump_html: Optional[Path],
) -> list[NewsItem]:
    """Scrape all pages.

    Unlike InvestorRoom, Notified/Drupal does NOT expose a server-side year
    filter in the URL, so we always scrape all pages and filter client-side.
    This is slightly wasteful but correct, and mirrors how the site works.
    """
    items = scrape_one_pass(
        listing_url=listing_url,
        slug=slug,
        ticker=ticker,
        start_page=0,
        polite_delay=polite_delay,
        timeout=timeout,
        debug_dump_html=debug_dump_html,
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

def merge_into_daily_csvs(
    items: list[NewsItem], dry_run: bool, data_dir: Path = DATA_DIR
) -> dict:
    """Merge scraped items into per-date CSV files under data_dir."""
    dated = [item for item in items if item.publish_date is not None]
    undated = [item for item in items if item.publish_date is None]

    rows_by_date: dict[date, list[dict]] = {}
    for item in dated:
        rows_by_date.setdefault(item.publish_date, []).append(item.to_row())

    summary = _csv_merge_into_daily_csvs(rows_by_date, data_dir, dry_run)
    summary["undated"] = len(undated)

    if undated:
        logger.warning(
            "%d item(s) had no resolvable publish date and were NOT written. "
            "Re-run with --fetch-detail-pages to attempt resolution.",
            len(undated),
        )
        for item in undated:
            logger.warning("  UNDATED: %s | %s", item.title, item.url)

    return summary


# ---------------------------------------------------------------------------
# Source resolution (sources.yaml integration)
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
) -> tuple[str, str, str]:
    """Resolve (listing_url, slug, ticker) from CLI args and sources.yaml.

    Returns (listing_url, slug, ticker).  listing_url is the full listing
    page URL (e.g. https://investors.abbvie.com/financial-releases), not just
    the site root.  Unlike scrape_investorroom.py, this scraper takes the
    listing URL directly because the path varies by company
    (e.g. /financial-releases vs /news-releases).
    """
    try:
        from sources_utils import find_source, find_source_by_ir_url, load_sources
        sources = load_sources()
    except Exception as exc:
        logger.warning("Could not load sources.yaml (%s); slug/ticker lookup disabled.", exc)
        sources = []

    url = url or ""
    slug = slug or ""
    ticker = ticker or ""

    if slug or ticker:
        query = slug or ticker
        record = find_source(sources, query) if sources else None
        if record is None:
            logger.warning(
                "No sources.yaml record found for '%s'. Using provided values as-is.", query
            )
        else:
            slug = slug or record.get("slug", "")
            ticker = ticker or record.get("ticker", "")
            if not url:
                url = record.get("ir_url", "").rstrip("/")
    elif url:
        # Keep the full listing URL (including path) -- don't strip to root.
        record = None
        parsed = urlparse(url)
        site_root = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        if sources:
            try:
                from sources_utils import find_source_by_ir_url
                record = find_source_by_ir_url(sources, site_root)
            except Exception:
                pass
        if record is None:
            logger.warning(
                "No sources.yaml record matched the host of '%s'. "
                "Slug and ticker will be empty.", url
            )
        else:
            slug = record.get("slug", "")
            ticker = record.get("ticker", "")
    else:
        slug, ticker, url = DEFAULT_SLUG, DEFAULT_TICKER, DEFAULT_LISTING_URL

    if not slug:
        logger.warning("Slug is empty; CSV rows will have an empty slug column.")
    if not ticker:
        logger.warning("Ticker is empty; CSV rows will have an empty ticker column.")

    return url, slug, ticker


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

    detail = parser.add_argument_group("detail-page fetch")
    detail.add_argument(
        "--fetch-detail-pages", action="store_true",
        help=(
            "For items with no date found on the listing page, fetch each "
            "detail page to extract the date."
        ),
    )

    network = parser.add_argument_group("network")
    network.add_argument(
        "--polite-delay", type=float, default=15.0, metavar="SECONDS",
        help="Seconds between requests (default: 15).",
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

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.format == "json" and args.output is None:
        parser.error("--output PATH is required when --format json")

    listing_url, slug, ticker = resolve_source(args.url, args.slug, args.ticker)
    logger.info("Scraping %s (%s) from %s", slug, ticker, listing_url)

    years = parse_year_args(args)

    all_items = scrape(
        listing_url=listing_url,
        slug=slug,
        ticker=ticker,
        years=years,
        polite_delay=args.polite_delay,
        timeout=args.timeout,
        debug_dump_html=args.debug_dump_html,
    )
    logger.info("Scraped %d item(s) total (before filtering).", len(all_items))

    if args.fetch_detail_pages:
        fetch_missing_dates(all_items, polite_delay=args.polite_delay, timeout=args.timeout)

    filtered = filter_items(
        all_items, years=years, since=args.since, until=args.until, limit=None
    )
    logger.info("%d item(s) after filtering.", len(filtered))

    if args.dry_run:
        print_preview(filtered)

    if args.format == "json":
        write_json(filtered, args.output, dry_run=args.dry_run)
    else:
        summary = merge_into_daily_csvs(filtered, dry_run=args.dry_run)
        undated_note = (
            f" ({summary['undated']} undated item(s) skipped)" if summary["undated"] else ""
        )
        action = "Would write" if args.dry_run else "Wrote"
        dated_count = (
            summary["files_written"] if not args.dry_run
            else len({i.publish_date for i in filtered if i.publish_date})
        )
        print(
            f"{action} {summary['rows_added']} new + {summary['rows_updated']} updated row(s) "
            f"across {dated_count} daily CSV file(s){undated_note}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())