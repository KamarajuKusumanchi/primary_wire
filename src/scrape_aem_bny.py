#!/usr/bin/env python3
"""
scrape_aem_bny.py

Scrape press-release listings from BNY's (The Bank of New York Mellon
Corporation) investor-relations site and merge them into primary_wire's
daily data/YYYY/YYYY-MM-DD.csv files.

  https://www.bny.com/corporate/global/en/investor-relations/press-releases.html

This was split out of the former scrape_aem.py, which handled BNY and CME
Group in one file on the theory that "same platform (AEM) => same
scraper". That theory holds for the underlying CMS -- see the fingerprint
note at the bottom of this docstring, both sites really do run on Adobe
Experience Manager -- but AEM is a page-authoring platform, not a
prepackaged press-release-listing widget: every AEM site's IR team builds
its own bespoke listing markup, pagination control, and filter UI on top
of it, and BNY's and CME's turned out to share basically none of that.
See scrape_aem_cme.py's module docstring for the CME side and just how
different it is; this file now only carries what's true for BNY.

Page structure
--------------
  1. Each press-release card is a bespoke widget, not Adobe Core
     Components' documented List/Teaser markup: a
     `<div class="list-item-tile">` containing a `<time class="list-item-
     header__date">` and a `<div class="title"><a href="...">headline</a>
     </div>`. ITEM_SELECTOR_CASCADE's first entry is this exact class;
     the rest of the cascade is generic Adobe Core Component / search-
     result guesses, kept as a safety net in case BNY's own theme changes
     out from under this scraper -- see is_probable_press_release_link()'s
     same-host fallback for what happens if none of them match at all.

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

If BNY's markup changes, --item-selector overrides the selector cascade
without touching code. Re-run with --show-browser --debug-dump-html to
inspect the current rendered page before assuming the selectors above
still apply.

Architecture
------------
Same shape as scrape_q4_ir.py: Playwright drives a real Chrome instance
(headless by default) because AEM listings render via JS, then the fully
rendered DOM is parsed with BeautifulSoup. No private/internal API is used
-- this reads exactly what a human visiting the page would see, including
clicking through paginated "Next" controls if present.

Fingerprint (used by src/reporting/detect_ir_platform.py's aem check)
-----------------------------------------------------------------------
This confirms BNY genuinely IS built on Adobe Experience Manager (the
"aem" platform label in detect_ir_platform.py / sources_utils.PLATFORMS is
correct for this site -- what's split out here is the *scraper strategy*,
not the platform classification):
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
     Used only as a last resort (see resolve_publish_date()) since a URL
     slug's date is not guaranteed to match the actual publish date.
  5. (Opt-in via --fetch-detail-pages) fetch each still-undated item's own
     detail page in the same browser session and look there, the same
     fallback scrape_q4_ir.py uses.

Usage
-----
  # Dry-run (no files written)
  python src/scrape_aem_bny.py --dry-run

  # Scrape by slug/ticker (looked up in sources.yaml) or URL directly
  python src/scrape_aem_bny.py --slug bny --dry-run
  python src/scrape_aem_bny.py --ticker BNY --dry-run
  python src/scrape_aem_bny.py --url https://www.bny.com/corporate/global/en/investor-relations/press-releases.html --dry-run

  # Restrict to a year or range (uses the in-page year filter --
  # see _try_select_year())
  python src/scrape_aem_bny.py --year 2025 --dry-run
  python src/scrape_aem_bny.py --start-year 2023 --end-year 2025 --dry-run

  # Fetch detail pages to resolve any dates the listing page didn't expose
  python src/scrape_aem_bny.py --fetch-detail-pages --dry-run

  # Watch the browser and save the rendered HTML for debugging selectors
  python src/scrape_aem_bny.py --show-browser --debug-dump-html /tmp/bny.html --dry-run

  # Override the item-card CSS selector if BNY's markup changes
  python src/scrape_aem_bny.py --item-selector ".my-custom-card" --dry-run

  # Output as JSON
  python src/scrape_aem_bny.py --format json --output out.json --dry-run

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

DEFAULT_SLUG = "bny"
DEFAULT_TICKER = "BNY"
DEFAULT_URL = "https://www.bny.com/corporate/global/en/investor-relations/press-releases.html"

logger = logging.getLogger("scrape_aem_bny")


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------

# Tried as one combined CSS selector (BeautifulSoup .select() / Playwright
# locator both accept a comma-separated list and match any of them). Ordered
# most-specific-and-most-likely-correct first:
#
#   1. BNY's own bespoke card widget (see module docstring's "Page
#      structure" section) -- this is expected to match on essentially
#      every real run.
#   2-6. Generic Adobe Core Components / search-result / bare-<article>
#      guesses, kept only as a safety net for if BNY's theme changes.
#
# Each entry here is a *container* selector (the card), not the link itself
# -- parse_listing_page() finds the actual <a href> inside each matched
# container. This is deliberately more tolerant than matching the anchor
# directly: it survives a theme wrapping the headline in a nested <span>
# or <h3>.
ITEM_SELECTOR_CASCADE: list[str] = [
    ".list-item-tile",  # BNY's press-release card -- see module docstring
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

# Date-label CSS classes tried on each matched item container before
# falling back to a bare-text-node walk.
ITEM_DATE_SELECTORS: list[str] = [
    "time",
    ".list-item-header__date",  # BNY's dateline element -- see module docstring
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
# from mistaking a nav link for a press release.
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


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem(_BaseNewsItem):
    """BNY (AEM) press-release item.

    Inherits slug, ticker, title, url, publish_date, raw_date_text, and
    publish_date_str from scrape_utils.NewsItem. No extra fields needed.
    """


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

# /2026/01/15/some-title.html   (path-segment date, common for AEM blogs/news)
# /2026-01-15-some-title.html   (hyphenated date prefix, InvestorRoom-style)
_URL_DATE_PATH_RE = re.compile(r"/(\d{4})/(\d{1,2})/(\d{1,2})(?:/|$)")
_URL_DATE_SLUG_RE = re.compile(r"/(\d{4}-\d{2}-\d{2})-")


def date_from_url(url: str) -> Optional[date]:
    """Best-effort publish date parsed directly out of a detail-page URL.

    Only used as a last resort (see resolve_publish_date()) -- BNY's own
    URL date is not guaranteed to match the article's real publish date.
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


def extract_item_date(container, anchor) -> tuple[Optional[date], str]:
    """Find a publish date inside one item's card.

    Thin wrapper around utils.scrape_aem_utils.extract_item_date() binding
    it to BNY's own ITEM_DATE_SELECTORS (which includes BNY's dateline
    class, ".list-item-header__date") -- see that function's docstring for
    the fallback order tried.
    """
    return _shared_extract_item_date(container, anchor, ITEM_DATE_SELECTORS)


# resolve_publish_date() itself is imported from utils.scrape_aem_utils --
# identical logic between CME and BNY, no site-specific bit needed.


# ---------------------------------------------------------------------------
# Same-host heuristic fallback (used only when no item-selector match fires)
# ---------------------------------------------------------------------------

def is_probable_press_release_link(href: str, base_url: str) -> bool:
    """Necessary-but-not-sufficient check for "might be a press-release
    detail link", used only as a last-resort fallback when none of
    ITEM_SELECTOR_CASCADE matches anything on the page (i.e. BNY's markup
    has changed from what's documented above).

    Same-host, no query string/fragment, and not one of the known nav paths
    in NAV_EXCLUDE_PATHS. The caller (parse_listing_page()) still requires a
    headline-length title AND a real nearby date before accepting a link
    found this way.
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

# _launch_browser(), _goto_with_retry(), _current_item_hrefs(),
# _wait_for_items(), and _wait_for_list_change() are all imported from
# utils.scrape_aem_utils -- identical between CME and BNY (same Akamai
# bot-mitigation workaround, same generic polling logic), no site-specific
# bit needed. See that module's docstring for the shared rationale.


def _click_pagination_next(page: Page, timeout_ms: int) -> bool:
    """Click BNY's "next page" control if one is visible and not disabled.

    BNY's own bespoke numbered-pagination widget: the right arrow is a
    `<label>` (not an `<a>`/`<button>`, and with no accessible name -- it's
    an inline SVG icon) whose class contains "keyboard_arrow_right",
    alongside a handful of equally-plausible generic class-name guesses
    ("chevron-right", "arrow-right", "pagination-next") kept as a safety
    net, plus the standard accessible rel="next"/aria-label/"Next" name
    conventions in case BNY's theme is ever rebuilt on top of ordinary
    semantic markup. `:not(.disabled)` skips the arrow once BNY's own
    widget marks it spent (the left arrow starts with a literal "disabled"
    class on page 1).

    Every match within a selector group (not just the first in DOM order)
    is checked for visibility before moving to the next group -- an
    unrelated, invisible element elsewhere on the page matching one of the
    generic guesses should not stop this from finding the real, visible
    control matched by a later group. (This exact failure mode is what
    broke CME's pagination back when both sites shared one cascaded
    selector list -- see scrape_aem_cme.py's docstring for that story; it
    doesn't apply to BNY's own markup today, but the defensive per-match
    visibility check is cheap to keep.)

    Returns False if none of the above is found or clickable, which the
    caller treats as "no more pages".
    """
    selector_groups = [
        "[class*='keyboard_arrow_right']:not(.disabled)",
        "[class*='chevron-right']:not(.disabled)",
        "[class*='arrow-right']:not(.disabled)",
        "[class*='pagination-next']:not(.disabled)",
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
    """Apply BNY's in-page "Filter by" year control.

    A bespoke listbox widget, not a native <select> -- a clickable
    `.list-filter-dropdown` container that reveals a `<ul class=
    "select_ul">` of `<li class="option">` entries on click, each labeled
    with a plain 4-digit year (also carrying a `data-attr-val` ending in
    "/<year>" as a second way to match, in case the visible label text
    ever isn't a bare year).

    Tries a real <select> first regardless, in case BNY's theme is ever
    rebuilt on top of ordinary markup.

    Returns True if the year control was found and successfully clicked,
    False otherwise. Not finding one is non-fatal: the caller falls back
    to an unfiltered listing plus this scraper's own click-through
    pagination across everything, which still gets there, just less
    efficiently -- see scrape()'s docstring. In practice this should
    always succeed for BNY; a False return here is itself worth
    investigating (the site's filter UI has likely changed).
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

# _dump_path_for_page() is imported from utils.scrape_aem_utils --
# identical between CME and BNY. (BNY's pagination replaces the DOM's item
# list rather than appending to it -- see render_and_parse_year_pass()'s
# docstring -- so a single dump file can only ever show one page at a time;
# that's exactly what the shared helper's page-numbered suffix is for.)


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
    it.

    year=None does one unfiltered pass over whatever the site shows by
    default (most recent releases across all years, mixed) -- used when
    no --year/--start-year/--end-year/--since/--until was given at all.

    Unlike CME (see scrape_aem_cme.py), BNY's year filter reliably applies
    server-side, so the "paginate through the unfiltered listing and stop
    once every item is older than the target year" fallback below is a
    defensive measure for if the filter UI ever breaks, not this
    scraper's normal path.
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
                    "Could not find/apply BNY's in-page year filter for %d -- this is "
                    "unexpected (BNY normally has one; the filter UI may have changed). "
                    "Falling back to paginating through the unfiltered default listing, "
                    "stopping once every item on a page is older than %d.",
                    year, year,
                )

        effective_max_load_more = max_load_more
        if year is not None and not year_filter_applied:
            # Only relevant if the fallback above actually triggered -- see
            # its warning. 300 is a generous but bounded safety cap so a
            # request for a year BNY has no releases for (typo, or predates
            # the site's history) doesn't spin through its entire history.
            effective_max_load_more = max(max_load_more, 300)

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
                        "pass early.", page_num, year,
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

    Tries item_selector's container matches first (see ITEM_SELECTOR_CASCADE
    / DEFAULT_ITEM_SELECTOR); if that finds nothing at all, falls back to
    the same-host heuristic scan (is_probable_press_release_link() +
    is_confirmed_heuristic_item()) over every link on the page.
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
            "This usually means BNY's markup has changed -- pass --item-selector (or "
            "--debug-dump-html to inspect the rendered page and figure out the right one).",
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
    sources.yaml, bound to BNY's own defaults.

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
            "listing, if that URL isn't already the full listing page (default: none -- "
            "BNY's sources.yaml news_url is already the complete listing URL). Overrides "
            "an 'aem_listing_path' sources.yaml field for this run."
        ),
    )
    source.add_argument(
        "--item-selector", default=None, metavar="CSS_SELECTOR",
        help=(
            "CSS selector for one press-release card container (default: BNY's known "
            "'.list-item-tile' card plus a handful of generic Adobe Core Component "
            "guesses as a safety net -- see ITEM_SELECTOR_CASCADE in this file's source). "
            "Overrides an 'aem_item_selector' sources.yaml field for this run. Use "
            "--debug-dump-html to inspect the rendered page if the default doesn't match."
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
        help="Timeout in ms to wait for the item list to change after a pagination "
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
    requested year (using the in-page year filter -- see
    _try_select_year()), or a single unfiltered pass over the default view
    when no --year/--start-year/--end-year/--since/--until was given at
    all. Mirrors scrape_investorroom.py's per-year-pass design.
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
        default_json_path=REPO_ROOT / "bny_news.json",
        write=write,
    )
    if not filtered:
        logger.warning("No items matched the requested filters.")

    return 0, filtered


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for standalone invocation (``python src/scrape_aem_bny.py ...``)."""
    return_code, _items = scrape_and_filter(argv)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())