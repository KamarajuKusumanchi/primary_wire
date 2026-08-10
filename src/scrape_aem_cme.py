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

Date-range filter (--use-date-filter, default-on, opt-out via --no-date-filter)
--------------------------------------------------------------------------------
The original scrape_aem.py never used this filter at all -- it always
walked CME's listing from page 1 (most recent) forward, relying on the
early-stop-once-past-the-target-year logic below to bound the work. That
is correct but wasteful for anything other than the current year: the
--debug-dump-html capture this split was written from needed 11 pages
(330 cards) to reach October 2023 -- i.e. it fetched and parsed roughly
300 press releases it had no use for to find ~20 that mattered.

A first version of this feature tried to drive CME's "Refine Your
Search" date-picker UI directly -- `.fill()`-ing the `input[name=start]`
/ `input[name=end]` text fields and clicking
`#btnSearchFilterConfirmBottom`. That version shipped untested and
turned out not to work at all: those inputs belong to a
bootstrap-datepicker widget that only registers a date (and fires the
change event CME's own JS listens for) when it's clicked inside the
widget's calendar dropdown -- a raw `.fill()` of the underlying
`<input>` doesn't touch the widget's internal state, so "Filter
Results" always submitted empty/stale values and silently did nothing.
Every run fell back to the unfiltered walk-and-stop strategy, which is
why a --year 2024 run walked all 11 pages of CME's entire unfiltered
history instead of the 4 pages that actually cover 2024.

_try_apply_cme_date_range() now sidesteps the calendar widget entirely:
CME's press-releases page is itself a client-side router keyed off the
URL fragment, `#pageNum=<n>&dateFrom=<epoch-ms>&dateTo=<epoch-ms>` --
exactly the state "Filter Results" was (unsuccessfully) trying to reach
via the UI. This navigates straight to that fragment, with dateFrom/
dateTo computed by _cme_date_range_ms() to match what CME's own picker
produces for Jan 1 - Dec 31 of the target year in the
America/New_York-pinned browser context this scraper already uses (see
_launch_browser()). CME's own pagination preserves dateFrom/dateTo in
the hash as pageNum increments, so the existing
_click_pagination_next()-driven loop below carries the filter forward
for every later page unmodified -- confirmed against a manually-driven
CME session covering three different years, each landing on the
expected 3-4 filtered pages instead of the 11+ an unfiltered walk needs.

This is still best-effort with a clean fallback: if navigating to the
filtered URL doesn't visibly narrow the result set within
--change-timeout (e.g. CME changes its hash-routing scheme), this
scraper logs a warning and falls back to the unfiltered walk-and-stop
strategy, exactly as if --use-date-filter had never been passed. Pass
--no-date-filter to force the old always-unfiltered behavior.

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

  # Restrict to a year (uses CME's date-filtered listing state by default --
  # see "Date-range filter" above; pass --no-date-filter for the old,
  # slower unfiltered walk-and-stop behavior)
  python src/scrape_aem_cme.py --year 2024 --dry-run
  python src/scrape_aem_cme.py --year 2024 --no-date-filter --show-browser --dry-run

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
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

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

from utils.scrape_aem_utils import (
    current_item_hrefs as _current_item_hrefs,
    dump_path_for_page as _dump_path_for_page,
    extract_item_date as _shared_extract_item_date,
    extract_title as _extract_title,
    fetch_missing_dates,
    first_link_in as _first_link_in,
    goto_with_retry as _goto_with_retry,
    launch_browser as _launch_browser,
    resolve_publish_date,
    resolve_source as _shared_resolve_source,
    wait_for_items as _wait_for_items,
    wait_for_list_change as _wait_for_list_change,
)
from utils.scrape_utils import (
    NewsItem as _BaseNewsItem,
    add_common_args,
    dedupe_by_url,
    finalize_and_output,
    parse_year_args,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_SLUG = "cme"
DEFAULT_TICKER = "CME"
DEFAULT_URL = "https://www.cmegroup.com/media-room/press-releases.html"

# Timezone CME's own date-range picker computes its dateFrom/dateTo
# millisecond timestamps in -- see _cme_date_range_ms()'s docstring.
# Matches _launch_browser()'s browser-context timezone_id, so a
# hand-verified epoch value and this scraper's computed one agree.
CME_TIMEZONE = ZoneInfo("America/New_York")

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


def extract_item_date(container, anchor) -> tuple[Optional[date], str]:
    """Find a publish date inside one item's card.

    Thin wrapper around utils.scrape_aem_utils.extract_item_date() binding
    it to CME's own ITEM_DATE_SELECTORS (which includes CME's dateline
    class, ".cmeBrowseAllDate") -- see that function's docstring for the
    fallback order tried.
    """
    return _shared_extract_item_date(container, anchor, ITEM_DATE_SELECTORS)


# resolve_publish_date() itself is imported from utils.scrape_aem_utils --
# identical logic between CME and BNY, no site-specific bit needed.


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

# _launch_browser(), _goto_with_retry(), _current_item_hrefs(),
# _wait_for_items(), and _wait_for_list_change() are all imported from
# utils.scrape_aem_utils -- identical between CME and BNY (same Akamai
# bot-mitigation workaround, same generic polling logic), no site-specific
# bit needed. See that module's docstring for the shared rationale.


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


def _cme_date_range_ms(year: int) -> tuple[int, int]:
    """Return (dateFrom, dateTo) as millisecond Unix timestamps for
    CME's date-range filter covering all of *year*.

    Reverse-engineered from a manually-driven "Refine Your Search"
    session: CME's bootstrap-datepicker resolves the selected Jan 1 /
    Dec 31 calendar dates to *local* midnight in the browser's own
    timezone, then hands those off as plain epoch milliseconds -- e.g.
    selecting "1/1/2024" - "12/31/2024" in a browser whose timezone is
    America/New_York produces dateFrom=1704085200000 (2024-01-01
    00:00:00 -05:00) and dateTo=1735621200000 (2024-12-31 00:00:00
    -05:00; note this is *midnight* Dec 31, not end-of-day -- CME's
    backend evidently treats dateTo as "any time on this calendar day
    or earlier", not as an exact upper bound, since it still returns
    Dec 31 releases). This only lines up with CME's own numbers because
    _launch_browser() pins the context to timezone_id="America/New_York";
    don't compute this in UTC or the local machine's timezone.
    """
    start = datetime(year, 1, 1, tzinfo=CME_TIMEZONE)
    end = datetime(year, 12, 31, tzinfo=CME_TIMEZONE)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _cme_date_filter_url(base_url: str, page_num: int, date_from_ms: int, date_to_ms: int) -> str:
    """Build one of CME's own hash-routed, date-filtered listing URLs,
    e.g.:
      https://www.cmegroup.com/media-room/press-releases.html
        #pageNum=1&dateFrom=1704085200000&dateTo=1735621200000
    """
    root = base_url.split("#", 1)[0]
    return f"{root}#pageNum={page_num}&dateFrom={date_from_ms}&dateTo={date_to_ms}"


def _navigate_full(page: Page, target_url: str, timeout_ms: int) -> None:
    """Navigate to *target_url*, guaranteeing a genuine (re)load even when
    it differs from the page's current URL only in its fragment
    ("#...") identifier.

    This is what actually broke --use-date-filter even after the URL
    fragment itself (dateFrom/dateTo/pageNum) was fixed and verified
    correct: a plain page.goto() from an already-loaded page to a URL
    that differs *only* by its "#hash" is treated by Chromium/CDP as a
    same-document navigation (effectively a history.pushState) rather
    than a real page load -- no new HTTP request, and critically, none
    of the page's bootstrap JavaScript re-runs. CME's press-releases
    listing reads its dateFrom/dateTo/pageNum filter state out of
    location.hash only during that bootstrap. render_and_parse_year_pass()
    always did a plain, hash-less page.goto(url) first (to establish a
    baseline "before" item set for change detection), so by the time
    _try_apply_cme_date_range() called page.goto(url + "#dateFrom=...")
    afterwards, the document was already loaded -- that second goto()
    silently turned into a no-op hash update, which is exactly what a
    live run showed: the correct filtered URL, followed by "Item list
    did not change within 8000ms."

    The fix: detect the same-document case and force a real reload
    instead of relying on goto(). page.reload() -- unlike goto() --
    always does a full navigation, so setting location.hash directly via
    page.evaluate() and then calling page.reload() reliably re-runs the
    page's bootstrap JS against the new hash.
    """
    if "#" in target_url:
        base, fragment = target_url.split("#", 1)
    else:
        base, fragment = target_url, ""

    current_base = page.url.split("#", 1)[0] if page.url else ""
    if current_base != base:
        # A genuine cross-document navigation -- plain goto() actually
        # loads the page and re-runs its bootstrap JS, no special
        # handling needed.
        _goto_with_retry(page, target_url, timeout_ms)
        return

    # Same document (base URL unchanged from the page's current URL,
    # only the fragment differs) -- goto() here would be a
    # same-document navigation and silently do nothing. Set the hash
    # directly, then force a real reload so the page's bootstrap JS
    # re-runs against the new hash.
    page.evaluate("h => { window.location.hash = h; }", fragment)
    page.reload(wait_until="domcontentloaded", timeout=timeout_ms)


def _try_apply_cme_date_range(
    page: Page, base_url: str, year: int, timeout_ms: int, item_selector: str,
) -> bool:
    """Best-effort, opt-out via --no-date-filter (see --use-date-filter):
    jump straight to CME's own date-filtered listing state for *year*.

    Two things had to be fixed here, in order, before this actually
    worked:

    1. WHAT to navigate to. The first version of this function tried to
       drive the "Refine Your Search" UI directly -- `.fill()`-ing the
       `input[name=start]` / `input[name=end]` text fields and clicking
       `#btnSearchFilterConfirmBottom`. That never worked at all: those
       fields belong to a bootstrap-datepicker widget that only picks up
       (and fires the change event CME's own JS listens for) a date
       selected by clicking inside its calendar dropdown -- a raw
       `.fill()` of the underlying `<input>` leaves the widget's
       internal state untouched, so "Filter Results" submitted
       empty/stale values every time and did nothing.

       Fix: CME's press-releases page is itself a client-side router
       keyed off the URL fragment --
       `#pageNum=<n>&dateFrom=<epoch-ms>&dateTo=<epoch-ms>` -- so this
       navigates straight to that fragment instead, using
       _cme_date_range_ms() to compute the epoch values CME's own
       picker would produce for Jan 1 - Dec 31 of *year*.

    2. HOW to navigate there. Even with the correct fragment, a plain
       page.goto() to it did nothing on a page that had already loaded
       the same base URL a moment earlier: Chromium treats a
       fragment-only URL change against an already-loaded document as
       a same-document navigation, which doesn't re-run the page's
       bootstrap JS -- and that bootstrap JS is the only place CME's
       listing reads location.hash. A live run showed exactly this
       symptom: the correct filtered URL logged, immediately followed
       by "Item list did not change."

       Fix: see _navigate_full(), which detects this same-document case
       and forces a genuine reload (set the hash, then page.reload())
       instead of relying on goto().

    CME's pagination control preserves dateFrom/dateTo in the hash as
    pageNum increments, so once this establishes page 1 of the filtered
    state, the existing _click_pagination_next()-driven loop in
    render_and_parse_year_pass() carries the filter forward for every
    subsequent page unmodified.

    Returns True if navigation to the filtered URL completed without a
    Playwright error -- NOT a guarantee CME actually narrowed the
    results; the caller's own _wait_for_list_change() / early-stop
    check is what actually verifies that, and it falls back to the
    unfiltered walk-and-stop strategy exactly as before if this doesn't
    pan out.
    """
    date_from_ms, date_to_ms = _cme_date_range_ms(year)
    target = _cme_date_filter_url(base_url, 1, date_from_ms, date_to_ms)
    logger.info("Navigating to CME's date-filtered listing state for %d: %s", year, target)
    try:
        _navigate_full(page, target, timeout_ms)
        _wait_for_items(page, timeout_ms, item_selector)
        return True
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        logger.debug("Navigation to CME's date-filtered listing URL failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Listing-page rendering
# ---------------------------------------------------------------------------

# _dump_path_for_page() is imported from utils.scrape_aem_utils --
# identical between CME and BNY.


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

    Early-stop-once-past-the-target-year (fallback strategy for a
    specific year, used when the date filter isn't applied)
    -----------------------------------------------------------------
    By default (--use-date-filter) this scraper jumps straight to CME's
    date-filtered listing state for *year* -- see _try_apply_cme_date_range().
    If that isn't applied (--no-date-filter was passed, or the filtered
    URL didn't visibly narrow the results), this instead paginates the
    unfiltered, newest-first listing and stops as soon as an entire
    page's items are all older than Jan 1 of *year* -- there's nothing
    older left to find. This is both a correctness fix (a low
    --max-load-more no longer risks silently missing a year that's
    simply further back than the default page count reaches) and a
    politeness one (stops as soon as the answer is known). It is NOT
    cheap for an old year: reaching October 2023 from today's listing
    took 11 pages / ~330 cards in testing, vs. 3-4 pages with the date
    filter applied. UNFILTERED_YEAR_HUNT_SAFETY_CAP is the hard ceiling
    for this fallback case.
    """
    with sync_playwright() as p:
        browser, page = _launch_browser(p, headless, browser_channel, timeout_ms)

        logger.info("Loading %s ...", url)
        _goto_with_retry(page, url, timeout_ms)
        _wait_for_items(page, timeout_ms, item_selector)

        date_filter_applied = False
        if year is not None and use_date_filter:
            before = _current_item_hrefs(page, item_selector)
            if _try_apply_cme_date_range(page, url, year, timeout_ms, item_selector):
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
                        "Navigated to CME's date-filtered listing URL for %d, but the "
                        "result list didn't change -- falling back to the unfiltered "
                        "walk-and-stop strategy. (Pass --no-date-filter to skip straight "
                        "to that strategy and silence this warning.)",
                        year,
                    )

        effective_max_load_more = max_load_more
        if year is not None and not date_filter_applied:
            effective_max_load_more = max(max_load_more, UNFILTERED_YEAR_HUNT_SAFETY_CAP)

        all_items: list[NewsItem] = []
        seen_urls: set[str] = set()
        page_num = 1

        while True:
            print(f"Scraping: {page.url}")
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

            if year is not None and page_items:
                dated = [i.publish_date for i in page_items if i.publish_date is not None]
                if not date_filter_applied:
                    if dated and max(dated) < date(year, 1, 1):
                        logger.info(
                            "Page %d is entirely older than %d -- stopping this unfiltered "
                            "pass early (every %d item on this listing has now been seen).",
                            page_num, year, year,
                        )
                        break
                else:
                    # Filtered pass sanity check: the filter claims to be
                    # scoped to `year`, so a page containing only dates
                    # past it is a sign the filter didn't actually apply
                    # the way we assumed -- warn rather than silently
                    # trust it (don't stop early on a possibly-wrong
                    # premise; --max-load-more / the exhausted-pagination
                    # break below still bound the run).
                    if dated and min(dated) > date(year, 12, 31):
                        logger.warning(
                            "Page %d of the date-filtered pass for %d has items newer than "
                            "%d-12-31 -- the CME date filter may not have applied as expected.",
                            page_num, year, year,
                        )

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

# _first_link_in() and _extract_title() are imported from
# utils.scrape_aem_utils -- identical between CME and BNY.


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

# fetch_missing_dates() is imported directly from utils.scrape_aem_utils --
# identical between CME and BNY, same signature.


# ---------------------------------------------------------------------------
# Source resolution (sources.yaml integration)
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str], slug: Optional[str], ticker: Optional[str],
    listing_path: Optional[str] = None, item_selector: Optional[str] = None,
) -> tuple[str, str, str, str]:
    """Resolve (url, slug, ticker, item_selector) from CLI args and
    sources.yaml, bound to CME's own defaults.

    Thin wrapper around utils.scrape_aem_utils.resolve_source() -- see its
    docstring for the full listing_path/item_selector precedence rules.
    """
    return _shared_resolve_source(
        url, slug, ticker, listing_path, item_selector,
        default_item_selector=DEFAULT_ITEM_SELECTOR,
        default_slug=DEFAULT_SLUG, default_ticker=DEFAULT_TICKER, default_url=DEFAULT_URL,
    )


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
        "--use-date-filter", dest="use_date_filter", action="store_true", default=True,
        help=(
            "For --year/--start-year/--end-year runs, jump directly to CME's "
            "hash-routed date-filtered listing state instead of paginating the entire "
            "unfiltered listing (default: on). See the 'Date-range filter' section of "
            "this module's docstring. Falls back automatically to the unfiltered "
            "walk-and-stop strategy if the filter doesn't visibly narrow the results."
        ),
    )
    source.add_argument(
        "--no-date-filter", dest="use_date_filter", action="store_false",
        help=(
            "Disable the date-range filter and always paginate the full unfiltered "
            "listing for --year/--start-year/--end-year runs (the old default "
            "behavior)."
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