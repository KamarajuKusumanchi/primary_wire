#!/usr/bin/env python3
"""
scrape_investis.py

Scrape press-release listings from any IR site powered by the Investis
Digital platform and merge them into primary_wire's daily
data/YYYY/YYYY-MM-DD.csv files. Home Depot is the first confirmed site
("Delivered by Investis Digital" in the page footer).

Platform fingerprints
---------------------
You can identify an Investis Digital site by any of:

  * Page footer reads "Delivered by Investis Digital"
    (linking to https://www.investisdigital.com)
  * The listing URL is /news-releases/<YYYY> -- the year is part of the
    PATH, not a query parameter or a client-side filter
  * Pagination uses ?page=N, 1-based (no ?page= at all means page 1)
  * Detail pages use paths like:
      /news-releases/<YYYY>/<MM-DD-YYYY>-<digits>
    e.g. /news-releases/2025/12-10-2025-140135946
  * Dates in the listing table appear as plain "Mon DD, YYYY" text
    (e.g. "Dec 10, 2025") in the row's Date column

URL structure
-------------
Listing page (one page per calendar year, paginated by page number):
  {base_url}/{news_releases_path}/{year}                first page
  {base_url}/{news_releases_path}/{year}?page=2          second page
  {base_url}/{news_releases_path}/{year}?page=N          N-th page

There is no site-wide listing that spans every year at once -- unlike
Notified/InvestorRoom, where one paginated feed holds the whole history,
here each year is its own address. That actually makes this platform
*simpler* to scrape correctly: no year filter to reverse-engineer, no
binary search across pages needed to jump to a target year. The scraper
just requests the exact year(s) it wants directly.

Older years (observed on Home Depot back to 2006) are not linked from the
year dropdown itself but are still reachable at the very same
/news-releases/<YYYY> address -- the dropdown just links to a "/news-releases/
archive" page which lists them out. A calendar year with zero press releases
(e.g. before the company existed, or before this site had press releases)
renders the same page shell with an empty table ("Information will appear
here in due course."), which is what this scraper uses to detect the end of
a full-history scan -- see scrape() below.

Is scraping this "Load More" listing instead of clicking through it by hand
a good idea? Yes, and more confidently so than for most IR platforms: the
"Load More News Releases" button seen in a browser is just a progressive-
enhancement affordance over an ordinary server-rendered ?page=N pagination
scheme that already works with a plain GET request (confirmed by fetching
.../news-releases/2025?page=4 directly and getting back full, correctly-
paginated HTML, pager state included). No JavaScript execution or headless
browser is required at all -- unlike Q4 Inc. sites (scrape_q4_ir.py), which
render client-side and need Playwright.

Usage
-----
  # Default: scrape Home Depot's entire press-release history, dry-run
  python src/scrape_investis.py --dry-run

  # Write real data for Home Depot
  python src/scrape_investis.py

  # Scrape any Investis Digital IR site by URL, slug, or ticker
  python src/scrape_investis.py --url https://ir.homedepot.com --dry-run
  python src/scrape_investis.py --slug home-depot --dry-run
  python src/scrape_investis.py --ticker HD --dry-run

  # Restrict to a year or range (skips the full-history scan entirely --
  # only the requested year(s)' pages are fetched)
  python src/scrape_investis.py --year 2025 --dry-run
  python src/scrape_investis.py --start-year 2023 --end-year 2025 --dry-run

  # Date range
  python src/scrape_investis.py --since 2024-01-01 --until 2024-12-31 --dry-run

  # Override the news-releases listing path (rare; most Investis sites use
  # the default "news-releases"). Normally set once in sources.yaml's
  # news_releases_path field instead of passing this every time.
  python src/scrape_investis.py --slug home-depot --news-releases-path press-releases --dry-run

  # Fetch detail pages to resolve any dates the listing page couldn't parse
  python src/scrape_investis.py --fetch-detail-pages --dry-run

  # Output as JSON
  python src/scrape_investis.py --format json --output out.json --dry-run

  # Save raw HTML of the first fetched page for debugging
  python src/scrape_investis.py --debug-dump-html /tmp/homedepot_2025_p1.html --dry-run

Requires
--------
  pip install curl_cffi beautifulsoup4 lxml ruamel.yaml

  curl_cffi is used (rather than plain ``requests``) as a precaution: the
  site sits behind Cloudflare (visible via its cdn-cgi email-obfuscation
  markup), and Cloudflare deployments sometimes reject the plain Python
  requests/TLS stack the way Notified/Drupal sites are confirmed to (see
  scrape_notified.py). That has NOT been independently confirmed for
  ir.homedepot.com specifically -- unlike scrape_notified.py's AbbVie
  default, this is a defensive choice, not a verified requirement. If it
  turns out plain ``requests`` works fine here too, get_session() below is
  the only place that would need to change.

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
from urllib.parse import urljoin, urlparse, urlunparse

try:
    from curl_cffi import requests
except ImportError:
    sys.exit(
        "Missing dependency: curl_cffi.\nInstall with: pip install curl_cffi\n"
        "(See this module's docstring for why curl_cffi is used here.)"
    )

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

from utils.sources_utils import (
    INVESTIS_DEFAULT_NEWS_RELEASES_PATH,
    join_url_path,
    resolve_field_precedence,
    resolve_source_identity,
)
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

DEFAULT_SLUG = "home-depot"
DEFAULT_TICKER = "HD"
DEFAULT_BASE_URL = "https://ir.homedepot.com"

# Imported from utils.sources_utils.PLATFORMS["investis"] rather than
# defined here, so this scraper and reporting scripts (detect_ir_platform.py,
# check_scraper_coverage.py) can never disagree about Investis's default
# listing path -- see that registry's own comment for why it's the single
# source of truth. Only one Investis Digital site (Home Depot) is known to
# primary_wire so far; the registry entry is written generically enough to
# cover a second one without needing to change.
DEFAULT_NEWS_RELEASES_PATH = INVESTIS_DEFAULT_NEWS_RELEASES_PATH

# Safety caps on pagination/full-history loops -- these stop the scraper
# from looping forever if the site's markup changes in a way that breaks
# our "is there a next page?" / "is this year empty?" detection.
MAX_PAGES_PER_YEAR = 50
MAX_YEARS_BACK = 60  # from the current year, for a full-history scan

# Detail-page URL: /news-releases/<YYYY>/<slug>, e.g.
#   /news-releases/2025/12-10-2025-140135946
# Requires a 4-digit year segment followed by a second, non-empty segment,
# so a bare year-listing nav link (e.g. "/news-releases/2025" with nothing
# after it) is never mistaken for a press release.
DETAIL_URL_RE = re.compile(
    r"/(?:news-releases|press-releases)/\d{4}/[^/?#]+/?$", re.IGNORECASE
)

# The detail-page slug itself embeds the publish date as MM-DD-YYYY right
# after the year segment, e.g. ".../2025/12-10-2025-140135946" -> Dec 10, 2025.
# Used only as a zero-request fallback when the listing table's own Date
# column can't be parsed (see date_from_url() below) -- the listing-page
# date is authoritative, matching scrape_investorroom.py's treatment of its
# own URL-embedded "Style B" dates for the same reason (a URL-embedded date
# is not guaranteed to always agree with the article's real publish date).
URL_DATE_RE = re.compile(r"/news-releases/\d{4}/(\d{2})-(\d{2})-(\d{4})-\d+")

# Any pagination link's ?page=N value. Used to find the highest page number
# advertised on a given listing page (see find_max_pagination_page()).
PAGE_LINK_RE = re.compile(r"[?&]page=(\d+)")

logger = logging.getLogger("scrape_investis")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem(_BaseNewsItem):
    """Investis Digital press-release item.

    Inherits slug, ticker, title, url, publish_date, raw_date_text, and
    publish_date_str from scrape_utils.NewsItem. No extra fields needed --
    this platform doesn't expose a publish time or category in the listing
    markup the way some Notified/InvestorRoom sites do.
    """


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

_SESSION = None


def get_session():
    """Return a persistent HTTP session impersonating Chrome's TLS fingerprint.

    See the module docstring's "Requires" section for why curl_cffi is used
    defensively here rather than plain ``requests``.
    """
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session(impersonate="chrome124")
    return _SESSION


def fetch_html(url: str, timeout: int = 30) -> str:
    """Fetch a URL and return its HTML. Raises on HTTP errors."""
    resp = get_session().get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

def listing_page_url(
    base_url: str,
    year: int,
    page: int = 1,
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
) -> str:
    """Build the listing URL for one page of one calendar year.

    page=1 is the first page and is reachable without a ?page= parameter at
    all; this always omits it for page 1 to match the canonical URL a
    browser would show (and to avoid a needless page=1 fetch looking
    different from the "no filter" URL in logs/debug dumps).
    """
    year_path = f"{news_releases_path.strip('/')}/{year}"
    url = join_url_path(base_url, year_path)
    if page > 1:
        url = f"{url}?page={page}"
    return url


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def find_max_pagination_page(soup: BeautifulSoup) -> Optional[int]:
    """Return the highest ?page=N value advertised anywhere on this page.

    The current page number is rendered as plain (non-linked) text, so on
    the last page of a year, the highest *linked* page number found is one
    less than the current page -- that's exactly the signal used to detect
    "this was the last page" in scrape_year() below: if the max found here
    is not greater than the page just fetched, there's nothing further to
    fetch. Returns None if no pagination links are present at all (a year
    with only one page of results renders no pager).
    """
    max_page: Optional[int] = None
    for a in soup.find_all("a", href=True):
        m = PAGE_LINK_RE.search(a["href"])
        if m:
            value = int(m.group(1))
            if max_page is None or value > max_page:
                max_page = value
    return max_page


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def date_from_url(url: str) -> Optional[date]:
    """Extract the MM-DD-YYYY date embedded in a detail-page URL, e.g.
    '.../news-releases/2025/12-10-2025-140135946' -> date(2025, 12, 10).

    Zero-request fallback only -- see URL_DATE_RE's comment above for why
    this isn't used as the primary date source.
    """
    m = URL_DATE_RE.search(url)
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_row_date(anchor) -> tuple[Optional[date], str]:
    """Find and parse the Date column for the listing row containing ``anchor``.

    Strategy 1 (primary): climb to the enclosing <tr> and read its first
    <td> -- that's the Date column in this platform's table layout. The
    cell's accessible "Date" label (rendered right before the visible date
    text for responsive/mobile table view) is harmless here: parse_date()
    only looks for month-name-shaped substrings, so a leading label word
    doesn't need to be stripped out first.

    Strategy 2 (fallback): if no <tr> ancestor is found -- e.g. a future
    theme change that renders cards instead of a <table> -- scan up to 4
    ancestors' full text for a parseable date, mirroring the equivalent
    fallback in utils/scrape_notified_utils.extract_date_and_time_from_row().
    """
    tr = anchor.find_parent("tr")
    if tr is not None:
        cells = tr.find_all("td")
        if cells:
            d, raw = parse_date(cells[0].get_text(separator=" ", strip=True))
            if d:
                return d, raw

    node = anchor
    for _ in range(4):
        node = node.parent
        if node is None or getattr(node, "name", None) in ("body", "html", "[document]"):
            break
        d, raw = parse_date(node.get_text(separator=" ", strip=True))
        if d:
            return d, raw

    return None, ""


# ---------------------------------------------------------------------------
# Listing-page parsing
# ---------------------------------------------------------------------------

def parse_listing_page(html: str, base_url: str, slug: str, ticker: str) -> list[NewsItem]:
    """Parse one listing page; return the list of NewsItems found on it."""
    parsed = urlparse(base_url)
    site_root = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not DETAIL_URL_RE.search(href):
            continue

        full_url = urljoin(site_root, href)
        norm_url = full_url.rstrip("/")
        if norm_url in seen_urls:
            continue

        title = anchor.get_text(separator=" ", strip=True)
        if not title:
            logger.debug("Skipping link with no title text: %s", full_url)
            continue

        seen_urls.add(norm_url)

        publish_date, raw_date_text = extract_row_date(anchor)
        if publish_date is None:
            publish_date = date_from_url(full_url)
            raw_date_text = "(from URL)" if publish_date else ""

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

    Shares the same cross-platform heuristics as scrape_investorroom.py and
    scrape_notified.py via scrape_utils.extract_date_from_detail_html().
    Only invoked for items where both the listing table and the URL-embedded
    date fallback come up empty (rare -- see parse_listing_page()).
    """
    try:
        html = fetch_html(url, timeout=timeout)
    except Exception as exc:
        logger.warning("Failed to fetch detail page %s: %s", url, exc)
        return None, ""
    return extract_date_from_detail_html(html)


# ---------------------------------------------------------------------------
# Scraping one year
# ---------------------------------------------------------------------------

def scrape_year(
    base_url: str,
    year: int,
    slug: str,
    ticker: str,
    polite_delay: float,
    timeout: int,
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
    debug_dump_html: Optional[Path] = None,
) -> list[NewsItem]:
    """Scrape every page of one calendar year's listing.

    Paginates forward from page 1 until either: the page comes back empty
    (year has no more -- or, if page 1, no -- releases), a page yields
    nothing new (safety net against an infinite loop), or
    find_max_pagination_page() says there's no page beyond the one just
    fetched. See that function's docstring for how the last-page signal
    works on this platform.
    """
    items: list[NewsItem] = []
    seen_urls: set[str] = set()
    page = 1

    while page <= MAX_PAGES_PER_YEAR:
        url = listing_page_url(base_url, year, page, news_releases_path)
        logger.info("Fetching %d news releases, page %d: %s", year, page, url)

        try:
            html = fetch_html(url, timeout=timeout)
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", url, exc)
            break

        if debug_dump_html and page == 1:
            debug_dump_html.write_text(html, encoding="utf-8")
            logger.info("Saved HTML to %s", debug_dump_html)

        soup = BeautifulSoup(html, "lxml")
        page_items = parse_listing_page(html, base_url=base_url, slug=slug, ticker=ticker)

        new_items = [item for item in page_items if item.url.rstrip("/") not in seen_urls]
        for item in new_items:
            seen_urls.add(item.url.rstrip("/"))
        items.extend(new_items)

        logger.info(
            "%d page %d: %d item(s) found, %d new",
            year, page, len(page_items), len(new_items),
        )

        if not page_items:
            logger.info("%d page %d is empty. Done with this year.", year, page)
            break

        if not new_items:
            logger.warning(
                "%d page %d: all %d item(s) already seen -- stopping to avoid a loop.",
                year, page, len(page_items),
            )
            break

        max_page = find_max_pagination_page(soup)
        if max_page is None or max_page <= page:
            logger.info("%d page %d is the last page. Done with this year.", year, page)
            break

        page += 1
        time.sleep(polite_delay)

    return items


# ---------------------------------------------------------------------------
# Scraping (full history or a specific set of years)
# ---------------------------------------------------------------------------

def scrape(
    base_url: str,
    slug: str,
    ticker: str,
    years: Optional[set[int]],
    polite_delay: float,
    timeout: int,
    debug_dump_html: Optional[Path],
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
) -> list[NewsItem]:
    """Scrape either a specific set of years or the site's entire history.

    Because the year is part of the URL on this platform (unlike Notified/
    InvestorRoom), there's no need to paginate an unfiltered feed and filter
    client-side, and no need for the binary-search-across-pages trick
    scrape_notified.py uses to jump to a target year -- each requested year
    is simply its own direct fetch.

    With an explicit *years* filter: fetch exactly those years, newest first.

    Without one (the default, "give me everything"): walk backward one
    calendar year at a time starting from the current year, stopping the
    first time a year comes back with zero items -- which is exactly what
    an out-of-range year renders as ("Information will appear here in due
    course.", confirmed on Home Depot's /news-releases/archive page for
    years before the company's earliest press releases). MAX_YEARS_BACK is
    a hard safety cap in case that assumption ever breaks (e.g. a single
    missing/placeholder year in the middle of otherwise-real history would
    stop the scan early) -- if that happens for a real source, prefer an
    explicit --start-year/--end-year over relying on the full-history scan.
    """
    all_items: list[NewsItem] = []

    if years:
        target_years = sorted(years, reverse=True)
        for i, year in enumerate(target_years):
            year_items = scrape_year(
                base_url, year, slug, ticker, polite_delay, timeout,
                news_releases_path=news_releases_path,
                debug_dump_html=debug_dump_html if i == 0 else None,
            )
            all_items.extend(year_items)
            if i < len(target_years) - 1:
                time.sleep(polite_delay)
    else:
        current_year = date.today().year
        year = current_year
        first_fetch = True
        while current_year - year <= MAX_YEARS_BACK:
            year_items = scrape_year(
                base_url, year, slug, ticker, polite_delay, timeout,
                news_releases_path=news_releases_path,
                debug_dump_html=debug_dump_html if first_fetch else None,
            )
            first_fetch = False
            if not year_items:
                logger.info(
                    "%d has no press releases -- treating it as the end of "
                    "this site's history and stopping the full-history scan.",
                    year,
                )
                break
            all_items.extend(year_items)
            year -= 1
            time.sleep(polite_delay)
        else:
            logger.warning(
                "Reached MAX_YEARS_BACK (%d) without finding an empty year; "
                "stopping. Pass --start-year to scrape further back explicitly.",
                MAX_YEARS_BACK,
            )

    return dedupe_by_url(all_items)


# ---------------------------------------------------------------------------
# Source resolution (sources.yaml integration)
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
    news_releases_path: Optional[str] = None,
) -> tuple[str, str, str, str]:
    """Resolve (base_url, slug, ticker, news_releases_path) from CLI args
    and sources.yaml.

    base_url is the IR site root (e.g. https://ir.homedepot.com), NOT the
    news-releases listing URL -- callers append news_releases_path and the
    year themselves via listing_page_url().

    news_releases_path precedence (highest wins):
      1. --news-releases-path on the CLI
      2. the "news_releases_path" field on the matched sources.yaml record
      3. DEFAULT_NEWS_RELEASES_PATH ("news-releases")
    """
    url, slug, ticker, record, _extra_query_params = resolve_source_identity(
        url, slug, ticker,
        default_slug=DEFAULT_SLUG, default_ticker=DEFAULT_TICKER, default_url=DEFAULT_BASE_URL,
        strip_url_to_root=True, logger=logger,
    )

    news_releases_path = resolve_field_precedence(
        news_releases_path, record, "news_releases_path", DEFAULT_NEWS_RELEASES_PATH
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
            "Listing path appended to the IR site root, before the year, "
            "e.g. press-releases (default: news-releases). Overrides "
            "sources.yaml's news_releases_path field for this run."
        ),
    )

    detail = parser.add_argument_group("detail-page fetch")
    detail.add_argument(
        "--fetch-detail-pages", action="store_true",
        help=(
            "For items with no date found on the listing page or in their "
            "URL, fetch each detail page to extract the date."
        ),
    )

    out = parser.add_argument_group("output")
    out.add_argument(
        "--data-dir", type=Path, default=DATA_DIR,
        help=f"Root of the data/ tree for --format csv (default: {DATA_DIR})",
    )

    # Shared: --polite-delay/--timeout/--debug-dump-html/--verbose
    add_network_and_debug_args(parser, default_polite_delay=15.0)

    return parser


def scrape_and_filter(
    argv: Optional[list[str]] = None, *, write: bool = True
) -> tuple[int, list[NewsItem]]:
    """Parse args, scrape, filter/preview, and (by default) write out results.

    Split out from main() so a caller other than the command line --
    scrape_all.py -- can invoke it directly and get the scraped items back
    as a normal return value. write=False skips merging into data/'s daily
    CSVs and leaves that to the caller instead; see finalize_and_output()'s
    docstring for why. Returns (return_code, filtered_items); return_code is
    always 0 here (this scraper has no early-exit failure path today).
    """
    # See scrape_notified.py's scrape_and_filter() for why this reconfigure
    # exists (avoids a UnicodeEncodeError crash on Windows' legacy console
    # codepage). Kept here (rather than in main() below) so it still applies
    # when scrape_all.py calls this function directly, bypassing main().
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(errors="replace")
            except Exception:
                pass

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    base_url, slug, ticker, news_releases_path = resolve_source(
        args.url, args.slug, args.ticker, args.news_releases_path
    )
    logger.info(
        "Scraping %s (%s) from %s",
        slug, ticker, join_url_path(base_url, news_releases_path),
    )

    years = parse_year_args(args)
    if years:
        logger.info("Restricting to year(s): %s", sorted(years))
    else:
        logger.info("No --year filter given; scraping full history.")

    all_items = scrape(
        base_url=base_url,
        slug=slug,
        ticker=ticker,
        years=years,
        polite_delay=args.polite_delay,
        timeout=args.timeout,
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
    # standardizes across scrape_notified.py/scrape_investorroom.py/
    # scrape_q4_ir.py/scrape_investis.py (preview-always, --format both,
    # --output default path), and for what write= does.
    filtered = finalize_and_output(
        all_items,
        years=years, since=args.since, until=args.until, limit=None,
        format=args.format, output=args.output, dry_run=args.dry_run,
        data_dir=args.data_dir,
        default_json_path=REPO_ROOT / "investis_news.json",
        write=write,
    )

    return 0, filtered


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for standalone invocation (``python src/scrape_investis.py ...``).

    Thin wrapper around scrape_and_filter(); see that function's docstring
    for the write= behavior scrape_all.py relies on when calling it directly
    instead of going through this main().
    """
    return_code, _items = scrape_and_filter(argv)
    return return_code


if __name__ == "__main__":
    sys.exit(main())