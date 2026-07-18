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

  1. Listing-page parse (fast, zero extra requests): for each news link, try
     the anchor's own "aria-label" attribute first (some Q4 themes, e.g. CDW,
     render an accessible label like "CDW Reports First Quarter 2026
     Earnings, May 6, 2026" right on the link -- no DOM climbing needed).
     Failing that, walk up to 5 ancestor elements around the link looking for
     a date in the surrounding card text, deduplicating sibling news links by
     href so a card's headline link and its separate "Continue Reading" link
     to the same article aren't mistaken for two different sibling items.
     Between the two, this covers every Q4 theme seen so far (Costco, CDW).

  2. Detail-page fallback (opt-in via --fetch-detail-pages, or automatically
     enabled by a source's "needs_detail_page_dates: true" field in
     sources.yaml): for any item still missing a date after stage 1, fetch
     its individual detail page and parse the date from there. Fetches are
     spaced by --polite-delay. Each detail page is loaded in the same
     already-open browser session to avoid repeated launch overhead. Kept
     around as a safety net for future Q4 themes whose listing page truly
     omits the date anywhere in stage 1's reach.

Examples:
    # Costco -- dates found on listing page, no detail fetches needed
    python src/scrape_q4_ir.py --dry-run

    # CDW -- dates come from the news link's aria-label on the listing page;
    # no --fetch-detail-pages needed
    python src/scrape_q4_ir.py --slug cdw --dry-run
    python src/scrape_q4_ir.py --ticker CDW --dry-run

    # A source whose listing page truly omits dates still needs the flag
    # passed explicitly (or "needs_detail_page_dates: true" in sources.yaml)
    python src/scrape_q4_ir.py \\
        --url https://investor.example.com/news/default.aspx \\
        --fetch-detail-pages --dry-run

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

from utils.scrape_utils import (
    NewsItem as _BaseNewsItem,
    add_common_args,
    finalize_and_output,
    parse_date,
    parse_year_args,
    print_preview as _base_print_preview,
)
from utils.q4_link_pattern import (
    DEFAULT_NEWS_DETAILS_SEGMENT,
    DEFAULT_NEWS_PATH,
    q4_news_link_re,
    q4_news_link_selector,
    strip_year_placeholder,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_URL = "https://investor.costco.com/news/default.aspx"
DEFAULT_SLUG = "costco"
DEFAULT_TICKER = "COST"

# DEFAULT_NEWS_PATH: listing-page path appended to a slug/ticker-derived
# ir_url. Most Q4 themes (Costco, CDW) use a fixed listing URL and select
# the year via an in-page dropdown instead (see _try_select_year()). Some
# themes (e.g. Netflix) embed the year directly in the listing URL's path;
# for those, sources.yaml's "news_path" field (or --news-path) should
# contain a "{year}" placeholder segment, e.g.
# "investor-news-and-events/financial-releases/{year}/default.aspx". See
# _resolve_year_url() for how the placeholder is filled in (or dropped when
# no year is requested).
#
# DEFAULT_NEWS_DETAILS_SEGMENT: the "-details" path segment used by
# press-release detail links, e.g. the "news-details" in
# /news/news-details/<year>/<slug>/default.aspx. Most Q4 themes (Costco,
# CDW) share this literal segment; some (e.g. Netflix, whose detail links
# use /investor-news-and-events/financial-releases/
# press-release-details/<year>/<slug>/default.aspx) use a different word.
# Overridable via sources.yaml's "news_details_segment" field or
# --news-details-segment.
#
# Both constants, and the link-matching logic below, live in
# utils/q4_link_pattern.py, shared with src/reporting/detect_ir_platform.py
# (which fingerprints a source's IR platform using this same link shape).

CATEGORY_CHOICES = ["All News", "Sales Releases", "Earnings Releases", "Other Company Releases"]


def _news_link_matcher(details_segment: str) -> tuple[re.Pattern, str]:
    """Build the (regex, CSS selector) pair that identifies a press-release
    detail link for one source's Q4 theme, e.g. for the default
    "news-details" segment: matches /news/news-details/<year>/<slug>
    [/default.aspx] on any Q4 IR hostname.

    Thin wrapper around utils.q4_link_pattern's shared builders.
    """
    return q4_news_link_re(details_segment), q4_news_link_selector(details_segment)


def _resolve_year_url(url_template: str, year: Optional[int]) -> str:
    """Fill in (or drop) the "{year}" path placeholder in a listing URL.

    Themes whose listing URL is year-specific (e.g. Netflix) put "{year}" in
    their news_path template. When a concrete `year` is requested, it's
    substituted directly. When `year` is None -- no --year/--start-year was
    given, or this theme selects the year via an in-page dropdown instead
    (no "{year}" in the template at all) -- the "{year}/" segment is dropped,
    falling back to the theme's undated default listing (which, for Netflix,
    happens to show the current year).
    """
    if "{year}" not in url_template:
        return url_template
    if year is not None:
        return url_template.format(year=year)
    return strip_year_placeholder(url_template)


# Default pair, used as a fallback default arg where a per-source one hasn't
# been resolved yet (main() always resolves and passes a source-specific
# pair explicitly; see resolve_source()).
NEWS_LINK_RE_DEFAULT, NEWS_LINK_SELECTOR_DEFAULT = _news_link_matcher(DEFAULT_NEWS_DETAILS_SEGMENT)

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
        d["publish_date"] = self.publish_date_str
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


def _current_news_hrefs(page: Page, link_selector: str) -> set[str]:
    return set(page.locator(link_selector).evaluate_all("els => els.map(e => e.getAttribute('href'))"))


def _wait_for_news_links(page: Page, timeout_ms: int, link_selector: str) -> None:
    """Wait for at least one press-release link to appear in the DOM.

    Uses direct selector polling rather than networkidle, which is unreliable
    on SPA content (analytics / consent scripts keep the network busy after the
    content widget has already finished rendering).
    """
    try:
        page.wait_for_selector(link_selector, timeout=timeout_ms, state="attached")
    except PlaywrightTimeoutError:
        logger.warning(
            "Timed out after %dms waiting for '%s'. Page may be slow or blocked; "
            "continuing so --debug-dump-html can still capture what loaded.",
            timeout_ms, link_selector,
        )


def _wait_for_list_change(
    page: Page,
    previous_hrefs: set[str],
    timeout_ms: int,
    link_selector: str,
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
        current = _current_news_hrefs(page, link_selector)
        if current != previous_hrefs:
            time.sleep(settle_ms / 1000)
            return _current_news_hrefs(page, link_selector)
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
    link_selector: str = NEWS_LINK_SELECTOR_DEFAULT,
) -> str:
    """Drive Chrome to the listing page, apply filters, expand pagination, and
    return the fully rendered HTML.

    `year` here only drives the in-page year dropdown (_try_select_year);
    pass None when the year is instead already baked into `url` (see
    _resolve_year_url() / scrape_all_years()) so this doesn't also try --
    redundantly and noisily -- to click a dropdown control that a
    year-in-path theme like Netflix's doesn't have.
    """
    with sync_playwright() as p:
        browser, page = _launch_browser(p, headless, browser_channel, timeout_ms)

        logger.info("Loading %s ...", url)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        _wait_for_news_links(page, timeout_ms, link_selector)

        if category and category != "All News":
            logger.info("Selecting category: %s", category)
            before = _current_news_hrefs(page, link_selector)
            if _try_select_category(page, category, timeout_ms):
                time.sleep(polite_delay)
                _wait_for_list_change(page, before, change_timeout_ms, link_selector)
            else:
                logger.warning(
                    "Could not apply category filter '%s'; continuing with default view.", category
                )

        if year is not None:
            logger.info("Selecting year: %s", year)
            before = _current_news_hrefs(page, link_selector)
            if _try_select_year(page, year, timeout_ms):
                time.sleep(polite_delay)
                _wait_for_list_change(page, before, change_timeout_ms, link_selector)
            else:
                logger.warning(
                    "Could not find/apply year filter for %s. Falling back to default view "
                    "and filtering by year client-side.",
                    year,
                )

        clicks = 0
        while clicks < max_load_more:
            before = _current_news_hrefs(page, link_selector)
            if not _click_load_more(page, timeout_ms):
                break
            time.sleep(polite_delay)
            after = _wait_for_list_change(page, before, change_timeout_ms, link_selector)
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

def parse_news_items(
    html: str,
    base_url: str,
    slug: str,
    ticker: str,
    link_re: re.Pattern = NEWS_LINK_RE_DEFAULT,
) -> list[NewsItem]:
    """Extract news items from the rendered listing-page HTML.

    Date extraction tries two sources, in order:

      1. The news anchor's own `aria-label` attribute, if present. Some Q4
         themes (e.g. CDW) render an accessible label like "CDW Reports
         First Quarter 2026 Earnings, May 6, 2026" directly on the link --
         the date is right there, scoped to exactly this item, with no DOM
         climbing needed. parse_date() finds the trailing "Month Day, Year"
         even when the headline text itself contains commas.

      2. Walk up to 5 ancestor elements looking for a date (and category) in
         the surrounding card. Stops climbing as soon as the ancestor
         contains more than one *distinct* news-item URL -- that means we've
         crossed into the shared list wrapper and would pick up sibling
         items' dates. Sibling links are deduplicated by href before this
         count: several Q4 themes (CDW included) render both a headline link
         and a separate "Continue Reading" link pointing at the same
         article within one card, and without deduping, that pair alone
         looks like "more than one news item" and stops the climb one level
         too early -- one level before the ancestor that actually holds the
         date text.

    This works on Q4 themes that embed the date in each card (e.g. Costco).
    For themes where neither stage finds anything, items come back with
    publish_date=None; use fetch_missing_dates() to fill them in from
    individual detail pages.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not link_re.search(href):
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

        aria_label = anchor.get("aria-label", "")
        if aria_label:
            publish_date, raw_date_text = parse_date(aria_label)

        node = anchor
        for _ in range(5):
            node = node.find_parent(["li", "article", "div", "section"])
            if node is None:
                break
            sibling_hrefs = {
                a["href"] for a in node.find_all("a", href=True)
                if link_re.search(a["href"])
            }
            if len(sibling_hrefs) > 1:
                break
            # Exclude the headline anchor's own text from the date search.
            # A headline can itself mention an unrelated date (e.g. "...
            # Schedules Special Meeting for March 20, 2026, to Approve...")
            # that has nothing to do with the release's actual publish date.
            # parse_date() takes the first match it finds, so if that
            # in-headline date happens to sit earlier in reading order than
            # the card's real dateline (e.g. a "Feb 17, 2026" node rendered
            # after the headline), it would be picked by mistake. Searching
            # only the surrounding card text -- not the anchor's own label --
            # keeps the headline's own wording out of the running entirely.
            own_text_nodes = set(anchor.find_all(string=True))
            context_text = " ".join(
                s.strip() for s in node.find_all(string=True)
                if s not in own_text_nodes and s.strip()
            )
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

# CSV/JSON writing and the "Wrote N new + M updated ..." summary line are
# handled by scrape_utils.finalize_and_output(), shared with
# scrape_investorroom.py and scrape_notified.py. Called directly from
# main() below -- see there for the undated-item warning that precedes it.


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
    fetch_detail_pages: Optional[bool] = None,
    news_path: Optional[str] = None,
    news_details_segment: Optional[str] = None,
) -> tuple[str, str, str, bool, re.Pattern, str]:
    """Resolve (url, slug, ticker, fetch_detail_pages, link_re, link_selector)
    by consulting sources.yaml.

    Thin Q4-specific wrapper around sources_utils.resolve_source_identity():
    Q4 sites want one complete listing URL, so a URL derived from a slug/
    ticker lookup has news_path appended (listing_path_suffix); see that
    function's docstring for the full priority order (slug/ticker -> url ->
    Costco defaults). The returned url may still contain a literal "{year}"
    placeholder segment (see _resolve_year_url()) for a source whose
    news_path template has one (e.g. Netflix) -- it's resolved per-year later,
    in scrape_all_years(), not here.

    news_path precedence (highest wins):
      1. the news_path argument (i.e. --news-path on the CLI)
      2. the "news_path" field on the matched sources.yaml record
      3. DEFAULT_NEWS_PATH ("news/default.aspx")

    Because news_path affects how resolve_source_identity builds the URL
    (it's passed as that function's listing_path_suffix), and *that* function
    is what looks up the matching sources.yaml record, resolving news_path
    from the record requires peeking at the record ourselves first when only
    slug/ticker was given -- a cheap, read-only re-lookup (see
    sources_utils.find_source/load_sources) before delegating the rest of
    the resolution (--url precedence, defaults, warnings) to
    resolve_source_identity as usual.

    news_details_segment precedence follows the same pattern (CLI arg >
    sources.yaml field > DEFAULT_NEWS_DETAILS_SEGMENT ("news-details")), and
    is used to build the (link_re, link_selector) pair that identifies
    press-release detail links on this source's listing page.

    fetch_detail_pages precedence (highest wins):
      1. the fetch_detail_pages argument (i.e. --fetch-detail-pages on the CLI)
      2. the "needs_detail_page_dates" field on the matched sources.yaml record
      3. False

    Returns (url, slug, ticker, fetch_detail_pages, link_re, link_selector).
    url/slug/ticker are plain strings (never None); fetch_detail_pages is a
    plain bool. Logs warnings for any fields that could not be resolved.
    """
    from utils.sources_utils import find_source, find_source_by_ir_url, load_sources, resolve_source_identity

    peeked_record: Optional[dict] = None
    try:
        sources = load_sources()
        if slug or ticker:
            # Match resolve_source_identity()'s field-strict lookup below: a
            # --slug value is only checked against records' slug field, a
            # --ticker value only against ticker, and slug takes priority if
            # both are given. Otherwise this peek could land on a different
            # record than the one actually resolved a few lines down.
            if slug:
                peeked_record = find_source(sources, slug, field="slug")
            else:
                peeked_record = find_source(sources, ticker, field="ticker")
        elif url:
            peeked_record = find_source_by_ir_url(sources, url)
    except Exception as exc:
        logger.warning("Could not pre-load sources.yaml (%s); using defaults.", exc)

    if not news_path:
        news_path = (peeked_record.get("news_path") if peeked_record else None) or DEFAULT_NEWS_PATH
    if not news_details_segment:
        news_details_segment = (
            (peeked_record.get("news_details_segment") if peeked_record else None)
            or DEFAULT_NEWS_DETAILS_SEGMENT
        )

    url, slug, ticker, record = resolve_source_identity(
        url, slug, ticker,
        default_slug=DEFAULT_SLUG, default_ticker=DEFAULT_TICKER, default_url=DEFAULT_URL,
        listing_path_suffix=news_path, logger=logger,
    )

    # fetch_detail_pages precedence: explicit CLI flag > sources.yaml field > False.
    if fetch_detail_pages is None:
        fetch_detail_pages = bool(record.get("needs_detail_page_dates")) if record else False

    link_re, link_selector = _news_link_matcher(news_details_segment)

    return url, slug, ticker, fetch_detail_pages, link_re, link_selector


def scrape_all_years(
    url_template: str,
    slug: str,
    ticker: str,
    years: Optional[set[int]],
    args: argparse.Namespace,
    link_re: re.Pattern,
    link_selector: str,
) -> list[NewsItem]:
    """Render the listing page for each requested year and collect all NewsItems.

    `url_template` may contain a "{year}" placeholder segment (see
    _resolve_year_url()) for themes whose listing URL is year-specific (e.g.
    Netflix); it's filled in (or dropped) separately for each year visited.
    For themes without the placeholder (e.g. Costco/CDW), the same URL is
    used for every year and the year is instead selected via an in-page
    dropdown inside render_news_page().

    When no year filter is active, a single render of the default view is done.
    Multiple years are separated by --polite-delay to avoid hammering the site.
    Returns the combined, unfiltered list from all year passes.
    """
    years_to_visit: list[Optional[int]] = sorted(years) if years else [None]
    all_items: list[NewsItem] = []
    uses_year_in_path = "{year}" in url_template

    for i, year in enumerate(years_to_visit):
        if i > 0:
            time.sleep(args.polite_delay)

        url = _resolve_year_url(url_template, year)

        debug_path = args.debug_dump_html
        if debug_path and len(years_to_visit) > 1:
            debug_path = debug_path.with_name(f"{debug_path.stem}_{year}{debug_path.suffix}")

        html = render_news_page(
            url=url,
            # If the year is already baked into `url`, don't also pass it
            # through to the in-page dropdown selector -- that control
            # doesn't exist on year-in-path themes like Netflix's, and
            # attempting it just logs a spurious "could not find/apply
            # year filter" warning.
            year=None if uses_year_in_path else year,
            category=args.category,
            headless=args.headless,
            browser_channel=args.browser_channel,
            timeout_ms=args.timeout,
            change_timeout_ms=args.change_timeout,
            polite_delay=args.polite_delay,
            max_load_more=args.max_load_more,
            debug_dump_html=debug_path,
            link_selector=link_selector,
        )
        items = parse_news_items(html, base_url=url, slug=slug, ticker=ticker, link_re=link_re)
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

    source = parser.add_argument_group("source")
    source.add_argument(
        "--news-path", default=None, metavar="PATH",
        help=(
            "Listing path appended to a slug/ticker-derived ir_url, e.g. "
            "'investor-news-and-events/financial-releases/{year}/default.aspx' "
            "(default: 'news/default.aspx'). Include a literal '{year}' path "
            "segment for themes whose listing URL is year-specific (e.g. "
            "Netflix); it's filled in per --year, or dropped for the default "
            "undated view. Overrides sources.yaml's news_path field for this "
            "run; most sites don't need this."
        ),
    )
    source.add_argument(
        "--news-details-segment", default=None, metavar="SEGMENT",
        help=(
            "Path segment used by this theme's press-release detail links in "
            "place of 'news-details', e.g. 'press-release-details' for "
            "Netflix (default: 'news-details'). Overrides sources.yaml's "
            "news_details_segment field for this run; most sites don't need this."
        ),
    )

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
        "--fetch-detail-pages", dest="fetch_detail_pages", action="store_true", default=None,
        help="For any item whose date was not found on the listing page, fetch its "
             "individual press-release page to extract the date. Required for Q4 themes "
             "that do not embed dates in the listing cards (e.g. CDW). Adds one browser "
             "request per undated item, spaced by --polite-delay. Defaults to the "
             "'needs_detail_page_dates' field on the matched sources.yaml record if not "
             "passed explicitly; pass this flag to force it on for a source that doesn't "
             "have that field set.",
    )
    fallback.add_argument(
        "--no-fetch-detail-pages", dest="fetch_detail_pages", action="store_false",
        help="Force detail-page date fetching off, overriding a "
             "'needs_detail_page_dates: true' sources.yaml field for this source.",
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

    url, slug, ticker, fetch_detail_pages, link_re, link_selector = resolve_source(
        args.url, args.slug, args.ticker, args.fetch_detail_pages,
        args.news_path, args.news_details_segment,
    )
    if not url:
        logger.error("Could not determine a news URL. Pass --url, --slug, or --ticker.")
        return 1
    logger.info(
        "slug=%s  ticker=%s  url=%s  fetch_detail_pages=%s", slug, ticker, url, fetch_detail_pages
    )

    years = parse_year_args(args)
    all_items = scrape_all_years(url, slug, ticker, years, args, link_re, link_selector)

    if args.fallback_to_visible and args.headless and not all_items:
        logger.warning(
            "Headless run returned zero items -- likely blocked by Cloudflare or the IR server. "
            "Retrying with a visible browser window (--fallback-to-visible)."
        )
        args.headless = False
        all_items = scrape_all_years(url, slug, ticker, years, args, link_re, link_selector)

    if fetch_detail_pages:
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

    # Filters, always previews (with the category column, via print_preview
    # above), and writes CSV/JSON per --format; see finalize_and_output()'s
    # docstring for the three behaviors this standardizes across
    # scrape_q4_ir.py/scrape_investorroom.py/scrape_notified.py
    # (preview-always, --format both, --output default path).
    filtered = finalize_and_output(
        all_items,
        years=years, since=args.since, until=args.until, limit=args.limit,
        format=args.format, output=args.output, dry_run=args.dry_run,
        data_dir=args.data_dir,
        default_json_path=REPO_ROOT / "q4_ir_news.json",
        preview_fn=print_preview,
    )
    if not filtered:
        logger.warning("No items matched the requested filters.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())