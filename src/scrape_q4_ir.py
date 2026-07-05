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
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
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

from utils.csv_utils import merge_items_into_daily_csvs, print_merge_summary
from utils.scrape_utils import (
    NewsItem as _BaseNewsItem,
    add_common_args,
    filter_items,
    parse_date,
    parse_year_args,
    print_preview as _base_print_preview,
    write_json,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_URL = "https://investor.costco.com/news/default.aspx"
DEFAULT_SLUG = "costco"
DEFAULT_TICKER = "COST"

NEWS_PATH = "/news/default.aspx"

CATEGORY_CHOICES = ["All News", "Sales Releases", "Earnings Releases", "Other Company Releases"]

# Matches /news/news-details/<year>/<slug>[/default.aspx] on any Q4 IR hostname.
NEWS_LINK_RE = re.compile(r"/news/news-details/\d{4}/[^/]+/?(?:default\.aspx)?", re.IGNORECASE)
NEWS_LINK_SELECTOR = "a[href*='/news/news-details/']"

logger = logging.getLogger("scrape_q4_ir")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem(_BaseNewsItem):
    """Q4-specific press-release item, extending the shared base with a category."""

    category: str = ""

    def to_csv_row(self) -> dict:
        """Alias for to_row() kept for backward compatibility."""
        return self.to_row()

    def to_json_dict(self) -> dict:
        d = asdict(self)
        d["publish_date"] = self.publish_datetime
        return d


def _find_category_near(text: str) -> str:
    for cat in CATEGORY_CHOICES:
        if cat != "All News" and cat in text:
            return cat
    return ""


def print_preview(items: Iterable[NewsItem]) -> None:
    """Print a preview of Q4 items, including the category field."""
    _base_print_preview(items, show_category=True)


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
                publish_date, raw_date_text = parse_date(context_text)
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
    press-release <h3> title -- e.g.::

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
        d, _ = parse_date(dt_attr or tag.get_text())
        if d:
            return d

    # Fall back to scanning the first ~4 KB of page text. The date is always
    # near the top of a detail page; stopping early avoids false matches from
    # body text ("Q1 2026 results... March 31, 2026 balance sheet...").
    body_text = soup.get_text(" ", strip=True)[:4096]
    d, _ = parse_date(body_text)
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
# Output: CSV (primary_wire daily files) and JSON
# ---------------------------------------------------------------------------

# merge_into_daily_csvs() and the CSV-write summary line are handled by
# csv_utils.merge_items_into_daily_csvs() / print_merge_summary(), shared
# with scrape_investorroom.py and scrape_notified.py. Called directly from
# main() below -- see there for the undated-item warning and the final
# "Wrote N new + M updated ..." line.


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
) -> tuple[str, str, str]:
    """Resolve (url, slug, ticker) by consulting sources.yaml.

    Thin Q4-specific wrapper around sources_utils.resolve_source_identity():
    Q4 sites want one complete listing URL, so a URL derived from a slug/
    ticker lookup has NEWS_PATH appended (listing_path_suffix); see that
    function's docstring for the full priority order (slug/ticker -> url ->
    Costco defaults).

    Returns (url, slug, ticker) as plain strings (never None).
    Logs warnings for any fields that could not be resolved.
    """
    from utils.sources_utils import resolve_source_identity

    url, slug, ticker, _record = resolve_source_identity(
        url, slug, ticker,
        default_slug=DEFAULT_SLUG, default_ticker=DEFAULT_TICKER, default_url=DEFAULT_URL,
        listing_path_suffix=NEWS_PATH, logger=logger,
    )
    return url, slug, ticker


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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Shared: --url/--slug/--ticker, year/date filters, --format/--output/--dry-run
    add_common_args(parser)

    # Q4-specific filtering
    filt = parser.add_argument_group("filtering (q4-specific)")
    filt.add_argument(
        "--category", default="All News", choices=CATEGORY_CHOICES,
        help="Category filter to apply on the page (default: All News)",
    )
    filt.add_argument("--limit", type=int, help="Keep at most this many items (after sorting by date)")

    # Override --data-dir default (csv_utils default is fine for investorroom, but
    # q4 script historically exposed it explicitly)
    out = parser.add_argument_group("output (q4-specific)")
    out.add_argument(
        "--data-dir", type=Path, default=DATA_DIR,
        help=f"Root of the data/ tree for --format csv (default: {DATA_DIR})",
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

    import logging as _logging
    level = {0: _logging.WARNING, 1: _logging.INFO}.get(args.verbose, _logging.DEBUG)
    _logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    url, slug, ticker = resolve_source(args.url, args.slug, args.ticker)
    if not url:
        logger.error("Could not determine a news URL. Pass --url, --slug, or --ticker.")
        return 1
    logger.info("slug=%s  ticker=%s  url=%s", slug, ticker, url)

    years = parse_year_args(args)
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
        summary = merge_items_into_daily_csvs(filtered, args.data_dir, args.dry_run)
        print_merge_summary(summary, args.dry_run, filtered, data_dir=args.data_dir)

    if args.format in ("json", "both"):
        output = args.output or REPO_ROOT / "q4_ir_news.json"
        write_json(filtered, output, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())