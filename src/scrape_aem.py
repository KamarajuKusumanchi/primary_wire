#!/usr/bin/env python3
"""
scrape_aem.py

Scrape press-release listings from investor relations sites built on Adobe
Experience Manager (AEM) and merge them into primary_wire's daily
data/YYYY/YYYY-MM-DD.csv files.

Default source: BNY (The Bank of New York Mellon Corporation)
  https://www.bny.com/corporate/global/en/investor-relations/press-releases.html

Page structure (BNY)
--------------------
  1. Each press-release card is a bespoke widget, not Adobe Core
     Components' documented List/Teaser markup: a
     `<div class="list-item-tile">` containing a `<time class="list-item-
     header__date">` and a `<div class="title"><a href="...">headline</a>
     </div>`. This is ITEM_SELECTOR_CASCADE's first entry; the Core
     Component guesses and a same-host heuristic scan further down the
     cascade are fallbacks for *other* AEM sites that may use different
     markup.

  2. Pagination is a bespoke numbered widget (`<ul class="pagination">` of
     `<label onclick="showDataOnPagination(N,this)">`), not a real link or
     button, and each click *replaces* the visible card list rather than
     appending to it -- a page only ever holds 10 `.list-item-tile` cards
     in the DOM at once, however many total releases exist. So this
     scraper parses and collects items after *each* page load, not once at
     the very end -- see render_and_parse_year_pass()'s docstring.

  3. The "Filter by" year control is a real, working in-page year filter:
     a `.list-filter-dropdown` that reveals `<li class="option">` entries
     (one per year, plain "2018".."2026" text) on click. Selecting a year
     narrows the paginated result set server-side before any of this
     scraper's own click-through pagination happens, which is both more
     reliable and cheaper than paginating through the entire unfiltered
     history and filtering client-side -- see _try_select_year().

Page structure (CME Group)
---------------------------
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
     no rel="next", and its accessible name ("Next \u203a") doesn't match a
     bare "next" or "\u203a", so it needed its own entry in
     _click_pagination_next()'s selector cascade.
  4. No working in-page year filter was found -- there's a free-text
     date-range picker (`input[name=start]`/`input[name=end]`), but it's a
     JS-driven calendar widget, not a plain text field, and automating it
     reliably without being able to test live against the site wasn't
     attempted. So CME always falls back to the unfiltered paginate-and-
     filter-client-side path -- see render_and_parse_year_pass()'s early-
     stop-once-past-the-target-year logic, which makes that fallback both
     correct (it won't stop before reaching the requested year) and cheap
     (it won't keep paginating long after it has), independent of
     --max-load-more.

If BNY's markup changes, or another AEM site's differs, --item-selector
overrides the selector cascade without touching code. Re-run with
--show-browser --debug-dump-html to inspect the current rendered page
before assuming the selectors above still apply.

Architecture
------------
Same shape as scrape_q4_ir.py: Playwright drives a real Chrome instance
(headless by default) because AEM listings render via JS, then the fully
rendered DOM is parsed with BeautifulSoup. No private/internal API is used
-- this reads exactly what a human visiting the page would see, including
clicking through "Load More" / paginated "Next" controls if present.

Fingerprint (used by src/reporting/detect_ir_platform.py's aem check)
-----------------------------------------------------------------------
AEM sites are identifiable by:
  * Page source containing "/etc.clientlibs/" or "/content/dam/" asset paths
    (AEM's client-library and DAM asset conventions)
  * A `<meta name="cq:..."` or `<meta name="template"` tag referencing AEM's
    Content and Sightly (HTL) rendering pipeline
  * "cmp-" prefixed CSS classes (Adobe Core Components' BEM naming: cmp-list,
    cmp-teaser, cmp-search, ...)
  * Asset/content paths under /content/<site>/... or /content/dam/...

Date extraction
----------------
Tried in this order for each listing-page item, first match wins:
  1. A <time> element inside the item's card, preferring its `datetime`
     attribute (machine-readable, e.g. "2026-01-15") over its display text.
  2. Common "date label" CSS classes seen across AEM/press-release themes
     (see ITEM_DATE_SELECTORS).
  3. A bare-date text-node walk of the card, excluding the headline anchor's
     own text -- the same "must be the ENTIRE text of its own node" rule
     scrape_investorroom.py uses (see is_bare_date_text()), so a headline
     that happens to mention an unrelated date isn't mistaken for the
     card's real dateline.
  4. A date embedded in the detail-page URL itself, e.g. "/2026/01/15/..."
     or "/2026-01-15-...". Used only as a last resort (see
     resolve_publish_date()) since a URL slug's date is not guaranteed to
     match the actual publish date on every AEM theme.
  5. (Opt-in via --fetch-detail-pages) fetch each still-undated item's own
     detail page in the same browser session and look there, the same
     fallback scrape_q4_ir.py uses.

Usage
-----
  # Default: scrape BNY, dry-run (no files written)
  python src/scrape_aem.py --dry-run

  # Any AEM IR site by slug/ticker (looked up in sources.yaml)
  python src/scrape_aem.py --slug bny --dry-run
  python src/scrape_aem.py --ticker BNY --dry-run

  # Scrape by URL directly
  python src/scrape_aem.py --url https://www.bny.com/corporate/global/en/investor-relations/press-releases.html --dry-run

  # Restrict to a year or range (client-side filter; see module docstring
  # on why this scraper does one full-history pass rather than an in-page
  # year filter)
  python src/scrape_aem.py --year 2025 --dry-run
  python src/scrape_aem.py --start-year 2023 --end-year 2025 --dry-run

  # Fetch detail pages to resolve any dates the listing page didn't expose
  python src/scrape_aem.py --fetch-detail-pages --dry-run

  # Watch the browser and save the rendered HTML for debugging selectors
  python src/scrape_aem.py --show-browser --debug-dump-html /tmp/bny.html --dry-run

  # Override the item-card CSS selector for a site whose markup doesn't
  # match any of the built-in guesses (see ITEM_SELECTOR_CASCADE)
  python src/scrape_aem.py --item-selector ".my-custom-card" --dry-run

  # Output as JSON
  python src/scrape_aem.py --format json --output out.json --dry-run

Requires
--------
  pip install playwright beautifulsoup4 lxml
Chrome is assumed to already be installed; channel="chrome" reuses it
directly, no `playwright install` download needed.

Per README.txt's "Guidelines for automated contributions": run at most once
a day. --polite-delay paces in-page interactions (load-more clicks,
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

DEFAULT_SLUG = "bny"
DEFAULT_TICKER = "BNY"
DEFAULT_URL = "https://www.bny.com/corporate/global/en/investor-relations/press-releases.html"

logger = logging.getLogger("scrape_aem")


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

# Tried as one combined CSS selector (BeautifulSoup .select() / Playwright
# locator both accept a comma-separated list and match any of them). Ordered
# most-specific-and-most-likely-correct first:
#
#   1. BNY's own bespoke card widget (see module docstring's "Page structure
#      (BNY)" section).
#   2. CME's own bespoke card widget (see module docstring's "Page structure
#      (CME Group)" section).
#   3. Adobe Core Components' documented "List" component BEM classes.
#   4. Adobe Core Components' documented "Teaser" component BEM classes.
#   5. Generic "search result" class names used by AEM search/faceted-search
#      widgets.
#   6. Bare <article> -- a common semantic wrapper for a single result card
#      regardless of the specific component library on top of it.
#
# Each entry here is a *container* selector (the card), not the link itself
# -- parse_listing_page() finds the actual <a href> inside each matched
# container. This is deliberately more tolerant than matching the anchor
# directly: it survives a theme wrapping the headline in a nested <span>
# or <h3>, as several IR-platform themes elsewhere in this repo do.
ITEM_SELECTOR_CASCADE: list[str] = [
    ".list-item-tile",  # BNY's press-release card -- see module docstring
    "#cmeSearchFilterResults > li",  # CME's press-release card -- see module docstring
    ".cmp-list__item",
    ".cmp-teaser",
    "article.cmp-teaser",
    ".cmp-search__item",
    ".cmp-search__result",
    ".cmp-searchresults__item",
    ".search-result",
    ".press-release-item",
    ".press-release-card",
    "li.cmp-list__item",
    "article",
]
DEFAULT_ITEM_SELECTOR = ", ".join(ITEM_SELECTOR_CASCADE)

# Date-label CSS classes seen across AEM/press-release themes, tried on each
# matched item container before falling back to a bare-text-node walk.
ITEM_DATE_SELECTORS: list[str] = [
    "time",
    ".list-item-header__date",  # BNY's dateline element -- see module docstring
    ".cmeBrowseAllDate",  # CME's dateline element -- see module docstring
    ".cmp-list__item-date",
    ".cmp-teaser__date",
    ".cmp-search__item-date",
    ".date",
    ".press-release-date",
    ".release-date",
    ".article-date",
    "[class*='date']",
]

# Known non-article paths on BNY's own investor-relations subnav, used to
# keep the same-host heuristic fallback (see is_probable_press_release_link())
# from mistaking a nav link for a press release. Harmless to leave in place
# for other AEM sites; extend as needed for a new source's own known
# non-article paths.
NAV_EXCLUDE_PATHS = frozenset({
    "corporate/global/en/investor-relations/overview.html",
    "corporate/global/en/investor-relations/press-releases.html",
    "corporate/global/en/investor-relations/quarterly-earnings.html",
    "corporate/global/en/investor-relations/events-and-presentations.html",
    "corporate/global/en/investor-relations/annual-reports-and-proxy.html",
    "corporate/global/en/investor-relations/regulatory-filings.html",
    "corporate/global/en/investor-relations/shareholder-services.html",
    "corporate/global/en/investor-relations/fixed-income.html",
    "corporate/global/en/investor-relations/corporate-governance.html",
    "corporate/global/en/investor-relations/investor-contacts.html",
    "corporate/global/en/about-us/newsroom.html",
    "corporate/global/en/about-us/about-bny.html",
    "corporate/global/en/about-us/leadership.html",
    "corporate/global/en/about-us/locations.html",
    "corporate/global/en/about-us/careers/work-with-us.html",
    "corporate/global/en/contact-us.html",
    "corporate/global/en/insights.html",
})
MIN_HEADLINE_TITLE_LEN = 20  # chars; a real press-release title clears this, a nav label doesn't

# Safety ceiling for render_and_parse_year_pass()'s early-stop-once-past-
# the-target-year fallback (see its docstring) -- only ever raises the
# effective page-count cap above whatever --max-load-more already says, and
# only for the specific case of a year filter that couldn't be applied
# in-page. Exists purely to bound a request for a year the site has no
# releases for at all; a real target year always stops well before this via
# the date check, regardless of the site's total history length.
_UNFILTERED_YEAR_HUNT_SAFETY_CAP = 300


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem(_BaseNewsItem):
    """AEM press-release item.

    Inherits slug, ticker, title, url, publish_date, raw_date_text, and
    publish_date_str from scrape_utils.NewsItem. No extra fields needed for
    this platform.
    """


# ---------------------------------------------------------------------------
# Date helpers (platform-specific)
# ---------------------------------------------------------------------------

# Two URL date-slug conventions seen across AEM sites:
#   /2026/01/15/some-title.html   (path-segment date, common for AEM blogs/news)
#   /2026-01-15-some-title.html   (hyphenated date prefix, InvestorRoom-style)
# Two URL date-slug conventions seen across AEM sites:
#   /2026/01/15/some-title.html   (path-segment date, common for AEM blogs/news)
#   /2026/8/06/some-title.html    (same, but CME's own detail-page URLs don't
#                                   zero-pad the month segment -- \d{1,2}
#                                   rather than \d{2} below, or this
#                                   date-from-URL fallback would silently
#                                   never fire for CME at all)
#   /2026-01-15-some-title.html   (hyphenated date prefix, InvestorRoom-style)
_URL_DATE_PATH_RE = re.compile(r"/(\d{4})/(\d{1,2})/(\d{1,2})(?:/|$)")
_URL_DATE_SLUG_RE = re.compile(r"/(\d{4}-\d{2}-\d{2})-")


def date_from_url(url: str) -> Optional[date]:
    """Best-effort publish date parsed directly out of a detail-page URL.

    Only used as a last resort (see resolve_publish_date()) -- not every AEM
    theme's URL date actually matches the article's real publish date.
    """
    m = _URL_DATE_PATH_RE.search(url)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _URL_DATE_SLUG_RE.search(url)
    if m:
        try:
            return date.fromisoformat(m.group(1))
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
      2. Other common date-label CSS classes (ITEM_DATE_SELECTORS).
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
    URL, preferring the card -- mirroring scrape_investorroom.py's
    resolve_publish_date(), for the same reason: a URL-embedded date isn't
    guaranteed to match the article's real publish date on every theme, so
    it's kept only as a fallback for when the card itself has no date.
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
# Same-host heuristic fallback (used only when no item-selector match fires)
# ---------------------------------------------------------------------------

def is_probable_press_release_link(href: str, base_url: str) -> bool:
    """Necessary-but-not-sufficient check for "might be a press-release
    detail link", used only as a last-resort fallback when none of
    ITEM_SELECTOR_CASCADE matches anything on the page (i.e. the site's
    markup doesn't match any of the guessed AEM component conventions).

    Same-host, no query string/fragment, and not one of the known nav paths
    in NAV_EXCLUDE_PATHS. The caller (parse_listing_page()) still requires a
    headline-length title AND a real nearby date before accepting a link
    found this way -- exactly the two-stage "bare-slug" confirmation
    scrape_investorroom.py uses for the same reason (a same-host link alone
    can't be told apart from an ordinary nav/footer link by shape).
    """
    full_url = urljoin(base_url, href)
    parsed = urlsplit(full_url)
    if parsed.netloc != urlsplit(base_url).netloc:
        return False
    if parsed.query or parsed.fragment:
        return False
    path = parsed.path.strip("/").lower()
    if not path or path in NAV_EXCLUDE_PATHS:
        return False
    return True


def is_confirmed_heuristic_item(title: str, card_date: Optional[date]) -> bool:
    """Second-stage check for the same-host heuristic fallback -- a
    same-host link is only accepted as a press release once it also has a
    headline-length title and a real date sitting somewhere in its card."""
    return len(title) >= MIN_HEADLINE_TITLE_LEN and card_date is not None


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

# A plain, current desktop Chrome UA string. Playwright's own default UA in
# headless mode is normally fine (modern "headless=new" Chromium no longer
# advertises "HeadlessChrome/"), but we pin an explicit one anyway so the
# browser's UA, its Client-Hints headers, and its actual rendering behavior
# all agree with each other -- bot-mitigation CDNs like Akamai cross-check
# these against one another, and any mismatch is itself a signal.
_DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Patched into every page before any site JS runs. navigator.webdriver=true
# is the single most common automation tell that bot-mitigation scripts
# check for -- Playwright (like Selenium/Puppeteer) sets it because it drives
# the browser over CDP. Clearing it doesn't make the browser un-automated,
# but it removes the cheapest, most widely-used detection signal.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def _launch_browser(p, headless: bool, browser_channel: str, timeout_ms: int):
    """Launch a Chromium browser and return a configured Page.

    Same pattern as scrape_q4_ir.py's _launch_browser(): extracted to avoid
    duplicating the launch/configure block across render_listing_page() and
    fetch_missing_dates().

    cmegroup.com (and presumably other AEM sites behind the same class of
    CDN) runs Akamai Bot Manager in front of this page. Getting past it in
    headless mode takes more than just "launch Chromium and go":
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
    elsewhere. Bot-mitigation CDNs occasionally drop the very first
    connection attempt from a fresh browser context even once HTTP/2 is
    disabled (see _launch_browser()), so a bare retry with a short pause
    clears most of these without needing a whole new browser instance.
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


def _wait_for_items(page: Page, timeout_ms: int, item_selector: str) -> None:
    """Wait for at least one item-selector match to appear in the DOM.

    Uses direct selector polling rather than networkidle, which is
    unreliable on JS-rendered content (analytics/consent scripts keep the
    network busy well after the actual result cards have finished
    rendering) -- same rationale as scrape_q4_ir.py's _wait_for_news_links().
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
    then return the new set. Same "has the list changed" signal
    scrape_q4_ir.py uses in place of networkidle for in-page actions."""
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


def _click_load_more(page: Page, timeout_ms: int) -> bool:
    """Click a "load more" / "show more" / "view more" control if visible.
    Returns True if clicked, False if no such control exists."""
    button = page.get_by_role(
        "button", name=re.compile(r"load more|show more|view more|load additional|see more", re.IGNORECASE)
    )
    if button.count() == 0:
        button = page.get_by_text(
            re.compile(r"load more|show more|view more|load additional|see more", re.IGNORECASE)
        )
    if button.count() == 0:
        return False
    try:
        if not button.first.is_visible():
            return False
        button.first.click(timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def _click_pagination_next(page: Page, timeout_ms: int) -> bool:
    """Click a "next page" control if one is visible and not disabled.

    Layered selector cascade, most site-specific first:

      1. BNY's own bespoke numbered-pagination widget: the right arrow is a
         `<label>` (not an `<a>`/`<button>`, and with no accessible name --
         it's an inline SVG icon) whose class contains
         "keyboard_arrow_right", alongside a handful of equally-plausible
         generic class-name guesses ("chevron-right", "arrow-right",
         "pagination-next") for other AEM sites that might use a similarly
         non-semantic control. `:not(.disabled)` skips it once BNY's own
         widget marks it spent (the left arrow starts with a literal
         "disabled" class on page 1).
      2. CME's own "bootpag" jQuery pagination plugin: `<li class="next">`
         wrapping a plain `<a href="javascript:void(0);">Next ›</a>` -- no
         aria-label, no rel="next", and its accessible name ("Next ›")
         doesn't fully match the bare "next"/"›"/"»" names case 4 below
         looks for, so it needs its own entry. `:not(.disabled)` matters
         here too: the same `<li class="next">` gains a "disabled" class
         once the last page is reached.
      3. rel="next" or an aria-label containing "next" (the standard,
         accessible convention some AEM sites do implement properly).
      4. A link/button whose accessible name is literally "Next", "›", or
         "»" (kept from the original design, for sites that use ordinary
         semantic markup).

    Returns False if none of the above is found or clickable, which the
    caller treats as "no more pages".

    Bug this replaced (2026-08): the cascade used to be joined into a
    *single* comma-separated selector and only ``.first`` (i.e. first in
    DOM order) was tried. On CME that silently broke pagination after page
    1: CME's navbar has an unrelated, closed "Create an Account" dropdown
    item earlier in the DOM whose icon span carries a
    ``class="icon-chevron-right"`` -- matching the generic
    ``[class*='chevron-right']`` guess meant for *other* sites' arrow
    icons. Because that span sits before the real `<li class="next">`
    pagination control in the DOM, ``.first`` resolved to it every time,
    found it invisible (the dropdown is closed), and returned False --
    without ever looking at the real, visible "Next" control matched
    later by the very same combined selector. So each selector group is
    now tried in priority order, and *within* a group every match (not
    just the first) is checked for visibility before giving up on that
    group and moving to the next -- both restoring the documented
    site-specific priority and skipping incidental false-positive
    matches like CME's navbar icon."""
    selector_groups = [
        "[class*='keyboard_arrow_right']:not(.disabled)",
        "[class*='chevron-right']:not(.disabled)",
        "[class*='arrow-right']:not(.disabled)",
        "[class*='pagination-next']:not(.disabled)",
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


def _try_select_year(page: Page, year: int, timeout_ms: int) -> bool:
    """Best-effort: apply an in-page year filter, if this site exposes one.

    BNY's "Filter by" control is a bespoke listbox widget, not a native
    <select> -- a clickable `.list-filter-dropdown` container that reveals
    a `<ul class="select_ul">` of `<li class="option">` entries on click,
    each labeled with a plain 4-digit year (also carrying a `data-attr-val`
    ending in "/<year>" as a second way to match, in case the visible label
    text ever isn't a bare year on some other AEM site reusing this same
    widget).

    Tries a real <select> first regardless, since a different AEM site
    might implement the same "filter by year" idea with ordinary markup.

    Returns True if a year control was found and successfully clicked,
    False otherwise. Not finding one is non-fatal: the caller falls back to
    an unfiltered listing plus this scraper's own click-through pagination
    across everything, which still gets there, just less efficiently -- see
    scrape()'s docstring.
    """
    year_str = str(year)

    for select in page.locator("select").all():
        try:
            options = [o.strip() for o in select.locator("option").all_inner_texts()]
        except Exception:
            continue
        if any(re.fullmatch(r"\d{4}", o) for o in options):
            if year_str not in options:
                return False
            try:
                select.select_option(label=year_str, timeout=timeout_ms)
                return True
            except PlaywrightTimeoutError:
                return False

    dropdown = page.locator(".list-filter-dropdown")
    if dropdown.count() == 0:
        return False
    try:
        dropdown.first.click(timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return False

    option = page.locator(".list-filter-dropdown li.option").filter(
        has_text=re.compile(rf"^\s*{year_str}\s*$")
    )
    if option.count() == 0:
        option = page.locator(f"li.option[data-attr-val$='/{year_str}']")
    if option.count() == 0:
        return False
    try:
        option.first.click(timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


# ---------------------------------------------------------------------------
# Listing-page rendering
# ---------------------------------------------------------------------------

def _dump_path_for_page(debug_dump_html: Optional[Path], page_num: int) -> Optional[Path]:
    """Return the path to save page *page_num*'s HTML to, or None if
    --debug-dump-html wasn't passed. Page 1 keeps the exact path given;
    later pages get a "_page{N}" suffix inserted before the extension, since
    BNY's pagination replaces the DOM's item list rather than appending to
    it (see render_and_parse_year_pass()'s docstring), so a single dump
    file can only ever show one page at a time."""
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
) -> list[NewsItem]:
    """Drive Chrome through one listing pass -- optionally filtered to
    *year* via the in-page year control (see _try_select_year()) -- and
    return every item found across every page it can paginate through.

    This parses and collects items after *each* page loads rather than
    doing one page.content() at the very end. That's not optional: BNY's
    numbered-pagination widget replaces the rendered card list on every
    click rather than appending to it -- a page only ever has 10
    `.list-item-tile` cards in the DOM at once, regardless of how many
    total releases exist -- so a single final snapshot would only ever
    contain the *last* page visited, silently dropping every page before
    it. This is very likely also the right assumption for other AEM sites
    using a numbered-pagination widget rather than an infinite-scroll
    "Load More" button (which DOES append, and is handled fine by
    collecting duplicates away via seen_urls below rather than needing two
    separate code paths).

    year=None does one unfiltered pass over whatever the site shows by
    default (BNY's default view: most recent releases across all years,
    mixed) -- used when no --year/--start-year/--end-year/--since/--until
    was given at all, so there's nothing to narrow by up front.

    Early-stop-once-past-the-target-year (unfiltered-fallback only)
    -----------------------------------------------------------------
    When *year* is given but no in-page year control could be applied (see
    _try_select_year() -- true for CME, which has no working year filter at
    all, only a JS date-range picker this scraper doesn't attempt to drive),
    the listing is still sorted newest-first, so once an entire page's items
    are all older than Jan 1 of *year*, every subsequent page can only be
    older still -- there's nothing more to find for this year, and we stop
    right there instead of trusting a fixed --max-load-more page count to
    happen to land far enough back. This is both a correctness fix (a low
    --max-load-more no longer risks silently missing a year that's simply
    further back in an unfiltered listing than the default page count
    reaches) and a politeness one (stops as soon as the answer is known,
    rather than always walking exactly --max-load-more pages regardless of
    where the year boundary actually falls).
    _UNFILTERED_YEAR_HUNT_SAFETY_CAP is the hard ceiling for this case (a
    plain --max-load-more only kicks in if it's higher, e.g. explicitly
    raised): with a real stopping signal in hand the risk isn't stopping too
    late, it's a request for a year the site has no releases for at all
    (typo, or predates the site's history) spinning through its *entire*
    history -- this caps that at a fixed, generous number of pages instead.
    """
    with sync_playwright() as p:
        browser, page = _launch_browser(p, headless, browser_channel, timeout_ms)

        logger.info("Loading %s ...", url)
        _goto_with_retry(page, url, timeout_ms)
        _wait_for_items(page, timeout_ms, item_selector)

        year_filter_applied = False
        if year is not None:
            before = _current_item_hrefs(page, item_selector)
            if _try_select_year(page, year, timeout_ms):
                year_filter_applied = True
                logger.info("Applied in-page filter: year=%d", year)
                time.sleep(polite_delay)
                _wait_for_list_change(page, before, change_timeout_ms, item_selector)
            else:
                logger.warning(
                    "Could not find/apply an in-page year filter for %d -- falling back to "
                    "paginating through the unfiltered default listing, stopping once every "
                    "item on a page is older than %d (see render_and_parse_year_pass()'s "
                    "docstring on the early-stop this triggers).",
                    year, year,
                )

        effective_max_load_more = max_load_more
        if year is not None and not year_filter_applied:
            effective_max_load_more = max(max_load_more, _UNFILTERED_YEAR_HUNT_SAFETY_CAP)

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

            if year is not None and not year_filter_applied and page_items:
                dated = [i.publish_date for i in page_items if i.publish_date is not None]
                if dated and max(dated) < date(year, 1, 1):
                    logger.info(
                        "Page %d is entirely older than %d -- stopping this unfiltered "
                        "pass early (every %d item on this listing has now been seen).",
                        page_num, year, year,
                    )
                    break

            if page_num >= effective_max_load_more:
                logger.warning(
                    "Hit --max-load-more (%d) while paginating; there may be more, older "
                    "press releases this run didn't reach. Re-run with a higher "
                    "--max-load-more if so.", effective_max_load_more,
                )
                break

            before = _current_item_hrefs(page, item_selector)
            clicked = _click_load_more(page, timeout_ms)
            if not clicked:
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


def parse_listing_page(
    html: str, base_url: str, slug: str, ticker: str, item_selector: str = DEFAULT_ITEM_SELECTOR,
) -> list[NewsItem]:
    """Parse one fully-rendered listing page into NewsItems.

    Tries item_selector's container matches first (see ITEM_SELECTOR_CASCADE
    / DEFAULT_ITEM_SELECTOR); if that finds nothing at all, falls back to
    the same-host heuristic scan (is_probable_press_release_link() +
    is_confirmed_heuristic_item()) over every link on the page -- see the
    module docstring for why both exist.
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    containers = soup.select(item_selector)
    if containers:
        logger.debug("item_selector matched %d container(s).", len(containers))
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

    if not containers:
        logger.warning(
            "item_selector (%r) matched nothing; falling back to a same-host heuristic scan. "
            "This usually means the site's markup doesn't match any of the built-in AEM "
            "component guesses -- pass --item-selector (or --debug-dump-html to inspect the "
            "rendered page and figure out the right one).",
            item_selector,
        )
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not is_probable_press_release_link(href, base_url):
                continue

            full_url = urljoin(base_url, href)
            norm_url = full_url.rstrip("/")
            if norm_url in seen_urls:
                continue

            title = anchor.get_text(separator=" ", strip=True)
            if not title:
                continue

            # Walk up to 5 ancestors looking for a card-level date, same
            # bare-text-node rule as extract_item_date()'s stage 3, but
            # starting from the anchor itself since there's no known
            # container element to scope to here.
            card_date, card_raw_text = None, ""
            node = anchor
            for _ in range(5):
                node = node.parent
                if node is None:
                    break
                card_date, card_raw_text = extract_item_date(node, anchor)
                if card_date is not None:
                    break

            if not is_confirmed_heuristic_item(title, card_date):
                continue

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

    Reuses scrape_utils.extract_date_from_detail_html() (shared with
    scrape_investorroom.py/scrape_notified.py) for the actual date-in-HTML
    heuristics, but fetches via a real Playwright page load rather than a
    plain HTTP GET, since AEM detail pages are typically client-rendered
    just like the listing page -- mirroring scrape_q4_ir.py's
    fetch_missing_dates(). Modifies *items* in place.
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

    listing_path is appended (via listing_path_suffix) onto a slug/ticker-
    derived scrape URL (news_url if set, else ir_url -- see
    utils.sources_utils.resolve_scrape_url()) -- only relevant for a future
    AEM source whose sources.yaml record points at the IR site root rather
    than the full press-releases listing URL directly. For BNY, news_url
    already IS the complete press-releases.html listing page, so the
    default (no suffix) leaves it untouched.

    listing_path precedence (highest wins):
      1. --listing-path on the CLI
      2. an "aem_listing_path" field on the matched sources.yaml record
         (not part of the documented sources.yaml schema today -- add it to
         a record only if a future AEM source actually needs it)
      3. "" (use the resolved URL as-is)

    item_selector precedence (highest wins):
      1. --item-selector on the CLI
      2. an "aem_item_selector" field on the matched sources.yaml record
         (same status as aem_listing_path above)
      3. DEFAULT_ITEM_SELECTOR

    Returns (url, slug, ticker, item_selector); url/slug/ticker are plain
    strings (never None). Warns via this module's logger for any field that
    could not be resolved (same behavior as the other scrapers'
    resolve_source() wrappers).
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

    # Shared: --url/--slug/--ticker, year/date filters, --format/--output/--dry-run
    add_common_args(parser)

    source = parser.add_argument_group("source")
    source.add_argument(
        "--listing-path", default=None, metavar="PATH",
        help=(
            "Path appended to a slug/ticker-derived scrape URL for the press-release "
            "listing, if that URL isn't already the full listing page (default: none -- "
            "BNY's sources.yaml news_url is already the complete listing URL). Overrides "
            "an 'aem_listing_path' sources.yaml field for this run."
        ),
    )
    source.add_argument(
        "--item-selector", default=None, metavar="CSS_SELECTOR",
        help=(
            "CSS selector for one press-release card container (default: a cascade of "
            "common AEM Core Component and search-result class names -- see "
            "ITEM_SELECTOR_CASCADE in this file's source). Overrides an "
            "'aem_item_selector' sources.yaml field for this run. Use --debug-dump-html "
            "to inspect the rendered page if the default doesn't match anything."
        ),
    )

    # Override --data-dir default (matches scrape_q4_ir.py/scrape_investorroom.py)
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
        help="Timeout in ms to wait for the item list to change after a load-more/next "
             "click (default: 8000)",
    )
    browser.add_argument(
        "--polite-delay", type=float, default=2.0,
        help="Seconds to pause after each in-page interaction and between detail-page "
             "fetches (default: 2.0)",
    )
    browser.add_argument(
        "--max-load-more", type=int, default=20,
        help="Safety cap on load-more/pagination clicks per run (default: 20)",
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
    requested year (using the in-page year filter -- see
    _try_select_year()), or a single unfiltered pass over the default view
    when no --year/--start-year/--end-year/--since/--until was given at
    all. Mirrors scrape_investorroom.py's per-year-pass design.

    Filtering server-side via the in-page control (when *years* is given)
    is both more reliable and far cheaper than paginating through a site's
    entire unfiltered history and filtering client-side -- see
    render_and_parse_year_pass()'s docstring for what happens if a
    requested year's control can't be found (falls back to the unfiltered
    walk, still bounded by --max-load-more).

    Note --since/--until (arbitrary date ranges, not necessarily aligned to
    whole years) fall through to a single unfiltered pass here since there's
    no in-page date-range control to drive -- finalize_and_output() still
    applies them afterward, same as always.
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
        )
        all_items.extend(items)

    return dedupe_by_url(all_items)


def scrape_and_filter(
    argv: Optional[list[str]] = None, *, write: bool = True
) -> tuple[int, list[NewsItem]]:
    """Parse args, scrape, filter/preview, and (by default) write out results.

    Split out from main() so a caller other than the command line --
    scrape_all.py -- can invoke it directly and get the scraped items back
    as a normal return value, matching every other scraper in this repo
    (scrape_investorroom.py, scrape_notified.py, scrape_q4_ir.py). See
    finalize_and_output()'s docstring for what write= controls.
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
        # A hung goto() -- as opposed to zero items coming back from a page
        # that *did* load -- is just as much a bot-mitigation symptom (see
        # _launch_browser()'s docstring), so it gets the same fallback
        # treatment below rather than a bare crash. Only swallow it here if
        # we can actually retry; otherwise let it propagate as before.
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

    # Filters, always previews, and writes CSV/JSON per --format; see
    # finalize_and_output()'s docstring for the three standardized
    # behaviors this shares with scrape_investorroom.py/scrape_notified.py/
    # scrape_q4_ir.py (preview-always, --format both, --output default
    # path), and for what write= does.
    filtered = finalize_and_output(
        all_items,
        years=years, since=args.since, until=args.until, limit=None,
        format=args.format, output=args.output, dry_run=args.dry_run,
        data_dir=args.data_dir,
        default_json_path=REPO_ROOT / "aem_news.json",
        write=write,
    )
    if not filtered:
        logger.warning("No items matched the requested filters.")

    return 0, filtered


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for standalone invocation (``python src/scrape_aem.py ...``).

    Thin wrapper around scrape_and_filter(); see that function's docstring
    for the write= behavior scrape_all.py relies on when calling it directly
    instead of going through this main().
    """
    return_code, _items = scrape_and_filter(argv)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())