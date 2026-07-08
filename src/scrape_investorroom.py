#!/usr/bin/env python3
"""
scrape_investorroom.py

Scrape press-release listings from any IR site powered by the InvestorRoom
platform (sold by Notified, formerly Intrado/West) and merge them into
primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

InvestorRoom is a server-side-rendered IR platform used by a large number of
S&P 500 companies. Unlike Q4 Inc. sites (which require a headless browser),
these pages return full HTML to a plain HTTP request, so no Playwright is needed.

Platform fingerprints
---------------------
You can identify an InvestorRoom site by any of:

  * The listing URL ends with /news-releases (no .aspx extension)
  * Detail pages use ?item=NNNNN  OR  a date-prefixed slug
    e.g. /2025-10-29-chipotle-announces-q3-results
  * Static assets / PDFs served from filecache.investorroom.com
  * Page footer or source contains "investorroom" or "Notified"

URL structure
-------------
Listing page (paginated by offset):
  {ir_base}/news-releases
  {ir_base}/news-releases?l=100          (100 items per page)
  {ir_base}/news-releases?l=100&o=100    (next page)

  Parameters:
    ?l=<limit>   Number of listings per page (server default is 5; use 100)
    ?o=<offset>  Skip this many items (NOT a page number)

  Note: ?p=2 is NOT supported by InvestorRoom sites.

Press release detail pages come in two styles:
  Style A (legacy):  {ir_base}/news-releases?item=122457
  Style B (modern):  {ir_base}/2025-10-29-chipotle-announces-q3-results

Both styles are handled. Date extraction:

  1. Listing-page parse (zero extra requests): InvestorRoom listing pages
     include the date near each link in the card HTML. Style B URLs also
     embed the date in the URL slug.

  2. Detail-page fallback (opt-in via --fetch-detail-pages): for Style A
     items where no date was found on the listing page, fetch the detail page
     and extract the date from the article header.

Usage
-----
  # Default: scrape Chipotle, dry-run (no files written)
  python src/scrape_investorroom.py --dry-run

  # Write real data for Chipotle
  python src/scrape_investorroom.py

  # Scrape any InvestorRoom site by slug or ticker
  python src/scrape_investorroom.py --slug chipotle --dry-run
  python src/scrape_investorroom.py --ticker CMG --dry-run

  # Scrape by URL directly
  python src/scrape_investorroom.py --url https://ir.chipotle.com/news-releases --dry-run

  # Override the news-releases listing path for an InvestorRoom site that
  # doesn't use the default (rare -- most InvestorRoom sites use
  # news-releases). Normally set once in sources.yaml's news_releases_path
  # field instead of passing this every time.
  python src/scrape_investorroom.py --slug SLUG --news-releases-path press-releases --dry-run

  # Restrict to a year or range
  python src/scrape_investorroom.py --year 2025 --dry-run
  python src/scrape_investorroom.py --start-year 2023 --end-year 2025 --dry-run

  # Date range
  python src/scrape_investorroom.py --since 2024-01-01 --until 2024-12-31 --dry-run

  # Control items per listing page (default 100; server default without ?l= is 5)
  python src/scrape_investorroom.py --page-limit 50 --dry-run

  # Fetch detail pages to resolve missing dates
  python src/scrape_investorroom.py --fetch-detail-pages --dry-run

  # Output as JSON
  python src/scrape_investorroom.py --format json --output out.json --dry-run

  # Save raw HTML for debugging
  python src/scrape_investorroom.py --debug-dump-html /tmp/chipotle.html --dry-run

Requires
--------
  pip install requests beautifulsoup4 lxml ruamel.yaml

Run at most once per day. Requests are spaced by --polite-delay (default 15 s).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urljoin

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Install with: pip install requests")

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
    parse_date,
    parse_year_args,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_SLUG = "chipotle"
DEFAULT_TICKER = "CMG"
DEFAULT_BASE_URL = "https://ir.chipotle.com"

DEFAULT_NEWS_RELEASES_PATH = "news-releases"
# Actual path used for a given source resolves as (highest wins):
#   --news-releases-path CLI flag
#   > sources.yaml "news_releases_path" field for the matched source
#   > DEFAULT_NEWS_RELEASES_PATH
# See resolve_source(). All InvestorRoom sites currently in sources.yaml use
# the default; this exists for when a future one doesn't.

DEFAULT_PAGE_LIMIT = 100  # ?l=100 -- 100 items per page vs server default of 5
MAX_PAGES = 50            # safety cap on pagination loops

# Regex patterns to identify InvestorRoom detail-page URLs.
DETAIL_URL_LEGACY_RE = re.compile(r"[?&]item=\d+", re.IGNORECASE)
# Excludes fragment URLs (e.g. /2026-01-12-TITLE#assets_...) -- those are photo
# gallery anchors on the same page, not press-release detail pages.
DETAIL_URL_MODERN_RE = re.compile(r"/\d{4}-\d{2}-\d{2}-[^/#]+/?$", re.IGNORECASE)

logger = logging.getLogger("scrape_investorroom")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem(_BaseNewsItem):
    """InvestorRoom press-release item.

    Inherits slug, ticker, title, url, publish_date, raw_date_text, and
    publish_date_str from scrape_utils.NewsItem.  No extra fields needed for
    this platform; subclassing keeps isinstance() checks consistent and leaves
    room for future additions without touching shared code.
    """


# ---------------------------------------------------------------------------
# Date helpers (platform-specific)
# ---------------------------------------------------------------------------

def date_from_url(url: str) -> Optional[date]:
    """Extract a publish date from a modern InvestorRoom URL like /2025-10-29-title."""
    m = re.search(r"/(\d{4}-\d{2}-\d{2})-", url)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_SESSION: Optional[requests.Session] = None


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
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
    """Return True if ``href`` looks like an InvestorRoom press-release detail URL."""
    return bool(DETAIL_URL_LEGACY_RE.search(href) or DETAIL_URL_MODERN_RE.search(href))


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

def listing_page_url(
    base_url: str,
    offset: int = 0,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
) -> str:
    """Build a paginated listing URL using InvestorRoom's ?l= / ?o= parameters.

    ?l=<limit>   items per page (server default is 5 when omitted; always pass explicitly)
    ?o=<offset>  skip this many items (0-based; omit on the first page)

    news_releases_path defaults to "news-releases"; callers resolve the
    right value via resolve_source() / sources.yaml before calling this.
    """
    base = join_url_path(base_url, news_releases_path)
    params: dict[str, int] = {"l": page_limit}
    if offset > 0:
        params["o"] = offset
    return base + "?" + urlencode(params)


def year_filter_url(
    base_url: str,
    year: int,
    offset: int = 0,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
) -> str:
    """Build a year-filtered listing URL. See listing_page_url() for news_releases_path."""
    base = join_url_path(base_url, news_releases_path)
    params: dict[str, object] = {"year": year, "l": page_limit}
    if offset > 0:
        params["o"] = offset
    return base + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# Listing-page parsing
# ---------------------------------------------------------------------------

def extract_date_near_link(anchor) -> tuple[Optional[date], str]:
    """Walk up to 5 ancestor elements of ``anchor`` looking for a date in text."""
    node = anchor
    for _ in range(5):
        parent = node.parent
        if parent is None:
            break
        card_text = parent.get_text(separator=" ", strip=True)
        d, raw = parse_date(card_text)
        if d:
            return d, raw
        node = parent
    return None, ""


def find_next_page_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """Find the 'Next' pagination link in a parsed listing page.

    Reading the href directly is more reliable than constructing it ourselves
    because the page size (?l=) may vary by site or theme.
    """
    for candidate in soup.find_all("a", href=True):
        text = candidate.get_text(strip=True).lower()
        aria = (candidate.get("aria-label") or "").lower()
        rel = " ".join(candidate.get("rel") or []).lower()
        is_next = (
            text in ("next", "›", "»", "next »", "next›")
            or "next" in aria
            or "next" in rel
        )
        if not is_next:
            continue
        href = candidate["href"].strip()
        if href and href not in ("#", "javascript:void(0)", "javascript:;"):
            url = urljoin(base_url, href)
            logger.debug("Next page link: %s", url)
            return url
    return None


def parse_listing_page(
    html: str, base_url: str, slug: str, ticker: str
) -> tuple[list[NewsItem], Optional[str]]:
    """Parse one listing page.

    Returns (items, next_page_url).
    next_page_url is the absolute URL for the next page, or None on the last page.
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href: str = anchor["href"].strip()
        if not is_detail_url(href):
            continue

        full_url = urljoin(base_url, href)
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

        # Strategy 1: date embedded in modern URL slug.
        publish_date = date_from_url(href)
        raw_date_text = publish_date.isoformat() if publish_date else ""

        # Strategy 2: date in the surrounding card HTML.
        if publish_date is None:
            publish_date, raw_date_text = extract_date_near_link(anchor)

        items.append(NewsItem(
            slug=slug,
            ticker=ticker,
            title=title,
            url=full_url,
            publish_date=publish_date,
            raw_date_text=raw_date_text,
        ))

    next_url = find_next_page_url(soup, base_url)
    return items, next_url


# ---------------------------------------------------------------------------
# Detail-page date fallback
# ---------------------------------------------------------------------------

def fetch_date_from_detail_page(url: str, timeout: int = 30) -> tuple[Optional[date], str]:
    """Fetch a detail page and extract its publish date.

    Parsing heuristics live in scrape_utils.extract_date_from_detail_html(),
    shared with scrape_notified.py; this function owns only the fetch.
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

def scrape_one_pass(
    base_url: str,
    slug: str,
    ticker: str,
    start_url: str,
    polite_delay: float,
    timeout: int,
    debug_dump_html: Optional[Path] = None,
) -> list[NewsItem]:
    """Fetch all listing pages starting from ``start_url``, following Next links.

    Returns a deduplicated list of NewsItems.
    """
    next_url: Optional[str] = start_url
    all_items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = next_url
        logger.info("Fetching listing page %d: %s", page_num, url)

        try:
            html = fetch_html(url, timeout=timeout)
        except Exception as exc:
            logger.error("Failed to fetch listing page %s: %s", url, exc)
            break

        if debug_dump_html and page_num == 1:
            debug_dump_html.write_text(html, encoding="utf-8")
            logger.info("Saved HTML to %s", debug_dump_html)

        page_items, next_url = parse_listing_page(html, base_url=base_url, slug=slug, ticker=ticker)

        new_items = [
            item for item in page_items
            if item.url.rstrip("/") not in seen_urls
        ]
        for item in new_items:
            seen_urls.add(item.url.rstrip("/"))
        all_items.extend(new_items)

        logger.info(
            "Page %d: %d item(s) found, %d new%s",
            page_num, len(page_items), len(new_items),
            f"; next → {next_url}" if next_url else " [last page]",
        )

        if not new_items and page_items:
            logger.warning(
                "Page %d: all %d item(s) already seen -- stopping to avoid loop.",
                page_num, len(page_items),
            )
            break

        if not next_url:
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
    page_limit: int,
    debug_dump_html: Optional[Path],
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
) -> list[NewsItem]:
    """Scrape all years (or the default all-years view).

    When years are specified, one pass per year is made using the ?year= filter.
    Results are globally deduplicated before returning.
    """
    years_to_visit: list[Optional[int]] = sorted(years) if years else [None]
    all_items: list[NewsItem] = []

    for i, year in enumerate(years_to_visit):
        if i > 0:
            time.sleep(polite_delay)

        if year is not None:
            start_url = year_filter_url(
                base_url, year, page_limit=page_limit, news_releases_path=news_releases_path
            )
        else:
            start_url = listing_page_url(
                base_url, page_limit=page_limit, news_releases_path=news_releases_path
            )

        dump_path = debug_dump_html
        if dump_path and len(years_to_visit) > 1 and year is not None:
            dump_path = dump_path.with_name(f"{dump_path.stem}_{year}{dump_path.suffix}")

        items = scrape_one_pass(
            base_url=base_url,
            slug=slug,
            ticker=ticker,
            start_url=start_url,
            polite_delay=polite_delay,
            timeout=timeout,
            debug_dump_html=dump_path,
        )
        all_items.extend(items)

    # Global dedup across year passes.
    return dedupe_by_url(all_items)


# ---------------------------------------------------------------------------
# Output: daily CSVs
# ---------------------------------------------------------------------------

# CSV/JSON writing and the "Wrote N new + M updated ..." summary line are
# handled by scrape_utils.finalize_and_output(), shared with scrape_q4_ir.py
# and scrape_notified.py. Called directly from main() below.


# ---------------------------------------------------------------------------
# Source resolution (sources.yaml integration)
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
    news_releases_path: Optional[str] = None,
) -> tuple[str, str, str, str]:
    """Resolve (base_url, slug, ticker, news_releases_path) from CLI args and sources.yaml.

    Returns (base_url, slug, ticker, news_releases_path).  base_url is the IR
    site root (e.g. https://ir.chipotle.com), NOT the news-releases listing
    URL.  Callers append news_releases_path themselves via listing_page_url()
    / year_filter_url().

    news_releases_path precedence (highest wins):
      1. the news_releases_path argument (i.e. --news-releases-path on the CLI)
      2. the "news_releases_path" field on the matched sources.yaml record
      3. DEFAULT_NEWS_RELEASES_PATH ("news-releases")
    """
    from utils.sources_utils import resolve_source_identity

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

    return url, slug, ticker, news_releases_path


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

    detail = parser.add_argument_group("detail-page fetch")
    detail.add_argument(
        "--fetch-detail-pages", action="store_true",
        help=(
            "For items with no date found on the listing page, fetch each detail "
            "page to extract the date. Useful for legacy ?item=NNN URLs."
        ),
    )

    # Shared: --polite-delay/--timeout/--debug-dump-html/--verbose
    network = add_network_and_debug_args(parser, default_polite_delay=15.0)
    network.add_argument(
        "--page-limit", type=int, default=DEFAULT_PAGE_LIMIT, metavar="N",
        dest="page_limit",
        help=(
            f"Items per listing page via ?l= (default: {DEFAULT_PAGE_LIMIT}). "
            "The server default without ?l= is 5, which causes many more requests."
        ),
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    base_url, slug, ticker, news_releases_path = resolve_source(
        args.url, args.slug, args.ticker, args.news_releases_path
    )
    logger.info("Scraping %s (%s) from %s", slug, ticker, join_url_path(base_url, news_releases_path))

    years = parse_year_args(args)

    all_items = scrape(
        base_url=base_url,
        slug=slug,
        ticker=ticker,
        years=years,
        polite_delay=args.polite_delay,
        timeout=args.timeout,
        page_limit=args.page_limit,
        debug_dump_html=args.debug_dump_html,
        news_releases_path=news_releases_path,
    )
    logger.info("Scraped %d item(s) total (before filtering).", len(all_items))

    if args.fetch_detail_pages:
        fetch_missing_dates_via_http(
            all_items, fetch_date_from_detail_page, args.polite_delay, args.timeout
        )

    # Filters, always previews, and writes CSV/JSON per --format; see
    # finalize_and_output()'s docstring for the three behaviors this
    # standardizes across scrape_investorroom.py/scrape_notified.py/
    # scrape_q4_ir.py (preview-always, --format both, --output default path).
    finalize_and_output(
        all_items,
        years=years, since=args.since, until=args.until, limit=None,
        format=args.format, output=args.output, dry_run=args.dry_run,
        data_dir=DATA_DIR,
        default_json_path=REPO_ROOT / "investorroom_news.json",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())