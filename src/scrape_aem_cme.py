#!/usr/bin/env python3
"""
scrape_aem_cme.py

Scrape press-release listings from CME Group's investor-relations /
media-room site and merge them into primary_wire's daily
data/YYYY/YYYY-MM-DD.csv files.

  https://www.cmegroup.com/media-room/press-releases.html

This was split out of the former scrape_aem.py, which handled BNY and CME
Group in one file on the theory that "same platform (AEM) => same
scraper". That theory holds for the underlying CMS -- see the fingerprint
note at the bottom of this docstring, CME really does run on Adobe
Experience Manager -- but AEM is a page-authoring platform, not a
prepackaged press-release-listing widget: every AEM site's IR team builds
its own bespoke listing markup, pagination control, and filter UI on top
of it, and CME's turned out to share basically none of that with BNY's.
See scrape_aem_bny.py's module docstring for the BNY side.

Page structure
--------------
  1. Not an Adobe Core Components site at all under the hood -- CME's own
     "cme*"-prefixed widget classes on top of AEM. Each press-release card
     is `<li><div class="vcard column"><div class="vcard content">
     <div class="cmeBrowseAllLeft"><p class="cmeBrowseAllTitle"><a
     href="...">headline</a></p><p class="cmeBrowseAllDate">6 August,
     2026</p></div></div></div></li>`, with every card an `<li>` directly
     under `<ul id="cmeSearchFilterResults">`. ITEM_SELECTOR_CASCADE
     matches on that id directly since a bare "li" would false-positive on
     nav menus elsewhere on the page.
  2. The dateline text is day-first ("6 August, 2026", not "August 6,
     2026") -- DATE_PATTERNS in utils/scrape_utils.py has a dedicated
     pattern for this order; don't assume every AEM/press-release theme
     uses the US month-first convention BNY's does.
  3. Pagination is a jQuery "bootpag" widget (`<ul class="pagination
     bootpag">` of `<li data-lp="N"><a href="javascript:void(0);">...`),
     with the "next" control being `<li class="next">` -- no aria-label,
     no rel="next", and its accessible name ("Next \u203a") doesn't match
     a bare "next" or "\u203a", so it needs its own selector.
  4. Pages are 30 cards each (vs. BNY's 10), and there is no working
     in-page *year* filter -- but there IS a free-text date-range picker
     (`input[name=start]`/`input[name=end]` inside `#datepicker`, submitted
     via `#btnSearchFilterConfirmBottom`). See "Date-range filter" below
     for why this scraper still defaults to the unfiltered walk-and-stop
     strategy rather than that control.

Date-range filter (--use-date-filter, opt-in, best-effort)
------------------------------------------------------------
The original scrape_aem.py never drove this control -- it always walked
CME's listing from page 1 (most recent) forward, relying on the
early-stop-once-past-the-target-year logic below to bound the work. That
is correct but wasteful for anything other than the current year: the
--debug-dump-html capture this split was written from needed 11 pages
(330 cards) to reach October 2023 -- i.e. it fetched and parsed roughly
300 press releases it had no use for to find ~20 that mattered.

_try_apply_cme_date_range() below drives the picker directly (`start`/
`end` are plain `<input type="text">` fields, not a JS calendar overlay
that has to be clicked through -- filling them and clicking the "Filter
Results" submit button is enough for a typical bootstrap-datepicker-style
widget). This is NEW, unverified-against-the-live-site code: nothing in
this repo has driven this control before, and it could not be tested live
while writing it. It is therefore:
  * opt-in via --use-date-filter, not the default -- so existing automated
    runs keep the previously verified (slower but working) behavior.
  * best-effort with a clean fallback -- if the fill/submit doesn't
    visibly narrow the result set within --change-timeout, this scraper
    logs a warning and falls back to the unfiltered walk-and-stop
    strategy, exactly as if --use-date-filter had never been passed.
Before relying on this for real runs, verify it with --show-browser
--use-date-filter --year <a year with few releases> and eyeball what
actually happens.

If a future run confirms it works reliably, promoting it from opt-in to
default (and dropping the now-unnecessary full-history walk for older
years) is a reasonable follow-up -- just verify first.

Architecture
------------
Same shape as scrape_q4_ir.py: Playwright drives a real Chrome instance
(headless by default) because AEM listings render via JS, then the fully
rendered DOM is parsed with BeautifulSoup. No private/internal API is used
-- this reads exactly what a human visiting the page would see, including
clicking through "Next" pagination if present.

Fingerprint (used by src/reporting/detect_ir_platform.py's aem check)
-----------------------------------------------------------------------
This confirms CME genuinely IS built on Adobe Experience Manager (the
"aem" platform label in detect_ir_platform.py / sources_utils.PLATFORMS is
correct for this site -- what's split out here is the *scraper strategy*,
not the platform classification):
  * Page source containing an "/etc.clientlibs/" or "/content/dam/" asset
    path (AEM's client-library and DAM asset conventions), present even
    though CME's own theme doesn't use Adobe Core Components' "cmp-"
    prefixed CSS classes at all.

Date extraction
----------------
Tried in this order for each listing-page item, first match wins:
  1. A <time> element inside the item's card, preferring its `datetime`
     attribute over its display text. (CME's cards don't actually use
     <time>; this is here for forward-compatibility with a theme change.)
  2. Common "date label" CSS classes (ITEM_DATE_SELECTORS), including
     CME's own ".cmeBrowseAllDate".
  3. A bare-date text-node walk of the card, excluding the headline
     anchor's own text.
  4. A date embedded in the detail-page URL itself, e.g.
     "/2026/8/06/some-title.html" -- CME's own detail-page URLs don't
     zero-pad the month segment, so the regex below tolerates 1-2 digits.
     Used only as a last resort; CME's cards are normally dated directly
     via ".cmeBrowseAllDate" and rarely need this fallback in practice.
  5. (Opt-in via --fetch-detail-pages) fetch each still-undated item's own
     detail page in the same browser session and look there.

Usage
-----
  # Dry-run (no files written)
  python src/scrape_aem_cme.py --dry-run

  # Scrape by slug/ticker (looked up in sources.yaml) or URL directly
  python src/scrape_aem_cme.py --slug cme --dry-run
  python src/scrape_aem_cme.py --ticker CME --dry-run
  python src/scrape_aem_cme.py --url https://www.cmegroup.com/media-room/press-releases.html --dry-run

  # Restrict to a year (unfiltered walk-and-stop by default -- see
  # "Date-range filter" above for the opt-in alternative)
  python src/scrape_aem_cme.py --year 2024 --dry-run
  python src/scrape_aem_cme.py --year 2024 --use-date-filter --show-browser --dry-run

  # Fetch detail pages to resolve any dates the listing page didn't expose
  python src/scrape_aem_cme.py --fetch-detail-pages --dry-run

  # Watch the browser and save the rendered HTML for debugging selectors
  python src/scrape_aem_cme.py --show-browser --debug-dump-html /tmp/cme.html --dry-run

  # Override the item-card CSS selector if CME's markup changes
  python src/scrape_aem_cme.py --item-selector ".my-custom-card" --dry-run

  # Output as JSON
  python src/scrape_aem_cme.py --format json --output out.json --dry-run

Requires
--------
  pip install playwright beautifulsoup4 lxml
Chrome is assumed to already be installed; channel="chrome" reuses it
directly, no `playwright install` download needed.

Per README.txt's "Guidelines for automated contributions": run at most once
a day. --polite-delay paces in-page interactions (pagination clicks,
detail-page fetches) so the site isn't hammered.
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
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError:
    sys.exit("Missing dependency. Install with: pip install playwright")

from utils.scrape_utils import (
    NewsItem as _BaseNewsItem,
    add_common_args,
    dedupe_by_url,
    extract_date_from_detail_html,
    finalize_and_output,
    parse_date,
    parse_year_args,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_SLUG = "cme"
DEFAULT_TICKER = "CME"
DEFAULT_URL = "https://www.cmegroup.com/media-room/press-releases.html"

logger = logging.getLogger("scrape_aem_cme")


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

# CME's own press-release card -- see module docstring's "Page structure"
# section. Scoped to #cmeSearchFilterResults so a bare "li" doesn't
# false-positive on nav menus elsewhere on the page.
ITEM_SELECTOR_CASCADE: list[str] = [
    "#cmeSearchFilterResults > li",
]
DEFAULT_ITEM_SELECTOR = ", ".join(ITEM_SELECTOR_CASCADE)

# Date-label CSS classes tried on each matched item container before
# falling back to a bare-text-node walk.
ITEM_DATE_SELECTORS: list[str] = [
    "time",
    ".cmeBrowseAllDate",  # CME's dateline element -- see module docstring
    ".date",
    ".press-release-date",
    ".release-date",
    ".article-date",
    "[class*='date']",
]

MIN_HEADLINE_TITLE_LEN = 20  # chars; a real press-release title clears this, a nav label doesn't

# Safety ceiling for render_and_parse_year_pass()'s early-stop-once-past-
# the-target-year fallback (see its docstring) -- bounds a request for a
# year CME has no releases for at all. A real target year always stops
# well before this via the date check, regardless of the site's total
# history length.
UNFILTERED_YEAR_HUNT_SAFETY_CAP = 300


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem(_BaseNewsItem):
    """CME (AEM) press-release item.

    Inherits slug, ticker, title, url, publish_date, raw_date_text, and
    publish_date_str from scrape_utils.NewsItem. No extra fields needed.
    """


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

# /2026/8/06/some-title.html -- CME's detail-page URLs don't zero-pad the
# month segment, so \d{1,2} (not \d{2}) or this fallback would silently
# never fire for CME at all.
_URL_DATE_PATH_RE = re.compile(r"/(\d{4})/(\d{1,2})/(\d{1,2})(?:/|$)")


def date_from_url(url: str) -> Optional[date]:
    """Best-effort publish date parsed directly out of a detail-page URL.

    Only used as a last resort (see resolve_publish_date()) -- CME's cards
    are normally dated directly via ".cmeBrowseAllDate" and rarely need
    this fallback in practice.
    """
    m = _URL_DATE_PATH_RE.search(url)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def is_bare_date_text(text: str, raw_match: str) -> bool:
    """True if *text* is (almost) entirely *raw_match* and not a longer
    sentence that merely happens to contain a date somewhere in it.

    Same rule scrape_investorroom.py uses -- see its docstring for why a
    naive "first date-like substring in the card" approach is unreliable
    (headlines and summary snippets routinely mention unrelated dates).
    """
    remainder = text.replace(raw_match, "", 1)
    return remainder.strip(" \t\r\n-\u2013\u2014|\u00b7\u2022.,:") == ""


def extract_item_date(container, anchor) -> tuple[Optional[date], str]:
    """Find a publish date inside one item's card, trying (in order):

      1. A <time> element's `datetime` attribute, then its display text.
      2. Other common date-label CSS classes (ITEM_DATE_SELECTORS),
         including CME's own ".cmeBrowseAllDate".
      3. A bare-date text-node walk of the whole container, excluding any
         text that belongs to the headline anchor itself.

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

    for sel in ITEM_DATE_SELECTORS:
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
    URL, preferring the card -- a URL-embedded date isn't guaranteed to
    match the article's real publish date, so it's kept only as a
    fallback for when the card itself has no date.
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
# Browser helpers
# ---------------------------------------------------------------------------

_DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def _launch_browser(p, headless: bool, browser_channel: str, timeout_ms: int):
    """Launch a Chromium browser and return a configured Page.

    cmegroup.com runs Akamai Bot Manager in front of this page. Getting
    past it in headless mode takes more than just "launch Chromium and
    go":
      1. --disable-http2: without this, the very first navigation fails
         outright with net::ERR_HTTP2_PROTOCOL_ERROR -- Akamai's HTTP/2
         fingerprint check RSTs the stream before any HTML comes back.
      2. --disable-blink-features=AutomationControlled: removes one of the
         standard CDP-automation tells from the renderer.
      3. A real desktop UA + matching viewport/locale/timezone/headers, and
         patching navigator.webdriver via an init script: with HTTP/2 out of
         the way, an unconfigured automated browser doesn't get RST anymore,
         it just gets silently stalled (hangs past the goto timeout) --
         Akamai's JS challenge either never resolves or the response is
         withheld. Looking as close to a normal desktop Chrome session as
         possible is what gets a response back at all.
    None of this guarantees headless will get through -- if it still doesn't,
    --show-browser / --fallback-to-visible is the documented fallback (see
    scrape_and_filter()).
    """
    launch_kwargs: dict = {
        "headless": headless,
        "args": ["--disable-http2", "--disable-blink-features=AutomationControlled"],
    }
    if browser_channel:
        launch_kwargs["channel"] = browser_channel
    browser = p.chromium.launch(**launch_kwargs)
    context = browser.new_context(
        user_agent=_DESKTOP_CHROME_UA,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    context.add_init_script(_STEALTH_INIT_SCRIPT)
    page = context.new_page()
    page.set_default_timeout(timeout_ms)
    return browser, page


def _goto_with_retry(page: Page, url: str, timeout_ms: int, *, retries: int = 2) -> None:
    """page.goto() with a couple of retries on transient network-level
    failures (net::ERR_* -- connection resets, protocol errors, etc.), as
    opposed to PlaywrightTimeoutError which already gets its own handling
    elsewhere. Akamai occasionally drops the very first connection attempt
    from a fresh browser context even once HTTP/2 is disabled (see
    _launch_browser()), so a bare retry with a short pause clears most of
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


def _current_item_hrefs(page: Page, item_selector: str) -> set[str]:
    """Return the set of detail-page hrefs currently rendered, one per
    matched item-selector element."""
    return set(
        href for href in page.locator(item_selector).evaluate_all(
            """els => els.map(e => {
                const a = e.tagName === 'A' ? e : e.querySelector('a[href]');
                return a ? a.getAttribute('href') : null;
            })"""
        )
        if href
    )


def _wait_for_items(page: Page, timeout_ms: int, item_selector: str) -> None:
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


def _wait_for_list_change(
    page: Page, previous_hrefs: set[str], timeout_ms: int, item_selector: str,
    poll_interval_ms: int = 200, settle_ms: int = 400,
) -> set[str]:
    """Poll until the rendered item-href set differs from *previous_hrefs*,
    then return the new set."""
    deadline = time.monotonic() + timeout_ms / 1000
    current = previous_hrefs
    while time.monotonic() < deadline:
        current = _current_item_hrefs(page, item_selector)
        if current != previous_hrefs:
            time.sleep(settle_ms / 1000)
            return _current_item_hrefs(page, item_selector)
        time.sleep(poll_interval_ms / 1000)
    logger.warning(
        "Item list did not change within %dms after the last action. Proceeding with "
        "whatever is currently rendered (expected if this was the last page).",
        timeout_ms,
    )
    return current


def _click_pagination_next(page: Page, timeout_ms: int) -> bool:
    """Click CME's "next page" control if one is visible and not disabled.

    CME's own "bootpag" jQuery pagination plugin: `<li class="next">`
    wrapping a plain `<a href="javascript:void(0);">Next \u203a</a>` -- no
    aria-label, no rel="next", and its accessible name ("Next \u203a")
    doesn't fully match a bare "next"/"\u203a"/"\u00bb" name, so it needs its
    own selector ahead of the generic accessible-name fallbacks kept below
    in case CME's theme is ever rebuilt on top of ordinary semantic markup.
    `:not(.disabled)` matters: the same `<li class="next">` gains a
    "disabled" class once the last page is reached.

    Every match within a selector group (not just the first in DOM order)
    is checked for visibility before moving to the next group. This
    matters concretely for CME: its navbar has an unrelated, closed
    "Create an Account" dropdown item whose icon span carries
    class="icon-chevron-right", which would otherwise satisfy a naive
    "chevron-right" guess before ever reaching the real, visible pagination
    control further down the DOM (this is the exact bug the old shared
    scrape_aem.py hit once BNY's and CME's selector cascades were combined
    into one list and only `.first` was tried -- see git history/2026-08
    fix -- it doesn't recur here since this file only ever looks for CME's
    own real "next" control, but the defensive check is retained).

    Returns False if none of the above is found or clickable, which the
    caller treats as "no more pages".
    """
    selector_groups = [
        "li.next:not(.disabled) a",
        "a[rel='next']:not(.disabled)",
        "[aria-label*='next' i]:not(.disabled)",
    ]
    role_fallbacks = [
        ("link", re.compile(r"^\s*(next|›|»)\s*$", re.IGNORECASE)),
        ("button", re.compile(r"^\s*(next|›|»)\s*$", re.IGNORECASE)),
    ]

    def _click_first_visible(control) -> bool:
        for idx in range(control.count()):
            candidate = control.nth(idx)
            try:
                if not candidate.is_visible():
                    continue
                candidate.click(timeout=timeout_ms)
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    for selector in selector_groups:
        if _click_first_visible(page.locator(selector)):
            return True
    for role, name in role_fallbacks:
        if _click_first_visible(page.get_by_role(role, name=name)):
            return True
    return False


def _try_apply_cme_date_range(page: Page, year: int, timeout_ms: int) -> bool:
    """Best-effort, OPT-IN (see --use-date-filter): fill CME's date-range
    picker with Jan 1 - Dec 31 of *year* and submit it.

    CME's "Refine Your Search" panel has a `#datepicker` widget with two
    plain `<input type="text" name="start">` / `name="end">` fields (not a
    click-through calendar overlay -- see module docstring) and a
    `#btnSearchFilterConfirmBottom` submit button labeled "Filter Results".
    This fills both inputs directly and clicks that button.

    UNVERIFIED against the live site -- see the "Date-range filter"
    section of this module's docstring. Tries the two most common
    bootstrap-datepicker input formats ("mm/dd/yyyy" then "yyyy-mm-dd");
    if neither leaves a materially different result count/list after
    --change-timeout, returns False and the caller falls back to the
    unfiltered walk-and-stop strategy, same as if this had never been
    tried.

    Returns True only if the fields were filled and the confirm button
    was clicked without error -- NOT a guarantee the site actually
    narrowed the results; the caller's own _wait_for_list_change() /
    early-stop check is what actually verifies that.
    """
    start = page.locator("input[name='start']")
    end = page.locator("input[name='end']")
    confirm = page.locator("#btnSearchFilterConfirmBottom")
    if start.count() == 0 or end.count() == 0 or confirm.count() == 0:
        logger.debug("Date-range picker controls not found on the page.")
        return False

    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            start.first.fill(date(year, 1, 1).strftime(fmt), timeout=timeout_ms)
            end.first.fill(date(year, 12, 31).strftime(fmt), timeout=timeout_ms)
            confirm.first.click(timeout=timeout_ms)
            logger.info(
                "Submitted CME date-range filter for %d using %r-style dates -- "
                "UNVERIFIED against the live site, see module docstring; the caller "
                "will fall back automatically if this didn't actually narrow the results.",
                year, fmt,
            )
            return True
        except PlaywrightTimeoutError:
            logger.debug("Date-range fill/submit with format %r failed; trying the next format.", fmt)
            continue
    return False


# ---------------------------------------------------------------------------
# Listing-page rendering
# ---------------------------------------------------------------------------

def _dump_path_for_page(debug_dump_html: Optional[Path], page_num: int) -> Optional[Path]:
    """Return the path to save page *page_num*'s HTML to, or None if
    --debug-dump-html wasn't passed. Page 1 keeps the exact path given;
    later pages get a "_page{N}" suffix inserted before the extension."""
    if debug_dump_html is None:
        return None
    if page_num == 1:
        return debug_dump_html
    return debug_dump_html.with_name(f"{debug_dump_html.stem}_page{page_num}{debug_dump_html.suffix}")


def render_and_parse_year_pass(
    url: str,
    slug: str,
    ticker: str,
    year: Optional[int],
    headless: bool,
    browser_channel: str,
    timeout_ms: int,
    change_timeout_ms: int,
    polite_delay: float,
    max_load_more: int,
    debug_dump_html: Optional[Path],
    item_selector: str = DEFAULT_ITEM_SELECTOR,
    use_date_filter: bool = False,
) -> list[NewsItem]:
    """Drive Chrome through one listing pass and return every item found
    across every page it can paginate through.

    This parses and collects items after *each* page loads rather than
    doing one page.content() at the very end, same as BNY -- CME's
    bootpag pagination also replaces the rendered card list on every
    click rather than appending to it (a page only ever has 30
    `#cmeSearchFilterResults > li` cards in the DOM at once).

    year=None does one unfiltered pass over whatever the site shows by
    default (most recent releases across all years, mixed) -- used when
    no --year/--start-year/--end-year/--since/--until was given at all.

    Early-stop-once-past-the-target-year (the default strategy for a
    specific year)
    -----------------------------------------------------------------
    CME has no verified working year filter (see
    _try_apply_cme_date_range() for the opt-in, unverified date-range
    attempt). By default, this scraper instead paginates the unfiltered,
    newest-first listing and stops as soon as an entire page's items are
    all older than Jan 1 of *year* -- there's nothing older left to find.
    This is both a correctness fix (a low --max-load-more no longer risks
    silently missing a year that's simply further back than the default
    page count reaches) and a politeness one (stops as soon as the answer
    is known). It is NOT cheap for an old year: reaching October 2023 from
    today's listing took 11 pages / ~330 cards in testing. See
    --use-date-filter for an (unverified) way to avoid that.
    UNFILTERED_YEAR_HUNT_SAFETY_CAP is the hard ceiling for this case.
    """
    with sync_playwright() as p:
        browser, page = _launch_browser(p, headless, browser_channel, timeout_ms)

        logger.info("Loading %s ...", url)
        _goto_with_retry(page, url, timeout_ms)
        _wait_for_items(page, timeout_ms, item_selector)

        date_filter_applied = False
        if year is not None and use_date_filter:
            before = _current_item_hrefs(page, item_selector)
            if _try_apply_cme_date_range(page, year, timeout_ms):
                time.sleep(polite_delay)
                after = _wait_for_list_change(page, before, change_timeout_ms, item_selector)
                # Treat "the list actually changed" as confirmation the
                # filter took effect -- an unchanged list after submitting
                # means the site ignored/ate the input, so fall back rather
                # than trust an unfiltered page 1 as if it were filtered.
                date_filter_applied = after != before
                if date_filter_applied:
                    logger.info("CME date-range filter for %d appears to have applied.", year)
                else:
                    logger.warning(
                        "Submitted the CME date-range filter for %d, but the result list "
                        "didn't change -- falling back to the unfiltered walk-and-stop "
                        "strategy. (--use-date-filter is unverified against the live "
                        "site; this is the expected safe failure mode if it doesn't work.)",
                        year,
                    )

        effective_max_load_more = max_load_more
        if year is not None and not date_filter_applied:
            effective_max_load_more = max(max_load_more, UNFILTERED_YEAR_HUNT_SAFETY_CAP)

        all_items: list[NewsItem] = []
        seen_urls: set[str] = set()
        page_num = 1

        while True:
            html = page.content()
            dump_path = _dump_path_for_page(debug_dump_html, page_num)
            if dump_path:
                dump_path.parent.mkdir(parents=True, exist_ok=True)
                dump_path.write_text(html, encoding="utf-8")
                logger.info("Saved rendered HTML for page %d to %s", page_num, dump_path)

            page_items = parse_listing_page(html, base_url=url, slug=slug, ticker=ticker, item_selector=item_selector)
            new_items = [i for i in page_items if i.url.rstrip("/") not in seen_urls]
            for i in new_items:
                seen_urls.add(i.url.rstrip("/"))
            all_items.extend(new_items)
            logger.debug(
                "Page %d: %d item(s) parsed, %d new (%d total so far)",
                page_num, len(page_items), len(new_items), len(all_items),
            )

            if year is not None and not date_filter_applied and page_items:
                dated = [i.publish_date for i in page_items if i.publish_date is not None]
                if dated and max(dated) < date(year, 1, 1):
                    logger.info(
                        "Page %d is entirely older than %d -- stopping this unfiltered "
                        "pass early (every %d item on this listing has now been seen).",
                        page_num, year, year,
                    )
                    break
                if date_filter_applied and dated and min(dated) > date(year, 12, 31):
                    # Filtered pass sanity check: if the *filter* claims to
                    # be scoped to `year` but we're still seeing dates past
                    # it, something didn't apply the way we assumed --
                    # don't stop early on a possibly-wrong premise.
                    pass

            if page_num >= effective_max_load_more:
                logger.warning(
                    "Hit --max-load-more (%d) while paginating; there may be more, older "
                    "press releases this run didn't reach. Re-run with a higher "
                    "--max-load-more if so.", effective_max_load_more,
                )
                break

            before = _current_item_hrefs(page, item_selector)
            clicked = _click_pagination_next(page, timeout_ms)
            if not clicked:
                break
            time.sleep(polite_delay)
            after = _wait_for_list_change(page, before, change_timeout_ms, item_selector)
            if after == before:
                break
            page_num += 1

        browser.close()
        return all_items


# ---------------------------------------------------------------------------
# Listing-page parsing
# ---------------------------------------------------------------------------

def _first_link_in(container) -> Optional[object]:
    """Return the container itself if it IS an <a>, else its first
    descendant <a href>, else None."""
    if getattr(container, "name", None) == "a" and container.get("href"):
        return container
    return container.find("a", href=True)


def _extract_title(container, anchor) -> str:
    """Return the display title for one item's card.

    Prefers the anchor's own text; falls back to a heading element inside
    the container."""
    title = anchor.get_text(separator=" ", strip=True)
    if title:
        return title
    heading = container.find(["h1", "h2", "h3", "h4"])
    if heading is not None:
        return heading.get_text(separator=" ", strip=True)
    return ""


def parse_listing_page(
    html: str, base_url: str, slug: str, ticker: str, item_selector: str = DEFAULT_ITEM_SELECTOR,
) -> list[NewsItem]:
    """Parse one fully-rendered listing page into NewsItems.

    CME's cards are always matched directly by item_selector
    ("#cmeSearchFilterResults > li") -- unlike BNY, there's no same-host
    heuristic fallback here, since CME's markup doesn't have BNY's history
    of bespoke-widget drift and a same-host scan of a page this large
    (heavy nav, footer, and related-links chrome) would be considerably
    more error-prone. If item_selector ever matches nothing, that's
    treated as a hard signal CME's markup has changed and needs
    --item-selector (or a code update), not silently papered over.
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    containers = soup.select(item_selector)
    if containers:
        logger.debug("item_selector matched %d container(s).", len(containers))
    else:
        logger.warning(
            "item_selector (%r) matched nothing -- CME's markup may have changed. Pass "
            "--item-selector, or --debug-dump-html to inspect the rendered page.",
            item_selector,
        )

    for container in containers:
        anchor = _first_link_in(container)
        if anchor is None:
            continue

        href = anchor["href"].strip()
        full_url = urljoin(base_url, href)
        norm_url = full_url.rstrip("/")
        if norm_url in seen_urls:
            continue

        title = _extract_title(container, anchor)
        if not title:
            logger.debug("Skipping item with no title text: %s", full_url)
            continue

        card_date, card_raw_text = extract_item_date(container, anchor)
        url_date = date_from_url(href)
        publish_date, raw_date_text = resolve_publish_date(card_date, card_raw_text, url_date, full_url)

        seen_urls.add(norm_url)
        items.append(NewsItem(
            slug=slug, ticker=ticker, title=title, url=full_url,
            publish_date=publish_date, raw_date_text=raw_date_text,
        ))

    if not items:
        logger.warning(
            "No press-release items found at all. Re-run with --show-browser "
            "--debug-dump-html /tmp/out.html and inspect the saved file."
        )
    else:
        missing = sum(1 for i in items if i.publish_date is None)
        if missing:
            logger.warning(
                "%d of %d item(s) had no parseable date. Pass --fetch-detail-pages to "
                "resolve them from each item's own detail page.",
                missing, len(items),
            )

    return items


# ---------------------------------------------------------------------------
# Detail-page date fallback
# ---------------------------------------------------------------------------

def fetch_missing_dates(
    items: list[NewsItem], headless: bool, browser_channel: str, timeout_ms: int, polite_delay: float,
) -> None:
    """Fill in publish_date for items the listing-page parse left undated,
    by visiting each one's own detail page in the same browser session.
    Modifies *items* in place.
    """
    undated = [item for item in items if item.publish_date is None]
    if not undated:
        return

    logger.info("Fetching detail pages for %d undated item(s) to resolve publish dates ...", len(undated))

    with sync_playwright() as p:
        browser, page = _launch_browser(p, headless, browser_channel, timeout_ms)

        for i, item in enumerate(undated):
            if i > 0:
                time.sleep(polite_delay)
            logger.info("  [%d/%d] %s", i + 1, len(undated), item.url)
            try:
                _goto_with_retry(page, item.url, timeout_ms)
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
    listing_path: Optional[str] = None, item_selector: Optional[str] = None,
) -> tuple[str, str, str, str]:
    """Resolve (url, slug, ticker, item_selector) from CLI args and
    sources.yaml.

    listing_path precedence (highest wins):
      1. --listing-path on the CLI
      2. an "aem_listing_path" field on the matched sources.yaml record
      3. "" (use the resolved URL as-is -- CME's sources.yaml news_url is
         already the complete listing URL)

    item_selector precedence (highest wins):
      1. --item-selector on the CLI
      2. an "aem_item_selector" field on the matched sources.yaml record
      3. DEFAULT_ITEM_SELECTOR

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
            (peeked_record.get("aem_item_selector") if peeked_record else None) or DEFAULT_ITEM_SELECTOR
        )

    url, slug, ticker, _record, _extra_query_params = resolve_source_identity(
        url, slug, ticker,
        default_slug=DEFAULT_SLUG, default_ticker=DEFAULT_TICKER, default_url=DEFAULT_URL,
        listing_path_suffix=listing_path, logger=logger,
    )

    return url, slug, ticker, item_selector


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    add_common_args(parser)

    source = parser.add_argument_group("source")
    source.add_argument(
        "--listing-path", default=None, metavar="PATH",
        help=(
            "Path appended to a slug/ticker-derived scrape URL for the press-release "
            "listing, if that URL isn't already the full listing page (default: none). "
            "Overrides an 'aem_listing_path' sources.yaml field for this run."
        ),
    )
    source.add_argument(
        "--item-selector", default=None, metavar="CSS_SELECTOR",
        help=(
            "CSS selector for one press-release card container (default: CME's "
            "'#cmeSearchFilterResults > li' -- see ITEM_SELECTOR_CASCADE in this file's "
            "source). Overrides an 'aem_item_selector' sources.yaml field for this run. "
            "Use --debug-dump-html to inspect the rendered page if the default doesn't match."
        ),
    )
    source.add_argument(
        "--use-date-filter", action="store_true", default=False,
        help=(
            "For --year/--start-year/--end-year runs, try CME's in-page date-range "
            "picker instead of paginating the entire unfiltered listing. OPT-IN and "
            "UNVERIFIED against the live site -- see the 'Date-range filter' section of "
            "this module's docstring. Falls back automatically to the unfiltered "
            "walk-and-stop strategy if the filter doesn't visibly narrow the results."
        ),
    )

    out = parser.add_argument_group("output")
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
            "If the headless run finds zero items, or times out just navigating to the "
            "listing page (both are typical bot-mitigation symptoms), automatically retry "
            "with a visible browser window. Has no effect when --show-browser is already "
            "set. Not suitable for headless CI environments."
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
        help="Timeout in ms to wait for the item list to change after a pagination/filter "
             "click (default: 8000)",
    )
    browser.add_argument(
        "--polite-delay", type=float, default=2.0,
        help="Seconds to pause after each in-page interaction and between detail-page "
             "fetches (default: 2.0)",
    )
    browser.add_argument(
        "--max-load-more", type=int, default=20,
        help="Safety cap on pagination clicks per run (default: 20)",
    )
    browser.add_argument(
        "--debug-dump-html", type=Path,
        help="Save the final rendered listing-page HTML to this path for inspection",
    )

    fallback = parser.add_argument_group("date fallback")
    fallback.add_argument(
        "--fetch-detail-pages", action="store_true",
        help="For any item whose date was not found on the listing page, fetch its "
             "individual press-release page (via the same browser) to extract the date.",
    )

    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="-v for INFO, -vv for DEBUG (default: WARNING)",
    )

    return parser


def scrape(
    url: str, slug: str, ticker: str, years: Optional[Iterable[int]], args: argparse.Namespace, item_selector: str,
) -> list[NewsItem]:
    """Scrape one listing, doing one render_and_parse_year_pass() per
    requested year, or a single unfiltered pass over the default view
    when no --year/--start-year/--end-year/--since/--until was given at
    all.
    """
    years_to_visit: list[Optional[int]] = sorted(years) if years else [None]

    all_items: list[NewsItem] = []
    for i, year in enumerate(years_to_visit):
        if i > 0:
            time.sleep(args.polite_delay)
        dump_path = args.debug_dump_html
        if dump_path and len(years_to_visit) > 1 and year is not None:
            dump_path = dump_path.with_name(f"{dump_path.stem}_{year}{dump_path.suffix}")

        items = render_and_parse_year_pass(
            url=url,
            slug=slug,
            ticker=ticker,
            year=year,
            headless=args.headless,
            browser_channel=args.browser_channel,
            timeout_ms=args.timeout,
            change_timeout_ms=args.change_timeout,
            polite_delay=args.polite_delay,
            max_load_more=args.max_load_more,
            debug_dump_html=dump_path,
            item_selector=item_selector,
            use_date_filter=args.use_date_filter,
        )
        all_items.extend(items)

    return dedupe_by_url(all_items)


def scrape_and_filter(
    argv: Optional[list[str]] = None, *, write: bool = True
) -> tuple[int, list[NewsItem]]:
    """Parse args, scrape, filter/preview, and (by default) write out results.

    Split out from main() so a caller other than the command line --
    scrape_all.py -- can invoke it directly and get the scraped items back
    as a normal return value, matching every other scraper in this repo.
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    level = {0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    url, slug, ticker, item_selector = resolve_source(
        args.url, args.slug, args.ticker, args.listing_path, args.item_selector,
    )
    if not url:
        logger.error("Could not determine a listing URL. Pass --url, --slug, or --ticker.")
        return 1, []
    logger.info("slug=%s  ticker=%s  url=%s  item_selector=%r", slug, ticker, url, item_selector)
    print(f"Scraping: {url}")

    years = parse_year_args(args)
    try:
        all_items = scrape(url, slug, ticker, years, args, item_selector)
    except PlaywrightTimeoutError:
        if not (args.fallback_to_visible and args.headless):
            raise
        logger.warning("Headless run timed out navigating -- likely blocked by bot mitigation.")
        all_items = None

    if args.fallback_to_visible and args.headless and not all_items:
        logger.warning(
            "Headless run returned zero items -- likely blocked by bot mitigation. "
            "Retrying with a visible browser window (--fallback-to-visible)."
        )
        args.headless = False
        all_items = scrape(url, slug, ticker, years, args, item_selector)

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
                "%d item(s) have no date. Pass --fetch-detail-pages to resolve them from "
                "individual press-release pages.", undated_count,
            )

    filtered = finalize_and_output(
        all_items,
        years=years, since=args.since, until=args.until, limit=None,
        format=args.format, output=args.output, dry_run=args.dry_run,
        data_dir=args.data_dir,
        default_json_path=REPO_ROOT / "cme_news.json",
        write=write,
    )
    if not filtered:
        logger.warning("No items matched the requested filters.")

    return 0, filtered


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for standalone invocation (``python src/scrape_aem_cme.py ...``)."""
    return_code, _items = scrape_and_filter(argv)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())