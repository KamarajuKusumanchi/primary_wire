"""
src/utils/scrape_aem_utils.py

Shared helpers for primary_wire's AEM-family scrapers
(scrape_aem_cme.py, scrape_aem_bny.py -- and any future AEM-platform
listing scraper).

These two scrapers were deliberately split out of a single former
scrape_aem.py because their *site-specific* logic (item-card markup,
pagination widgets, year-filtering UI) turned out to share basically
nothing -- see scrape_aem_cme.py's and scrape_aem_bny.py's module
docstrings for that history, including the pagination-selector bug that
merging the two sites' cascades into one list caused.

What's collected here is the opposite half: the generic, Playwright-
driven browser plumbing and small parsing helpers that both files had
implemented identically (word-for-word aside from comments) once split
apart. None of it encodes CME- or BNY-specific markup; every function
below takes whatever site-specific bit it needs (an item selector, a
list of date-label CSS classes, a set of defaults) as a parameter.

Not shared with scrape_q4_ir.py: Q4's own _launch_browser() etc. are
similar in spirit but genuinely different in detail (no Akamai
bot-mitigation workaround needed there), so folding it in here would
trade a real duplication for a false one. Revisit if Q4 ever needs the
same browser-context hardening.

Public API
----------
DESKTOP_CHROME_UA, STEALTH_INIT_SCRIPT  -- browser-context constants
launch_browser(...)          -> (Browser, Page)
goto_with_retry(...)         -- page.goto() with transient-error retries
current_item_hrefs(...)      -> set[str]
wait_for_items(...)          -- wait for the item-selector to appear
wait_for_list_change(...)    -> set[str]
dump_path_for_page(...)      -> Optional[Path]
first_link_in(container)     -> the container's own/first descendant <a>
extract_title(...)           -> str
is_bare_date_text(...)       -> bool
extract_item_date(...)       -> (Optional[date], str)
resolve_publish_date(...)    -> (Optional[date], str)
fetch_missing_dates(...)     -- fill in missing dates via detail-page fetches
resolve_source(...)          -> (url, slug, ticker, item_selector)
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from utils.scrape_utils import NewsItem, extract_date_from_detail_html, parse_date

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def launch_browser(p, headless: bool, browser_channel: str, timeout_ms: int):
    """Launch a Chromium browser and return a configured (Browser, Page).

    Both cmegroup.com and bny.com run bot mitigation (Akamai) in front of
    their press-release pages. Getting past it in headless mode takes more
    than just "launch Chromium and go":
      1. --disable-http2: without this, the very first navigation fails
         outright with net::ERR_HTTP2_PROTOCOL_ERROR -- the HTTP/2
         fingerprint check RSTs the stream before any HTML comes back.
      2. --disable-blink-features=AutomationControlled: removes one of the
         standard CDP-automation tells from the renderer.
      3. A real desktop UA + matching viewport/locale/timezone/headers, and
         patching navigator.webdriver via an init script: with HTTP/2 out of
         the way, an unconfigured automated browser doesn't get RST anymore,
         it just gets silently stalled (hangs past the goto timeout) -- the
         JS challenge either never resolves or the response is withheld.
         Looking as close to a normal desktop Chrome session as possible is
         what gets a response back at all.
    None of this guarantees headless will get through -- if it still
    doesn't, --show-browser / --fallback-to-visible is each scraper's
    documented fallback.
    """
    launch_kwargs: dict = {
        "headless": headless,
        "args": ["--disable-http2", "--disable-blink-features=AutomationControlled"],
    }
    if browser_channel:
        launch_kwargs["channel"] = browser_channel
    browser = p.chromium.launch(**launch_kwargs)
    context = browser.new_context(
        user_agent=DESKTOP_CHROME_UA,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    context.add_init_script(STEALTH_INIT_SCRIPT)
    page = context.new_page()
    page.set_default_timeout(timeout_ms)
    return browser, page


def goto_with_retry(page: Page, url: str, timeout_ms: int, *, retries: int = 2) -> None:
    """page.goto() with a couple of retries on transient network-level
    failures (net::ERR_* -- connection resets, protocol errors, etc.), as
    opposed to PlaywrightTimeoutError which already gets its own handling
    elsewhere. Bot mitigation occasionally drops the very first connection
    attempt from a fresh browser context even once HTTP/2 is disabled (see
    launch_browser()), so a bare retry with a short pause clears most of
    these without needing a whole new browser instance.
    """
    last_error: Optional[PlaywrightError] = None
    for attempt in range(1, retries + 2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except PlaywrightTimeoutError:
            raise
        except PlaywrightError as exc:
            last_error = exc
            logger.warning(
                "Navigation to %s failed (attempt %d/%d): %s", url, attempt, retries + 1, exc,
            )
            if attempt <= retries:
                time.sleep(2.0)
    assert last_error is not None
    raise last_error


def current_item_hrefs(page: Page, item_selector: str) -> set[str]:
    """Return the set of detail-page hrefs currently rendered, one per
    matched item-selector element (resolving to that element's own href if
    it's an anchor, else its first descendant anchor's href)."""
    return set(
        href for href in page.locator(item_selector).evaluate_all(
            """els => els.map(e => {
                const a = e.tagName === 'A' ? e : e.querySelector('a[href]');
                return a ? a.getAttribute('href') : null;
            })"""
        )
        if href
    )


def wait_for_items(page: Page, timeout_ms: int, item_selector: str) -> None:
    """Wait for at least one item-selector match to appear in the DOM.

    Uses direct selector polling rather than networkidle, which is
    unreliable on JS-rendered content -- same rationale as
    scrape_q4_ir.py's _wait_for_news_links().
    """
    try:
        page.wait_for_selector(item_selector, timeout=timeout_ms, state="attached")
    except PlaywrightTimeoutError:
        logger.warning(
            "Timed out after %dms waiting for '%s'. The page may be slow, blocked, or use "
            "markup this scraper's selector cascade doesn't recognize -- continuing so "
            "--debug-dump-html can still capture what actually loaded.",
            timeout_ms, item_selector,
        )


def wait_for_list_change(
    page: Page, previous_hrefs: set[str], timeout_ms: int, item_selector: str,
    poll_interval_ms: int = 200, settle_ms: int = 400,
) -> set[str]:
    """Poll until the rendered item-href set differs from *previous_hrefs*,
    then return the new set."""
    deadline = time.monotonic() + timeout_ms / 1000
    current = previous_hrefs
    while time.monotonic() < deadline:
        current = current_item_hrefs(page, item_selector)
        if current != previous_hrefs:
            time.sleep(settle_ms / 1000)
            return current_item_hrefs(page, item_selector)
        time.sleep(poll_interval_ms / 1000)
    logger.warning(
        "Item list did not change within %dms after the last action. Proceeding with "
        "whatever is currently rendered (expected if this was the last page).",
        timeout_ms,
    )
    return current


def dump_path_for_page(debug_dump_html: Optional[Path], page_num: int) -> Optional[Path]:
    """Return the path to save page *page_num*'s HTML to, or None if
    --debug-dump-html wasn't passed. Page 1 keeps the exact path given;
    later pages get a "_page{N}" suffix inserted before the extension --
    useful whether a site appends cards across pages (CME) or replaces the
    DOM's item list each page (BNY, where a single dump file could
    otherwise only ever show one page at a time)."""
    if debug_dump_html is None:
        return None
    if page_num == 1:
        return debug_dump_html
    return debug_dump_html.with_name(f"{debug_dump_html.stem}_page{page_num}{debug_dump_html.suffix}")


# ---------------------------------------------------------------------------
# Listing-card parsing helpers
# ---------------------------------------------------------------------------

def first_link_in(container) -> Optional[object]:
    """Return the container itself if it IS an <a>, else its first
    descendant <a href>, else None."""
    if getattr(container, "name", None) == "a" and container.get("href"):
        return container
    return container.find("a", href=True)


def extract_title(container, anchor) -> str:
    """Return the display title for one item's card.

    Prefers the anchor's own text; falls back to a heading element inside
    the container (some AEM teaser themes put the anchor around an image
    thumbnail only, with the actual headline text in a sibling <h2>/<h3>
    "title" element instead of inside the link)."""
    title = anchor.get_text(separator=" ", strip=True)
    if title:
        return title
    heading = container.find(["h1", "h2", "h3", "h4"])
    if heading is not None:
        return heading.get_text(separator=" ", strip=True)
    return ""


def is_bare_date_text(text: str, raw_match: str) -> bool:
    """True if *text* is (almost) entirely *raw_match* and not a longer
    sentence that merely happens to contain a date somewhere in it.

    Same rule scrape_investorroom.py uses -- see its docstring for why a
    naive "first date-like substring in the card" approach is unreliable
    (headlines and summary snippets routinely mention unrelated dates).
    """
    remainder = text.replace(raw_match, "", 1)
    return remainder.strip(" \t\r\n-\u2013\u2014|\u00b7\u2022.,:") == ""


def extract_item_date(container, anchor, item_date_selectors: list[str]) -> tuple[Optional[date], str]:
    """Find a publish date inside one item's card, trying (in order):

      1. A <time> element's `datetime` attribute, then its display text.
      2. Other common date-label CSS classes (*item_date_selectors*).
      3. A bare-date text-node walk of the whole container, excluding any
         text that belongs to the headline anchor itself -- so a headline
         mentioning an unrelated date (e.g. an upcoming earnings call) isn't
         mistaken for the card's real dateline.

    Returns (None, "") if nothing parseable was found in the card at all;
    the caller falls back to a URL-embedded date, and finally to a
    detail-page fetch if --fetch-detail-pages was passed.
    """
    time_tag = container.find("time")
    if time_tag is not None:
        dt_attr = time_tag.get("datetime", "")
        if dt_attr:
            d, raw = parse_date(dt_attr)
            if d:
                return d, raw
        d, raw = parse_date(time_tag.get_text(strip=True))
        if d:
            return d, raw

    for sel in item_date_selectors:
        el = container.select_one(sel)
        if el is None or el is time_tag:
            continue
        d, raw = parse_date(el.get_text(strip=True))
        if d:
            return d, raw

    own_text_nodes = set(anchor.find_all(string=True)) if anchor is not None else set()
    for text_node in container.find_all(string=True):
        if text_node in own_text_nodes:
            continue
        candidate = text_node.strip()
        if not candidate:
            continue
        d, raw = parse_date(candidate)
        if d and is_bare_date_text(candidate, raw):
            return d, raw

    return None, ""


def resolve_publish_date(
    card_date: Optional[date], card_raw_text: str, url_date: Optional[date], url_for_logging: str,
) -> tuple[Optional[date], str]:
    """Reconcile the card's own date against one parsed out of the detail
    URL, preferring the card -- mirroring scrape_investorroom.py's
    resolve_publish_date(): a URL-embedded date isn't guaranteed to match
    the article's real publish date, so it's kept only as a fallback for
    when the card itself has no date.
    """
    if card_date is not None:
        if url_date is not None and url_date != card_date:
            logger.warning(
                "URL date (%s) disagrees with card date (%s) for %s -- using the card date.",
                url_date, card_date, url_for_logging,
            )
        return card_date, card_raw_text
    if url_date is not None:
        return url_date, url_date.isoformat()
    return None, ""


# ---------------------------------------------------------------------------
# Detail-page date fallback
# ---------------------------------------------------------------------------

def fetch_missing_dates(
    items: list[NewsItem], headless: bool, browser_channel: str, timeout_ms: int, polite_delay: float,
) -> None:
    """Fill in publish_date for items the listing-page parse left undated,
    by visiting each one's own detail page in the same browser session.

    Reuses scrape_utils.extract_date_from_detail_html() (shared with
    scrape_investorroom.py/scrape_notified.py) for the actual date-in-HTML
    heuristics, but fetches via a real Playwright page load rather than a
    plain HTTP GET, since AEM detail pages are typically client-rendered
    just like the listing page. Modifies *items* in place.
    """
    undated = [item for item in items if item.publish_date is None]
    if not undated:
        return

    logger.info("Fetching detail pages for %d undated item(s) to resolve publish dates ...", len(undated))

    with sync_playwright() as p:
        browser, page = launch_browser(p, headless, browser_channel, timeout_ms)

        for i, item in enumerate(undated):
            if i > 0:
                time.sleep(polite_delay)
            logger.info("  [%d/%d] %s", i + 1, len(undated), item.url)
            try:
                goto_with_retry(page, item.url, timeout_ms)
                try:
                    page.wait_for_selector("h1, h2, h3, time", timeout=timeout_ms, state="visible")
                except PlaywrightTimeoutError:
                    pass
                html = page.content()
                d, raw = extract_date_from_detail_html(html)
                if d:
                    item.publish_date = d
                    item.raw_date_text = raw or f"(detail page: {d.isoformat()})"
                    logger.debug("    -> %s", d)
                else:
                    logger.warning("    -> no date found on detail page: %s", item.url)
            except Exception as exc:
                logger.warning("    -> failed to fetch %s: %s", item.url, exc)

        browser.close()

    still_missing = sum(1 for item in undated if item.publish_date is None)
    if still_missing:
        logger.warning(
            "%d item(s) still have no date after detail-page fetch; they will be "
            "skipped in CSV output.", still_missing,
        )


# ---------------------------------------------------------------------------
# Source resolution (sources.yaml integration)
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str], slug: Optional[str], ticker: Optional[str],
    listing_path: Optional[str], item_selector: Optional[str],
    *,
    default_item_selector: str, default_slug: str, default_ticker: str, default_url: str,
) -> tuple[str, str, str, str]:
    """Resolve (url, slug, ticker, item_selector) from CLI args and
    sources.yaml.

    listing_path precedence (highest wins):
      1. --listing-path on the CLI
      2. an "aem_listing_path" field on the matched sources.yaml record
      3. "" (use the resolved URL as-is)

    item_selector precedence (highest wins):
      1. --item-selector on the CLI
      2. an "aem_item_selector" field on the matched sources.yaml record
      3. *default_item_selector*

    Returns (url, slug, ticker, item_selector); url/slug/ticker are plain
    strings (never None).
    """
    from utils.sources_utils import find_source, find_source_by_url, load_sources, resolve_source_identity

    peeked_record: Optional[dict] = None
    try:
        sources = load_sources()
        if slug:
            peeked_record = find_source(sources, slug, field="slug")
        elif ticker:
            peeked_record = find_source(sources, ticker, field="ticker")
        elif url:
            peeked_record = find_source_by_url(sources, url)
    except Exception as exc:
        logger.warning("Could not pre-load sources.yaml (%s); using defaults.", exc)

    if not listing_path:
        listing_path = (peeked_record.get("aem_listing_path") if peeked_record else None) or ""
    if not item_selector:
        item_selector = (
            (peeked_record.get("aem_item_selector") if peeked_record else None) or default_item_selector
        )

    url, slug, ticker, _record, _extra_query_params = resolve_source_identity(
        url, slug, ticker,
        default_slug=default_slug, default_ticker=default_ticker, default_url=default_url,
        listing_path_suffix=listing_path, logger=logger,
    )

    return url, slug, ticker, item_selector