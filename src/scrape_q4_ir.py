#!/usr/bin/env python3
"""
scrape_q4_ir.py

Scrape press-release listings from any Q4 Inc. investor relations "News" page
and merge them into primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

Q4-powered IR sites (Costco, CDW, and many more in sources.yaml) share
the same news-details URL shape:

    /news/news-details/<year>/<slug>/default.aspx

Pages are rendered client-side. A plain requests.get() returns only a
"Loading..." placeholder, so this script drives Chrome via Playwright and
parses the rendered DOM with BeautifulSoup. No private API is used -- this
reads exactly what a human visiting the page would see.

Date extraction works in two stages:

  1. Listing-page parse (fast, zero extra requests): walk up to 5 ancestor
     elements around each news link looking for a date in the card text.
     Works on many Q4 themes (e.g. Costco).

  2. Detail-page fallback (opt-in via --fetch-detail-pages): for any item
     still missing a date after stage 1, fetch its individual detail page
     and parse the date from there. Required for some Q4 themes (e.g. CDW)
     where the listing page does not include dates in the card HTML.
     Fetches are spaced by --polite-delay. Each detail page is loaded in
     the same already-open browser session to avoid repeated launch overhead.

Examples:
    # Costco -- dates found on listing page, no detail fetches needed
    python src/scrape_q4_ir.py --dry-run

    # CDW -- dates only on detail pages; --fetch-detail-pages is required
    python src/scrape_q4_ir.py \\
        --url https://investor.cdw.com/news/default.aspx \\
        --fetch-detail-pages --dry-run

    # Any other Q4 IR site by slug or ticker
    python src/scrape_q4_ir.py --slug cdw --fetch-detail-pages --dry-run
    python src/scrape_q4_ir.py --ticker CDW --fetch-detail-pages --dry-run

    # Scrape a specific year
    python src/scrape_q4_ir.py --year 2025

    # Scrape a range of years, output as JSON
    python src/scrape_q4_ir.py --start-year 2023 --end-year 2025 \\
        --format json --output out.json --dry-run

    # Watch the browser and save rendered HTML for debugging
    python src/scrape_q4_ir.py --show-browser --debug-dump-html /tmp/page.html --dry-run

    # Headless-first with automatic fallback to a visible browser if blocked
    #
    # Q4 IR sites are sometimes fronted by Cloudflare or a similar bot-detection
    # layer that fingerprints headless Chrome and serves a challenge page instead
    # of real content. The symptom is a timeout waiting for news-detail links
    # followed by zero items scraped. A visible browser window passes the check
    # because it presents the same fingerprint as a normal user session.
    #
    # --fallback-to-visible detects this automatically: if the headless pass
    # returns zero items it logs a warning and retries with a visible window.
    # Use it as the default for any site that intermittently blocks headless
    # requests; leave it off when you know the site is clean (faster, no GUI).
    #
    # Not suitable for headless CI environments -- the fallback opens a real
    # desktop window and will fail or hang on a machine with no display.
    python src/scrape_q4_ir.py --slug costco --year 2026 --fallback-to-visible

Requires:
    pip install playwright beautifulsoup4
Chrome is assumed to already be installed. channel="chrome" reuses it directly;
no `playwright install` download is needed.

Per README.txt's "Guidelines for automated contributions": run at most once a
day. The default --polite-delay spaces out in-page interactions and detail-page
fetches so the site is not hammered.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4")

try:
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except ImportError:
    sys.exit("Missing dependency. Install with: pip install playwright")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_URL = "https://investor.costco.com/news/default.aspx"
DEFAULT_SLUG = "costco"
DEFAULT_TICKER = "COST"

NEWS_PATH = "/news/default.aspx"

CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_datetime"]
SORT_FIELDS = ["publish_datetime", "slug", "ticker", "title", "url"]

CATEGORY_CHOICES = ["All News", "Sales Releases", "Earnings Releases", "Other Company Releases"]

# Matches /news/news-details/<year>/<slug>[/default.aspx] on any Q4 IR hostname.
NEWS_LINK_RE = re.compile(r"/news/news-details/\d{4}/[^/]+/?(?:default\.aspx)?", re.IGNORECASE)
NEWS_LINK_SELECTOR = "a[href*='/news/news-details/']"

MONTHS = (
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
DATE_PATTERNS = [
    # "Jun 18, 2026" / "June 18, 2026"
    (re.compile(rf"\b(?:{MONTHS})\.?\s+\d{{1,2}},\s*\d{{4}}\b"), ["%b %d, %Y", "%B %d, %Y"]),
    # "06/18/2026"
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"), ["%m/%d/%Y"]),
    # "2026-06-18"
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), ["%Y-%m-%d"]),
]

logger = logging.getLogger("scrape_q4_ir")


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
    category: str = ""
    raw_date_text: str = ""

    @property
    def publish_datetime(self) -> str:
        return self.publish_date.isoformat() if self.publish_date else ""

    def to_csv_row(self) -> dict:
        return {
            "slug": self.slug,
            "ticker": self.ticker,
            "title": self.title,
            "url": self.url,
            "publish_datetime": self.publish_datetime,
        }

    def to_json_dict(self) -> dict:
        d = asdict(self)
        d["publish_date"] = self.publish_datetime
        return d


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_date_near(text: str) -> tuple[Optional[date], str]:
    """Return the first parseable date found anywhere in `text`, plus its raw
    matched string. Returns (None, "") if no date is found."""
    for pattern, formats in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(0).strip()
        cleaned = re.sub(r"\s+", " ", raw)
        for fmt in formats:
            try:
                return datetime.strptime(cleaned, fmt).date(), raw
            except ValueError:
                continue
    return None, ""


def _find_category_near(text: str) -> str:
    for cat in CATEGORY_CHOICES:
        if cat != "All News" and cat in text:
            return cat
    return ""


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def _launch_browser(p, headless: bool, browser_channel: str, timeout_ms: int):
    """Launch a Chromium browser and return a configured Page.

    Extracted to avoid duplicating the same launch/configure block in both
    render_news_page() and fetch_missing_dates().
    """
    launch_kwargs: dict = {"headless": headless}
    if browser_channel:
        launch_kwargs["channel"] = browser_channel
    browser = p.chromium.launch(**launch_kwargs)
    page = browser.new_page()
    page.set_default_timeout(timeout_ms)
    return browser, page


def _try_select_year(page: Page, year: int, timeout_ms: int) -> bool:
    """Best-effort: set the page's year filter to `year`.

    Tries a native <select> first, then a custom listbox. Returns True if a
    year control was found and interacted with, False otherwise (non-fatal --
    caller falls back to client-side year filtering).
    """
    year_str = str(year)

    for select in page.locator("select").all():
        try:
            options = [o.strip() for o in select.locator("option").all_inner_texts()]
        except Exception:
            continue
        if any(re.fullmatch(r"\d{4}", o) for o in options):
            if year_str not in options:
                logger.warning(
                    "Year %s not found among available options (%s).", year_str, ", ".join(options)
                )
                return False
            select.select_option(label=year_str)
            return True

    candidates = page.get_by_text(re.compile(r"select year", re.IGNORECASE))
    if candidates.count() > 0:
        try:
            candidates.first.click(timeout=timeout_ms)
            option = page.get_by_text(re.compile(rf"^\s*{year_str}\s*$"))
            if option.count() > 0:
                option.first.click(timeout=timeout_ms)
                return True
        except PlaywrightTimeoutError:
            logger.warning("Timed out trying to click custom year-dropdown option for %s.", year_str)

    return False


def _try_select_category(page: Page, category: str, timeout_ms: int) -> bool:
    """Best-effort: set the category filter. Returns False if not found
    (non-fatal; script proceeds with the default category view)."""
    for select in page.locator("select").all():
        try:
            options = [o.strip() for o in select.locator("option").all_inner_texts()]
        except Exception:
            continue
        if any(c in options for c in CATEGORY_CHOICES):
            if category not in options:
                logger.warning(
                    "Category '%s' not found among options (%s).", category, ", ".join(options)
                )
                return False
            select.select_option(label=category)
            return True

    candidates = page.get_by_text(re.compile(re.escape(category), re.IGNORECASE))
    if candidates.count() > 0:
        try:
            candidates.first.click(timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            pass
    return False


def _click_load_more(page: Page, timeout_ms: int) -> bool:
    """Click a load-more / show-more / pagination-next control if visible.
    Returns True if clicked, False if no such control exists (end of list)."""
    button = page.get_by_role("button", name=re.compile(r"load more|show more|view more", re.IGNORECASE))
    if button.count() == 0:
        button = page.get_by_text(re.compile(r"load more|show more|view more", re.IGNORECASE))
    if button.count() == 0:
        return False
    try:
        if not button.first.is_visible():
            return False
        button.first.click(timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def _current_news_hrefs(page: Page) -> set[str]:
    return set(page.locator(NEWS_LINK_SELECTOR).evaluate_all("els => els.map(e => e.getAttribute('href'))"))


def _wait_for_news_links(page: Page, timeout_ms: int) -> None:
    """Wait for at least one press-release link to appear in the DOM.

    Uses direct selector polling rather than networkidle, which is unreliable
    on SPA content (analytics / consent scripts keep the network busy after the
    content widget has already finished rendering).
    """
    try:
        page.wait_for_selector(NEWS_LINK_SELECTOR, timeout=timeout_ms, state="attached")
    except PlaywrightTimeoutError:
        logger.warning(
            "Timed out after %dms waiting for '%s'. Page may be slow or blocked; "
            "continuing so --debug-dump-html can still capture what loaded.",
            timeout_ms, NEWS_LINK_SELECTOR,
        )


def _wait_for_list_change(
    page: Page,
    previous_hrefs: set[str],
    timeout_ms: int,
    poll_interval_ms: int = 200,
    settle_ms: int = 400,
) -> set[str]:
    """Poll until the rendered press-release link set differs from
    `previous_hrefs`, then return the new set.

    This replaces networkidle for in-page actions (year/category switch,
    load-more click): "has the list changed" is a tighter signal than
    "has the network gone quiet". If nothing changes before the timeout
    (e.g. same year re-selected, or genuinely no more pages), log and return
    the current snapshot.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    current = previous_hrefs
    while time.monotonic() < deadline:
        current = _current_news_hrefs(page)
        if current != previous_hrefs:
            time.sleep(settle_ms / 1000)
            return _current_news_hrefs(page)
        time.sleep(poll_interval_ms / 1000)
    logger.warning(
        "Press-release link set did not change within %dms after last action. "
        "Proceeding with whatever is currently rendered (expected if re-selecting "
        "the same year, or no more pages to load).",
        timeout_ms,
    )
    return current


# ---------------------------------------------------------------------------
# Listing-page rendering
# ---------------------------------------------------------------------------

def render_news_page(
    url: str,
    year: Optional[int],
    category: str,
    headless: bool,
    browser_channel: str,
    timeout_ms: int,
    change_timeout_ms: int,
    polite_delay: float,
    max_load_more: int,
    debug_dump_html: Optional[Path],
) -> str:
    """Drive Chrome to the listing page, apply filters, expand pagination, and
    return the fully rendered HTML."""
    with sync_playwright() as p:
        browser, page = _launch_browser(p, headless, browser_channel, timeout_ms)

        logger.info("Loading %s ...", url)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        _wait_for_news_links(page, timeout_ms)

        if category and category != "All News":
            logger.info("Selecting category: %s", category)
            before = _current_news_hrefs(page)
            if _try_select_category(page, category, timeout_ms):
                time.sleep(polite_delay)
                _wait_for_list_change(page, before, change_timeout_ms)
            else:
                logger.warning(
                    "Could not apply category filter '%s'; continuing with default view.", category
                )

        if year is not None:
            logger.info("Selecting year: %s", year)
            before = _current_news_hrefs(page)
            if _try_select_year(page, year, timeout_ms):
                time.sleep(polite_delay)
                _wait_for_list_change(page, before, change_timeout_ms)
            else:
                logger.warning(
                    "Could not find/apply year filter for %s. Falling back to default view "
                    "and filtering by year client-side.",
                    year,
                )

        clicks = 0
        while clicks < max_load_more:
            before = _current_news_hrefs(page)
            if not _click_load_more(page, timeout_ms):
                break
            time.sleep(polite_delay)
            after = _wait_for_list_change(page, before, change_timeout_ms)
            clicks += 1
            logger.debug("Load-more click #%d: %d -> %d links", clicks, len(before), len(after))
            if len(after) <= len(before):
                break

        html = page.content()
        if debug_dump_html:
            debug_dump_html.parent.mkdir(parents=True, exist_ok=True)
            debug_dump_html.write_text(html, encoding="utf-8")
            logger.info("Saved rendered HTML to %s", debug_dump_html)

        browser.close()
        return html


# ---------------------------------------------------------------------------
# Listing-page parse (stage 1)
# ---------------------------------------------------------------------------

def parse_news_items(html: str, base_url: str, slug: str, ticker: str) -> list[NewsItem]:
    """Extract news items from the rendered listing-page HTML.

    Strategy: find every <a> matching the news-details URL shape, then walk
    up to 5 ancestor elements looking for a date (and category) in the
    surrounding card. Stops climbing as soon as the ancestor contains more
    than one news link -- that means we've crossed into the shared list
    wrapper and would pick up sibling items' dates.

    This works on Q4 themes that embed the date in each card (e.g. Costco).
    For themes that don't (e.g. CDW), items come back with publish_date=None;
    use fetch_missing_dates() to fill them in from individual detail pages.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not NEWS_LINK_RE.search(href):
            continue

        url = urljoin(base_url, href)
        if url in seen_urls:
            continue

        title = anchor.get_text(strip=True)
        if not title:
            container = anchor.find_parent(["li", "article", "div"])
            title = container.get_text(" ", strip=True) if container else ""
        if not title:
            continue

        publish_date, raw_date_text, category = None, "", ""
        node = anchor
        for _ in range(5):
            node = node.find_parent(["li", "article", "div", "section"])
            if node is None:
                break
            sibling_links = [
                a for a in node.find_all("a", href=True)
                if NEWS_LINK_RE.search(a["href"])
            ]
            if len(sibling_links) > 1:
                break
            context_text = node.get_text(" ", strip=True)
            if publish_date is None:
                publish_date, raw_date_text = _parse_date_near(context_text)
            if not category:
                category = _find_category_near(context_text)
            if publish_date:
                break

        seen_urls.add(url)
        items.append(
            NewsItem(
                slug=slug,
                ticker=ticker,
                title=title,
                url=url,
                publish_date=publish_date,
                category=category,
                raw_date_text=raw_date_text,
            )
        )

    if not items:
        logger.warning(
            "No news items found. The page markup may not match the expected "
            "'/news/news-details/<year>/...' link pattern. Re-run with "
            "--debug-dump-html to inspect what was actually rendered."
        )
    else:
        missing = sum(1 for i in items if i.publish_date is None)
        if missing:
            logger.warning(
                "%d of %d items had no parseable date near their listing-page link.",
                missing, len(items),
            )

    return items


# ---------------------------------------------------------------------------
# Detail-page fallback (stage 2)
# ---------------------------------------------------------------------------

def _parse_date_from_detail_html(html: str) -> Optional[date]:
    """Extract a publish date from a Q4 IR detail page.

    Q4 detail pages place the date as a short text node immediately after the
    press-release <h3> title -- e.g.:

        <h3>CDW Reports First Quarter 2026 Earnings</h3>
        May 6, 2026

    We parse the full-page text with the same DATE_PATTERNS used for listing
    pages, but limit the search to the first 4 KB of rendered body text so we
    don't accidentally match a date buried in the press-release body copy.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Prefer a <time> element with a datetime attribute -- some Q4 themes use it.
    for tag in soup.find_all("time"):
        dt_attr = tag.get("datetime", "")
        d, _ = _parse_date_near(dt_attr or tag.get_text())
        if d:
            return d

    # Fall back to scanning the first ~4 KB of page text. The date is always
    # near the top of a detail page; stopping early avoids false matches from
    # body text ("Q1 2026 results... March 31, 2026 balance sheet...").
    body_text = soup.get_text(" ", strip=True)[:4096]
    d, _ = _parse_date_near(body_text)
    return d


def fetch_missing_dates(
    items: list[NewsItem],
    headless: bool,
    browser_channel: str,
    timeout_ms: int,
    polite_delay: float,
) -> None:
    """Fill in publish_date for items that stage-1 listing-page parsing missed.

    Opens one browser session and fetches each undated detail page in sequence,
    spacing requests by polite_delay seconds. Modifies items in place.
    """
    undated = [item for item in items if item.publish_date is None]
    if not undated:
        return

    logger.info(
        "Fetching detail pages for %d undated item(s) to resolve publish dates ...",
        len(undated),
    )

    with sync_playwright() as p:
        browser, page = _launch_browser(p, headless, browser_channel, timeout_ms)

        for i, item in enumerate(undated):
            if i > 0:
                time.sleep(polite_delay)
            logger.info("  [%d/%d] %s", i + 1, len(undated), item.url)
            try:
                page.goto(item.url, wait_until="domcontentloaded", timeout=timeout_ms)
                # Wait for the headline to appear -- confirms the JS widget rendered.
                try:
                    page.wait_for_selector("h1, h2, h3", timeout=timeout_ms, state="visible")
                except PlaywrightTimeoutError:
                    pass
                html = page.content()
                d = _parse_date_from_detail_html(html)
                if d:
                    item.publish_date = d
                    item.raw_date_text = f"(detail page: {d.isoformat()})"
                    logger.debug("    -> %s", d)
                else:
                    logger.warning("    -> no date found on detail page: %s", item.url)
            except Exception as exc:
                logger.warning("    -> failed to fetch %s: %s", item.url, exc)

        browser.close()

    still_missing = sum(1 for item in undated if item.publish_date is None)
    if still_missing:
        logger.warning(
            "%d item(s) still have no date after detail-page fetch. "
            "They will be skipped when writing CSV output.",
            still_missing,
        )


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
# Output: CSV (primary_wire daily files) and JSON
# ---------------------------------------------------------------------------

def csv_path_for_date(data_dir: Path, d: date) -> Path:
    return data_dir / f"{d.year}" / f"{d.isoformat()}.csv"


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: tuple(r.get(k, "") for k in SORT_FIELDS))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def merge_into_daily_csvs(items: list[NewsItem], data_dir: Path, dry_run: bool) -> dict:
    """Group items by publish date and merge into data/YYYY/YYYY-MM-DD.csv.

    Items with no resolvable date are skipped (date-keyed file layout requires
    one). Returns a summary dict for the final status line.
    """
    by_date: dict[date, list[NewsItem]] = {}
    undated: list[NewsItem] = []
    for item in items:
        if item.publish_date is None:
            undated.append(item)
        else:
            by_date.setdefault(item.publish_date, []).append(item)

    summary = {"files_written": 0, "rows_added": 0, "rows_updated": 0, "undated": len(undated)}

    for d, day_items in sorted(by_date.items()):
        path = csv_path_for_date(data_dir, d)
        existing_rows = load_csv(path)
        existing_urls = {r["url"] for r in existing_rows}

        new_count, updated_count = 0, 0
        for item in day_items:
            row = item.to_csv_row()
            if row["url"] in existing_urls:
                updated_count += 1
            else:
                new_count += 1
            existing_rows = [r for r in existing_rows if r["url"] != row["url"]]
            existing_rows.append(row)

        summary["rows_added"] += new_count
        summary["rows_updated"] += updated_count

        if dry_run:
            logger.info(
                "[dry-run] Would write %s (%d new, %d updated, %d total rows)",
                path.relative_to(data_dir.parent) if data_dir.parent in path.parents else path,
                new_count, updated_count, len(existing_rows),
            )
            continue

        write_csv(path, existing_rows)
        summary["files_written"] += 1
        logger.info(
            "Wrote %s (%d new, %d updated, %d total rows)",
            path, new_count, updated_count, len(existing_rows),
        )

    if undated:
        logger.warning(
            "%d item(s) had no resolvable publish date and were NOT written to any "
            "daily CSV. Use --debug-dump-html / --verbose to diagnose, or pass "
            "--fetch-detail-pages to resolve dates from individual press-release pages:",
            len(undated),
        )
        for item in undated:
            logger.warning("  UNDATED: %s | %s", item.title, item.url)

    return summary


def write_json(items: list[NewsItem], path: Path, dry_run: bool) -> None:
    payload = [item.to_json_dict() for item in items]
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
        cat = f" [{item.category}]" if item.category else ""
        print(f"  {d}  {item.title}{cat}")
        print(f"             {item.url}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
) -> tuple[str, str, str]:
    """Resolve (url, slug, ticker) by consulting sources.yaml.

    Accepts whichever subset of the three the caller supplied and fills in
    the rest from the matching sources.yaml record.  Priority:

      1. slug or ticker given  →  look up record, derive missing fields;
         url built from ir_url + NEWS_PATH if not already provided.
      2. only url given        →  look up record by hostname, derive slug + ticker.
      3. nothing given         →  fall back to Costco defaults so the bare
                                  "python scrape_q4_ir.py" invocation keeps working.

    Returns (url, slug, ticker) as plain strings (never None).
    Logs warnings for any fields that could not be resolved.
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
                "No sources.yaml record found for '%s'. Using provided slug/ticker as-is.", query
            )
        else:
            slug = slug or record.get("slug", "")
            ticker = ticker or record.get("ticker", "")
            if not url:
                ir_url = record.get("ir_url", "")
                if ir_url:
                    url = ir_url.rstrip("/") + NEWS_PATH
                else:
                    logger.warning(
                        "Record '%s' has no ir_url; cannot derive --url automatically.", slug
                    )
    elif url:
        record = find_source_by_ir_url(sources, url) if sources else None
        if record is None:
            logger.warning(
                "No sources.yaml record matched the host of '%s'. "
                "Slug and ticker will be empty unless passed explicitly.",
                url,
            )
        else:
            slug = record.get("slug", "")
            ticker = record.get("ticker", "")
    else:
        slug, ticker, url = DEFAULT_SLUG, DEFAULT_TICKER, DEFAULT_URL

    if not slug:
        logger.warning("Slug is empty; CSV rows will have an empty slug column.")
    if not ticker:
        logger.warning("Ticker is empty; CSV rows will have an empty ticker column.")

    return url, slug, ticker


def parse_year_list(args: argparse.Namespace) -> Optional[set[int]]:
    """Return the set of years to scrape, or None if no year filter was given.

    Normalises the three year-related args (--year, --start-year, --end-year)
    into one set so callers don't have to reason about all three.
    """
    # Normalise half-specified ranges before building the set.
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


def scrape_all_years(
    url: str,
    slug: str,
    ticker: str,
    years: Optional[set[int]],
    args: argparse.Namespace,
) -> list[NewsItem]:
    """Render the listing page for each requested year and collect all NewsItems.

    When no year filter is active, a single render of the default view is done.
    Multiple years are separated by --polite-delay to avoid hammering the site.
    Returns the combined, unfiltered list from all year passes.
    """
    years_to_visit: list[Optional[int]] = sorted(years) if years else [None]
    all_items: list[NewsItem] = []

    for i, year in enumerate(years_to_visit):
        if i > 0:
            time.sleep(args.polite_delay)

        debug_path = args.debug_dump_html
        if debug_path and len(years_to_visit) > 1:
            debug_path = debug_path.with_name(f"{debug_path.stem}_{year}{debug_path.suffix}")

        html = render_news_page(
            url=url,
            year=year,
            category=args.category,
            headless=args.headless,
            browser_channel=args.browser_channel,
            timeout_ms=args.timeout,
            change_timeout_ms=args.change_timeout,
            polite_delay=args.polite_delay,
            max_load_more=args.max_load_more,
            debug_dump_html=debug_path,
        )
        items = parse_news_items(html, base_url=url, slug=slug, ticker=ticker)
        all_items.extend(items)

    return all_items


def print_csv_summary(summary: dict, data_dir: Path, dry_run: bool, filtered: list[NewsItem]) -> None:
    """Print the one-line CSV write summary to stdout."""
    action = "Would write" if dry_run else "Wrote"
    dated_file_count = (
        summary["files_written"]
        if not dry_run
        else len({i.publish_date for i in filtered if i.publish_date})
    )
    skipped = f" ({summary['undated']} undated item(s) skipped)" if summary["undated"] else ""
    print(
        f"{action} {summary['rows_added']} new + {summary['rows_updated']} updated row(s) "
        f"across {dated_file_count} daily CSV file(s) under {data_dir}{skipped}"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    source = parser.add_argument_group("source")
    source.add_argument(
        "--url", default=None,
        help=(
            "IR news page URL. If omitted, derived from sources.yaml via --slug or --ticker. "
            f"Falls back to {DEFAULT_URL} if none of the three are given."
        ),
    )
    source.add_argument(
        "--slug", default=None,
        help="sources.yaml slug. Looked up from sources.yaml when --url or --ticker is given.",
    )
    source.add_argument(
        "--ticker", default=None,
        help="Ticker symbol. Looked up from sources.yaml when --url or --slug is given.",
    )

    filt = parser.add_argument_group("filtering")
    filt.add_argument(
        "--year", type=int, action="append",
        help="Year to scrape. Repeatable: --year 2024 --year 2025",
    )
    filt.add_argument("--start-year", type=int, help="Start of an inclusive year range")
    filt.add_argument("--end-year", type=int, help="End of an inclusive year range")
    filt.add_argument(
        "--since", type=lambda s: date.fromisoformat(s),
        help="Only keep items on/after this date (YYYY-MM-DD)",
    )
    filt.add_argument(
        "--until", type=lambda s: date.fromisoformat(s),
        help="Only keep items on/before this date (YYYY-MM-DD)",
    )
    filt.add_argument(
        "--category", default="All News", choices=CATEGORY_CHOICES,
        help="Category filter to apply on the page (default: All News)",
    )
    filt.add_argument("--limit", type=int, help="Keep at most this many items (after sorting by date)")

    out = parser.add_argument_group("output")
    out.add_argument(
        "--format", choices=["csv", "json", "both"], default="csv",
        help="csv = merge into data/YYYY/YYYY-MM-DD.csv files (default); "
             "json = single combined file",
    )
    out.add_argument(
        "--data-dir", type=Path, default=DATA_DIR,
        help=f"Root of the data/ tree for --format csv (default: {DATA_DIR})",
    )
    out.add_argument(
        "--output", type=Path, default=REPO_ROOT / "q4_ir_news.json",
        help="Output path for --format json",
    )
    out.add_argument(
        "--dry-run", action="store_true",
        help="Scrape and show what would be written, but write nothing",
    )

    browser = parser.add_argument_group("browser")
    browser.add_argument(
        "--show-browser", dest="headless", action="store_false", default=True,
        help="Show the browser window instead of running headless (useful for debugging)",
    )
    browser.add_argument(
        "--fallback-to-visible", action="store_true", default=False,
        help=(
            "If the headless run finds zero items (likely blocked by Cloudflare or the IR "
            "server), automatically retry with a visible browser window. Has no effect when "
            "--show-browser is already set. Not suitable for headless CI environments."
        ),
    )
    browser.add_argument(
        "--browser-channel", default="chrome",
        help="Playwright browser channel: chrome, chromium, msedge (default: chrome, reuses system install)",
    )
    browser.add_argument(
        "--timeout", type=int, default=30_000,
        help="Timeout in ms for page navigation and element interactions (default: 30000)",
    )
    browser.add_argument(
        "--change-timeout", type=int, default=8_000,
        help="Timeout in ms to wait for the release list to change after a year/category "
             "switch or load-more click (default: 8000 -- fast AJAX updates, not full reloads)",
    )
    browser.add_argument(
        "--polite-delay", type=float, default=2.0,
        help="Seconds to pause after each in-page interaction and between detail-page "
             "fetches (default: 2.0)",
    )
    browser.add_argument(
        "--max-load-more", type=int, default=20,
        help="Safety cap on load-more / pagination clicks (default: 20)",
    )
    browser.add_argument(
        "--debug-dump-html", type=Path,
        help="Save the final rendered listing-page HTML to this path for inspection",
    )

    fallback = parser.add_argument_group("date fallback")
    fallback.add_argument(
        "--fetch-detail-pages", action="store_true",
        help="For any item whose date was not found on the listing page, fetch its "
             "individual press-release page to extract the date. Required for Q4 themes "
             "that do not embed dates in the listing cards (e.g. CDW). Adds one browser "
             "request per undated item, spaced by --polite-delay.",
    )

    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="-v for INFO, -vv for DEBUG (default: WARNING)",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    level = {0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    url, slug, ticker = resolve_source(args.url, args.slug, args.ticker)
    if not url:
        logger.error("Could not determine a news URL. Pass --url, --slug, or --ticker.")
        return 1
    logger.info("slug=%s  ticker=%s  url=%s", slug, ticker, url)

    years = parse_year_list(args)
    all_items = scrape_all_years(url, slug, ticker, years, args)

    if args.fallback_to_visible and args.headless and not all_items:
        logger.warning(
            "Headless run returned zero items -- likely blocked by Cloudflare or the IR server. "
            "Retrying with a visible browser window (--fallback-to-visible)."
        )
        args.headless = False
        all_items = scrape_all_years(url, slug, ticker, years, args)

    if args.fetch_detail_pages:
        fetch_missing_dates(
            all_items,
            headless=args.headless,
            browser_channel=args.browser_channel,
            timeout_ms=args.timeout,
            polite_delay=args.polite_delay,
        )
    else:
        undated_count = sum(1 for i in all_items if i.publish_date is None)
        if undated_count:
            logger.warning(
                "%d item(s) have no date. Pass --fetch-detail-pages to resolve "
                "them from individual press-release pages.",
                undated_count,
            )

    filtered = filter_items(all_items, years=years, since=args.since, until=args.until, limit=args.limit)
    if not filtered:
        logger.warning("No items matched the requested filters.")

    print_preview(filtered)

    if args.format in ("csv", "both"):
        summary = merge_into_daily_csvs(filtered, args.data_dir, args.dry_run)
        print_csv_summary(summary, args.data_dir, args.dry_run, filtered)

    if args.format in ("json", "both"):
        write_json(filtered, args.output, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())