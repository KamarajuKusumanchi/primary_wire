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

  # Restrict to a year or range
  python src/scrape_investorroom.py --year 2025 --dry-run
  python src/scrape_investorroom.py --start-year 2023 --end-year 2025 --dry-run

  # Date range
  python src/scrape_investorroom.py --since 2024-01-01 --until 2024-12-31 --dry-run

  # Control items per listing page (default 100; server default without ?l= is 5)
  python src/scrape_investorroom.py --limit 50 --dry-run

  # Fetch detail pages to resolve missing dates
  python src/scrape_investorroom.py --fetch-detail-pages --dry-run

  # Output as JSON
  python src/scrape_investorroom.py --format json --output out.json --dry-run

  # Save raw HTML for debugging
  python src/scrape_investorroom.py --debug-dump-html /tmp/chipotle.html --dry-run

Requires
--------
  pip install requests beautifulsoup4 lxml pandas ruamel.yaml

Run at most once per day. Requests are spaced by --polite-delay (default 15 s).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse, urlencode

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Install with: pip install requests")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

try:
    import pandas as pd
except ImportError:
    sys.exit("Missing dependency. Install with: pip install pandas")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_SLUG = "chipotle"
DEFAULT_TICKER = "CMG"
DEFAULT_BASE_URL = "https://ir.chipotle.com"

NEWS_RELEASES_PATH = "/news-releases"

CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_datetime"]
SORT_FIELDS = ["publish_datetime", "slug", "ticker", "title", "url"]

DEFAULT_PAGE_LIMIT = 100  # ?l=100 -- 100 items per page vs server default of 5
MAX_PAGES = 50            # safety cap on pagination loops

# Regex patterns to identify InvestorRoom detail-page URLs.
DETAIL_URL_LEGACY_RE = re.compile(r"[?&]item=\d+", re.IGNORECASE)
# Excludes fragment URLs (e.g. /2026-01-12-TITLE#assets_...) -- those are photo
# gallery anchors on the same page, not press-release detail pages.
DETAIL_URL_MODERN_RE = re.compile(r"/\d{4}-\d{2}-\d{2}-[^/#]+/?$", re.IGNORECASE)

# Date parsing patterns used across listing and detail pages.
_MONTH_NAMES = (
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)
DATE_PATTERNS = [
    # "June 18, 2026" / "Jun 18, 2026"
    (re.compile(rf"\b(?:{_MONTH_NAMES})\.?\s+\d{{1,2}},\s*\d{{4}}\b"), ["%b %d, %Y", "%B %d, %Y"]),
    # "06/18/2026"
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"), ["%m/%d/%Y"]),
    # "2026-06-18"
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), ["%Y-%m-%d"]),
]

logger = logging.getLogger("scrape_investorroom")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
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
        return {
            "slug": self.slug,
            "ticker": self.ticker,
            "title": self.title,
            "url": self.url,
            "publish_datetime": self.publish_datetime,
        }


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_date(text: str) -> tuple[Optional[date], str]:
    """Return the first parseable date found in ``text`` and its raw match."""
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


def date_from_url(url: str) -> Optional[date]:
    """Extract date from a modern InvestorRoom URL like /2025-10-29-title."""
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

def listing_page_url(base_url: str, offset: int = 0, page_limit: int = DEFAULT_PAGE_LIMIT) -> str:
    """Build a paginated listing URL using InvestorRoom's ?l= / ?o= parameters.

    ?l=<limit>   items per page (server default is 5 when omitted; always pass explicitly)
    ?o=<offset>  skip this many items (0-based; omit on the first page)
    """
    base = base_url.rstrip("/") + NEWS_RELEASES_PATH
    params: dict[str, int] = {"l": page_limit}
    if offset > 0:
        params["o"] = offset
    return base + "?" + urlencode(params)


def year_filter_url(
    base_url: str, year: int, offset: int = 0, page_limit: int = DEFAULT_PAGE_LIMIT
) -> str:
    """Build a year-filtered listing URL."""
    base = base_url.rstrip("/") + NEWS_RELEASES_PATH
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

    Tries multiple selectors in priority order before falling back to a
    full-text scan of the article body.
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

    # Priority 2: common date CSS selectors on InvestorRoom pages.
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

    # Priority 3: scan the first 2000 characters of the article body.
    article = soup.find("article") or soup.find("main") or soup.find("body")
    if article:
        d, raw = parse_date(article.get_text(separator=" ", strip=True)[:2000])
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
            start_url = year_filter_url(base_url, year, page_limit=page_limit)
        else:
            start_url = listing_page_url(base_url, page_limit=page_limit)

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
    seen: set[str] = set()
    deduped: list[NewsItem] = []
    for item in all_items:
        k = item.url.rstrip("/")
        if k not in seen:
            seen.add(k)
            deduped.append(item)
    return deduped


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_items(
    items: list[NewsItem],
    years: Optional[set[int]],
    since: Optional[date],
    until: Optional[date],
    limit: Optional[int],
) -> list[NewsItem]:
    """Filter and sort items; apply an optional result cap."""
    result = [
        item for item in items
        if (years is None or (item.publish_date and item.publish_date.year in years))
        and (since is None or (item.publish_date and item.publish_date >= since))
        and (until is None or (item.publish_date and item.publish_date <= until))
    ]
    result.sort(key=lambda i: (i.publish_date or date.min, i.title))
    return result[:limit] if limit is not None else result


# ---------------------------------------------------------------------------
# Output: daily CSVs and JSON
# ---------------------------------------------------------------------------

def csv_path_for_date(d: date, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / str(d.year) / f"{d.isoformat()}.csv"


def load_csv(path: Path) -> pd.DataFrame:
    """Load an existing daily CSV, or return an empty DataFrame with the right columns."""
    if not path.exists():
        return pd.DataFrame(columns=CSV_FIELDS)
    return pd.read_csv(path, dtype=str).fillna("")


def write_csv(df: pd.DataFrame, path: Path) -> None:
    """Write a daily CSV, sorted by SORT_FIELDS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df_sorted = df.sort_values(by=SORT_FIELDS, na_position="last")
    df_sorted.to_csv(path, index=False, columns=CSV_FIELDS)


def merge_into_daily_csvs(
    items: list[NewsItem], dry_run: bool, data_dir: Path = DATA_DIR
) -> dict:
    """Merge scraped items into per-date CSV files under data_dir.

    Uses pandas to load, upsert by URL, sort, and write each file.
    """
    dated = [item for item in items if item.publish_date is not None]
    undated = [item for item in items if item.publish_date is None]

    summary = {"files_written": 0, "rows_added": 0, "rows_updated": 0, "undated": len(undated)}

    if dated:
        df_all = pd.DataFrame([item.to_row() for item in dated], columns=CSV_FIELDS)
        df_all["_date"] = pd.to_datetime(df_all["publish_datetime"]).dt.date

        for d, df_new in df_all.groupby("_date"):
            df_new = df_new.drop(columns=["_date"])
            path = csv_path_for_date(d, data_dir)
            df_existing = load_csv(path)

            # Upsert: drop existing rows whose URLs appear in the new batch, then concat.
            new_count = len(df_new[~df_new["url"].isin(set(df_existing["url"]))])
            updated_count = len(df_new) - new_count

            df_kept = df_existing[~df_existing["url"].isin(set(df_new["url"]))]
            df_merged = pd.concat([df_kept, df_new], ignore_index=True)

            summary["rows_added"] += new_count
            summary["rows_updated"] += updated_count

            rel_path = (
                path.relative_to(data_dir.parent)
                if data_dir.parent in path.parents else path
            )
            if dry_run:
                logger.info(
                    "[dry-run] Would write %s (%d new, %d updated, %d total rows)",
                    rel_path, new_count, updated_count, len(df_merged),
                )
                continue

            write_csv(df_merged, path)
            summary["files_written"] += 1
            logger.info(
                "Wrote %s (%d new, %d updated, %d total rows)",
                path, new_count, updated_count, len(df_merged),
            )

    if undated:
        logger.warning(
            "%d item(s) had no resolvable publish date and were NOT written. "
            "Re-run with --fetch-detail-pages to attempt resolution.",
            len(undated),
        )
        for item in undated:
            logger.warning("  UNDATED: %s | %s", item.title, item.url)

    return summary


def write_json(items: list[NewsItem], path: Path, dry_run: bool) -> None:
    payload = [item.to_row() for item in items]
    if dry_run:
        logger.info("[dry-run] Would write %d item(s) to %s", len(payload), path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info("Wrote %d item(s) to %s", len(payload), path)


def print_preview(items: list[NewsItem]) -> None:
    if not items:
        print("No items to preview.")
        return
    print(f"\n{len(items)} item(s):\n")
    for item in items:
        d = item.publish_datetime or "????-??-??"
        print(f"  {d}  {item.title}")
        print(f"             {item.url}")
    print()


# ---------------------------------------------------------------------------
# Source resolution (sources.yaml integration)
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
) -> tuple[str, str, str]:
    """Resolve (base_url, slug, ticker) from CLI args and sources.yaml.

    Returns (base_url, slug, ticker).  base_url is the IR site root
    (e.g. https://ir.chipotle.com), NOT the news-releases listing URL.
    Callers append NEWS_RELEASES_PATH themselves.
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
        # Strip path so we hold only the site root.
        parsed = urlparse(url)
        url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        record = find_source_by_ir_url(sources, url) if sources else None
        if record is None:
            logger.warning(
                "No sources.yaml record matched the host of '%s'. "
                "Slug and ticker will be empty.", url
            )
        else:
            slug = record.get("slug", "")
            ticker = record.get("ticker", "")
    else:
        slug, ticker, url = DEFAULT_SLUG, DEFAULT_TICKER, DEFAULT_BASE_URL

    if not slug:
        logger.warning("Slug is empty; CSV rows will have an empty slug column.")
    if not ticker:
        logger.warning("Ticker is empty; CSV rows will have an empty ticker column.")

    return url, slug, ticker


# ---------------------------------------------------------------------------
# Year argument parsing
# ---------------------------------------------------------------------------

def parse_year_args(args: argparse.Namespace) -> Optional[set[int]]:
    """Resolve --year / --start-year / --end-year into a set of years (or None for all)."""
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
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    source = parser.add_argument_group("source")
    source.add_argument(
        "--url", default=None,
        help=(
            "IR site base URL or news-releases page URL. If omitted, derived from "
            "sources.yaml via --slug or --ticker. Defaults to Chipotle."
        ),
    )
    source.add_argument("--slug", default=None, help="sources.yaml slug.")
    source.add_argument("--ticker", default=None, help="Ticker symbol.")

    filtering = parser.add_argument_group("filtering")
    filtering.add_argument(
        "--year", type=int, action="append", metavar="YYYY",
        help="Only include press releases from YEAR. Repeatable.",
    )
    filtering.add_argument("--start-year", type=int, metavar="YYYY")
    filtering.add_argument("--end-year", type=int, metavar="YYYY")
    filtering.add_argument(
        "--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="Only include releases on or after this date.",
    )
    filtering.add_argument(
        "--until", type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="Only include releases on or before this date.",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--format", choices=["csv", "json"], default="csv",
        help="Output format: 'csv' (daily files under data/) or 'json' (single file).",
    )
    output.add_argument(
        "--output", type=Path, default=None, metavar="PATH",
        help="Output path for --format json.",
    )
    output.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and parse but do not write any files.",
    )

    detail = parser.add_argument_group("detail-page fetch")
    detail.add_argument(
        "--fetch-detail-pages", action="store_true",
        help=(
            "For items with no date found on the listing page, fetch each detail "
            "page to extract the date. Useful for legacy ?item=NNN URLs."
        ),
    )

    network = parser.add_argument_group("network")
    network.add_argument(
        "--limit", type=int, default=DEFAULT_PAGE_LIMIT, metavar="N",
        help=(
            f"Items per listing page via ?l= (default: {DEFAULT_PAGE_LIMIT}). "
            "The server default without ?l= is 5, which causes many more requests."
        ),
    )
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

    base_url, slug, ticker = resolve_source(args.url, args.slug, args.ticker)
    logger.info("Scraping %s (%s) from %s", slug, ticker, base_url + NEWS_RELEASES_PATH)

    years = parse_year_args(args)

    # Scrape
    all_items = scrape(
        base_url=base_url,
        slug=slug,
        ticker=ticker,
        years=years,
        polite_delay=args.polite_delay,
        timeout=args.timeout,
        page_limit=args.limit,
        debug_dump_html=args.debug_dump_html,
    )
    logger.info("Scraped %d item(s) total (before filtering).", len(all_items))

    # Resolve missing dates from detail pages (opt-in)
    if args.fetch_detail_pages:
        fetch_missing_dates(all_items, polite_delay=args.polite_delay, timeout=args.timeout)

    # Filter
    filtered = filter_items(
        all_items, years=years, since=args.since, until=args.until, limit=None
    )
    logger.info("%d item(s) after filtering.", len(filtered))

    if args.dry_run:
        print_preview(filtered)

    # Output
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