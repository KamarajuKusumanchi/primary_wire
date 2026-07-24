#!/usr/bin/env python3
"""
scrape_notified_gated.py

Scrape press-release listings from Notified/Drupal investor-relations sites
(same listing markup as scrape_notified.py) that are ALSO protected by bot
mitigation (e.g. Akamai) strict enough to 403 a plain/headless request for
the year-filter widget itself. This is a variant of scrape_notified.py, not
a different platform: the listing table, detail-URL shape, and date format
are identical -- the only difference is how the year-filtered listing URL
is obtained.

Defaults are set for The TJX Companies (the first known site of this kind);
this is meant to generalize to other companies once tested against them.

Why this needs a browser at all
--------------------------------
scrape_notified.py itself paginates the *unfiltered* listing and filters by
year client-side, because on most Notified/Drupal sites the year dropdown
reloads via a form POST that isn't reflected in the URL. Some sites (TJX
confirmed) expose that same year dropdown as a Drupal Views "exposed
filter" widget whose hidden fields (a per-page ``widget_hash``-prefixed
``_widget_id`` field and a ``form_build_id`` token) CAN be resubmitted as
query-string params to get a year-filtered URL directly -- much cheaper
than paginating everything. The catch: those tokens only exist in the
live, JS-rendered DOM, and TJX's bot mitigation returns a 403 Access Denied
specifically to headless browser sessions (confirmed by testing headless
vs. headed Chrome against this site). A visible (headed) browser window
passes; headless does not. So the one-time token-reading step below always
launches Chromium headed, which requires an environment with a display (a
desktop machine, or a VM with a virtual display like Xvfb) -- it will not
work on a headless server/CI box as-is. (Left as a known limitation for
now; see the design discussion this script came out of.)

Once that year-filtered URL is in hand, this script drops Playwright
entirely and fetches + parses the listing with curl_cffi + BeautifulSoup,
exactly like scrape_notified.py: curl_cffi impersonates Chrome's TLS/JA3
fingerprint, which is what gets the year-filtered listing page itself past
the site's bot mitigation.

CONFIRMED against a live --debug-dump-html fetch of TJX (2025-07-10): the
year-filtered URL's server-rendered response contains the filtered rows
directly -- no client-side JS render/AJAX call needed. The rendered markup
is a classic Notified/Drupal table:

    <table class="nirtable ... news-table">
      <tbody>
        <tr>
          <td class="col-date">
            <div class="nir-widget--field nir-widget--news--date-time">
              12/09/25 - 3:35 PM EST
            </div>
          </td>
          <td class="col-title">
            <div class="nir-widget--field nir-widget--news--headline">
              <a href="/news-releases/news-release-details/SLUG">Title</a>
            </div>
          </td>
        </tr>
        ...

Detail links are served at /news-releases/news-release-details/<slug> with
NO "/investors/" prefix -- despite TJX's base listing page itself living at
/investors/press-releases. (The /investors/... paths seen in the page's nav
menu, e.g. /investors/tjx-stock/stock-quote, are a different, unrelated URL
space and must NOT be matched.)

If a site's markup changes in the future and this starts returning 0 items
again, run with --debug-dump-html and inspect the saved HTML -- DETAIL_URL_RE
and parse_listing_page() below will need re-adjusting to match whatever the
new real markup is.

The year filter only narrows *which* year is returned; it does not disable
the site's normal 10-items-per-page listing pager. TJX's filtered result
sets happened to fit on a single page for the years tested, which is why
earlier versions of this script fetched only page 0. Robinhood does not:
its year-filtered listing still paginates, so scrape_year() below now walks
pages the same way scrape_notified.py's scrape_one_pass() does (read the
'last »' pager link, then fetch page=1, 2, ... by appending &page=N to the
year-filtered URL) until it runs out of pages, hits an empty page, or a
page returns nothing new.

Site-specific config (temporary, single-source)
-------------------------------------------------
FORM_ID below is a hardcoded module constant tuned for TJX, NOT yet read
from sources.yaml or exposed as a CLI flag. news_releases_path, however, is
now resolved the same way scrape_notified.py does it: --news-releases-path
CLI flag > sources.yaml "news_releases_path" field for the matched source >
DEFAULT_NEWS_RELEASES_PATH. Once FORM_ID has been tested against other
companies using the same gated-Notified setup, it should get the same
treatment.

Usage
-----
  # Default: current year, print-only preview, TJX
  python src/scrape_notified_gated.py --dry-run

  # Specific year
  python src/scrape_notified_gated.py --year 2024 --dry-run

  # Also write CSV/JSON, same as scrape_notified.py
  python src/scrape_notified_gated.py --year 2024 --format json --output tjx_2024.json

  # By slug/ticker (looked up in sources.yaml for the site root only --
  # FORM_ID still comes from this script's constant)
  python src/scrape_notified_gated.py --slug tjx --dry-run

  # Different listing path (e.g. a new gated site without a sources.yaml
  # news_releases_path field yet, or a one-off override)
  python src/scrape_notified_gated.py --slug robinhood --news-releases-path press-releases --dry-run

Requires
--------
  pip install playwright curl_cffi beautifulsoup4 lxml
  playwright install chrome   # if Playwright can't find your Chrome install

  The one-time year-filter-token step uses a headed (non-headless) Chrome
  browser -- see the module docstring above for why, and for its
  headless/CI limitation. The listing fetch itself uses curl_cffi (Chrome
  TLS/JA3 impersonation) to get past the site's bot mitigation.

Run at most once per day. Requests are spaced by --polite-delay (default 15 s).
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
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

from utils.sources_utils import join_url_path
from utils.scrape_utils import (
    NewsItem as _BaseNewsItem,
    add_common_args,
    add_network_and_debug_args,
    configure_logging,
    dedupe_by_url,
    finalize_and_output,
    parse_date,
    parse_time,
    parse_year_args,
)
from utils.scrape_notified_utils import (
    MAX_PAGES,
    extract_date_and_time_from_row as _shared_extract_date_and_time_from_row,
    fetch_html as fetch_listing_html,
    find_last_page,
    parse_listing_page as _shared_parse_listing_page,
    parse_short_date,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_SLUG = "tjx"
DEFAULT_TICKER = "TJX"
DEFAULT_BASE_URL = "https://investor.tjx.com"
# Site root only. resolve_source() below derives this from sources.yaml's
# ir_url when --slug/--ticker is given; DEFAULT_BASE_URL is just the
# no-flags-at-all fallback, matching scrape_notified.py's convention.

# --- Hardcoded, single-source config (see "Site-specific config" above) ---
DEFAULT_NEWS_RELEASES_PATH = "investors/press-releases"
FORM_ID = "widget_form_base"

DEFAULT_TIMEOUT_MS = 45_000  # per-navigation timeout for the browser step

DEBUG_HTML_PATH = "notified_gated_debug_page.html"
# Where the live-DOM debug dump goes if the year-filter form's tokens can't
# be found (see get_form_tokens()). Separate from --debug-dump-html, which
# dumps the *fetched listing* HTML instead.

# Detail-page links, e.g. /news-releases/news-release-details/<slug>.
# Same shape as scrape_notified.py's DETAIL_URL_RE, but anchored to a single
# trailing segment (no intermediate section) since that's what this site's
# markup actually produces.
DETAIL_URL_RE = re.compile(
    r"/news-releases/news-release-details/[^/#?]+/?$",
    re.IGNORECASE,
)

# M/D/YY date parsing (e.g. "12/09/25"; two-digit years assumed to be in the
# 2000s), confirmed for TJX's "col-date" cell text ("12/09/25 - 3:35 PM
# EST") including correctly across the Mar/Nov DST boundary, is shared with
# scrape_notified.py -- see parse_short_date() in utils/scrape_notified_utils.py.

logger = logging.getLogger("scrape_notified_gated")


class NewsItem(_BaseNewsItem):
    """Press-release item for a gated Notified/Drupal IR site.

    Inherits slug, ticker, title, url, publish_date, raw_date_text, and
    publish_date_str from scrape_utils.NewsItem.
    """


@dataclass
class FormTokens:
    """The dynamic bits of the exposed-filter form we need to resubmit it."""
    widget_hash: str      # e.g. "3a25328c5338...845ec"
    form_build_id: str    # e.g. "form-49Stth9OoGllrf5hEHjBfQRqZlJy2MD7DPcs-I1nQFs"


# ---------------------------------------------------------------------------
# Step 1: get the year-filtered URL (Playwright, one-time, headed browser)
# ---------------------------------------------------------------------------

def _dump_debug_html(page) -> None:
    """Write the current page's full HTML (all frames) to disk for inspection."""
    try:
        with open(DEBUG_HTML_PATH, "w", encoding="utf-8") as f:
            for frame in page.frames:
                f.write(f"<!-- ===== FRAME: {frame.url} ===== -->\n")
                f.write(frame.content())
                f.write("\n\n")
    except Exception as exc:  # best-effort diagnostic, never fatal
        print(f"(couldn't write debug HTML: {exc})", file=sys.stderr)


def get_form_tokens(page) -> FormTokens:
    """
    Pull the current widget hash and form_build_id out of the year
    filter's hidden form fields. Searches the main frame *and* any
    iframes, since IR sites sometimes embed the exposed-filter widget in
    a separate frame.
    """
    widget_input = None
    build_id_input = None
    searched_frames = []

    for frame in page.frames:
        searched_frames.append(frame.url)
        candidate = frame.locator('input[name$="_widget_id"]').first
        if candidate.count() > 0 and widget_input is None:
            widget_input = candidate
        candidate_build = frame.locator('input[name="form_build_id"]').first
        if candidate_build.count() > 0 and build_id_input is None:
            build_id_input = candidate_build
        if widget_input is not None and build_id_input is not None:
            break

    if widget_input is None or build_id_input is None:
        _dump_debug_html(page)
        missing = []
        if widget_input is None:
            missing.append("widget_id field")
        if build_id_input is None:
            missing.append("form_build_id field")
        raise RuntimeError(
            f"Could not locate: {', '.join(missing)}. Searched {len(searched_frames)} "
            f"frame(s): {searched_frames}. Full page HTML dumped to "
            f"{DEBUG_HTML_PATH} for inspection -- open it and search for "
            f"'widget_id' or 'form_build_id' to see the actual field names/"
            f"structure. If this is a new site (not TJX), its exposed-filter "
            f"widget may use different field names -- this function will need "
            f"updating to match."
        )

    name_attr = widget_input.get_attribute("name") or ""
    match = re.match(r"^([0-9a-f]{40,})_widget_id$", name_attr)
    widget_hash = match.group(1) if match else widget_input.get_attribute("value")
    if not widget_hash:
        _dump_debug_html(page)
        raise RuntimeError(
            f"Found the widget_id field (name='{name_attr}') but couldn't "
            f"read a usable hash from it. Full page HTML dumped to "
            f"{DEBUG_HTML_PATH}."
        )

    form_build_id = build_id_input.get_attribute("value")
    if not form_build_id:
        _dump_debug_html(page)
        raise RuntimeError(
            f"Found form_build_id field but its value was empty. Full page "
            f"HTML dumped to {DEBUG_HTML_PATH}."
        )

    return FormTokens(widget_hash=widget_hash, form_build_id=form_build_id)


def build_year_url(
    base_url: str, year: int, tokens: FormTokens, form_id: str = FORM_ID,
    extra_params: Optional[dict[str, str]] = None,
) -> str:
    """Construct the filtered press-releases URL for a given year.

    *base_url* is the full listing-page URL (site root + news-releases
    path), e.g. https://investor.tjx.com/investors/press-releases.

    extra_params carries any site-specific query string the user passed
    directly on --url (e.g. ?category=788) -- resolve_source() strips --url
    down to its site root (see resolve_source_identity() in
    sources_utils.py) so news_releases_path can be joined onto the site
    root instead of whatever path --url happened to have, and without this
    that query string would otherwise be silently discarded, the same bug
    fixed for scrape_investorroom.py/scrape_notified.py. Merged in ahead of
    the exposed-filter's own params, which win on a key collision (those
    are the ones this function actually needs to hit the right form).
    """
    params: dict[str, str] = {}
    if extra_params:
        params.update(extra_params)
    params.update({
        f"{tokens.widget_hash}_year[value]": str(year),
        f"{tokens.widget_hash}_widget_id": tokens.widget_hash,
        "form_build_id": tokens.form_build_id,
        "form_id": form_id,
    })
    query = urlencode(params)
    return f"{base_url}?{query}#widget-form-base"


def get_year_url(base_url: str, year: int, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                  form_id: str = FORM_ID,
                  extra_params: Optional[dict[str, str]] = None) -> str:
    """Return the year-filtered press-releases URL for *year*.

    This is the ONLY function in this module that touches Playwright. It
    launches a headed Chromium session (required -- see module docstring
    for why headless gets 403'd), loads the base listing page just long
    enough to read the exposed-filter form's tokens, builds the URL via
    build_year_url(), and closes the browser immediately. Everything
    downstream of this call is plain HTTP.

    extra_params (see build_year_url()) is also appended to the page the
    browser loads to read the form tokens, so that initial load reflects
    the same site-specific filter (e.g. ?category=788) as the final
    year-filtered URL, rather than silently loading the unfiltered listing.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import TimeoutError as PWTimeoutError
    except ImportError:
        sys.exit(
            "Missing dependency. Install with: pip install playwright && "
            "playwright install chrome"
        )

    nav_url = f"{base_url}?{urlencode(extra_params)}" if extra_params else base_url

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=False)
            try:
                page = browser.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(nav_url, wait_until="networkidle")
                tokens = get_form_tokens(page)
                return build_year_url(base_url, year, tokens, form_id=form_id, extra_params=extra_params)
            finally:
                browser.close()
    except PWTimeoutError as exc:
        raise RuntimeError(f"Timed out loading {nav_url} to read form tokens: {exc}") from exc
    except PWError as exc:
        raise RuntimeError(f"Browser/navigation error reading form tokens: {exc}") from exc


# ---------------------------------------------------------------------------
# Step 2: fetch + parse the listing page (plain HTTP, no Playwright)
# ---------------------------------------------------------------------------

# get_session()/fetch_html() (imported above as fetch_listing_html) --
# curl_cffi Chrome-TLS-impersonation session, which is what gets the
# year-filtered listing page past this site's bot mitigation (see module
# docstring) -- are shared with scrape_notified.py and now live in
# utils/scrape_notified_utils.py.

def is_detail_url(href: str) -> bool:
    return bool(DETAIL_URL_RE.search(href))


def add_page_param(year_url: str, page: int) -> str:
    """Return *year_url* with a ``page=<page>`` query param added/updated.

    The year-filtered URL already carries the exposed-filter's own params
    (``<hash>_year[value]``, ``<hash>_widget_id``, ``form_build_id``,
    ``form_id``) plus a ``#widget-form-base`` fragment (see build_year_url()).
    Drupal Views pagers layer their own ``page=N`` (0-based) query param on
    top of whatever exposed-filter params are already present -- confirmed
    by the user against a live Robinhood listing, where appending
    ``&page=1`` to the year-filtered URL reaches the second page of that
    year's results. This just adds/replaces that one param, leaving
    everything else (and the fragment) untouched.
    """
    parsed = urlparse(year_url)
    # parse_qsl (not parse_qs) preserves param order and duplicate-free
    # single values, which is all we need here.
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "page"]
    params.append(("page", str(page)))
    new_query = urlencode(params)
    return urlunparse(parsed._replace(query=new_query))


def extract_date_and_time_from_row(anchor) -> tuple[Optional[date], str, str]:
    """Find the publish date/time near a press-release link.

    Thin wrapper around the shared implementation in
    utils/scrape_notified_utils.py (see that function's docstring for the
    full strategy). This script passes try_long_date_in_cell=True and
    try_short_date_in_row=True -- its original, TJX-tuned behavior of
    trying both the short M/D/YY format and the long-form date in both the
    date cell and the row text.

    Confirmed against TJX's actual markup (see module docstring): each <tr>
    has a first <td class="col-date"> holding the "M/D/YY - H:MM AM/PM TZ"
    text, and a second <td class="col-title"> holding the headline <a>. The
    walk-up-to-<tr>-then-find("td") branch hits the date cell directly (a
    few parent hops up from the anchor); the ancestor-text fallback loops
    exist for robustness on other sites using this same setup.
    """
    return _shared_extract_date_and_time_from_row(
        anchor, try_long_date_in_cell=True, try_short_date_in_row=True
    )


def log_empty_result_diagnostics(soup: "BeautifulSoup") -> None:
    """DETAIL_URL_RE is confirmed against a live fetch of TJX (see module
    docstring), but a new site's markup could differ. If the regex ever
    matches nothing, print the actual hrefs seen on the page so they can be
    pasted back directly -- much faster to act on than a full HTML dump.
    """
    all_anchors = soup.find_all("a", href=True)
    logger.warning(
        "No press-release links matched DETAIL_URL_RE out of %d total <a> "
        "tag(s) on the page. This likely means the site's markup differs "
        "from TJX's. Candidate hrefs below -- paste these (and their link "
        "text) back so the regex can be corrected against the new real "
        "markup:",
        len(all_anchors),
    )

    candidates = []
    seen = set()
    for a in all_anchors:
        href = a["href"].strip()
        if href in seen:
            continue
        seen.add(href)
        lowered = href.lower()
        if any(kw in lowered for kw in ("press-release", "news-release", "investor")):
            text = a.get_text(separator=" ", strip=True)
            candidates.append((href, text[:80]))

    if not candidates:
        logger.warning(
            "  (none of the %d unique hrefs contain 'press-release', "
            "'news-release', or 'investor' either -- the fetched page "
            "likely isn't the filtered listing, or requires a client-side "
            "render/AJAX call plain requests can't do. Try "
            "--debug-dump-html to inspect the full page.)",
            len(seen),
        )
        return

    for href, text in candidates[:40]:
        logger.warning("  href=%r text=%r", href, text)
    if len(candidates) > 40:
        logger.warning("  ... and %d more", len(candidates) - 40)


def parse_listing_page(html: str, base_url: str, slug: str, ticker: str) -> list[NewsItem]:
    """Parse one fetched listing page; return the NewsItems found.

    Thin wrapper around the shared row-parsing core in
    utils/scrape_notified_utils.py (see that function's docstring for the
    full strategy), shared with scrape_notified.py so a parsing bug fix
    only needs to be made once. Passes this script's own is_detail_url()
    (TJX's confirmed markup shape), NewsItem subclass, TJX-tuned
    extract_date_and_time_from_row() wrapper (both try_* flags True), and
    log_empty_result_diagnostics() for a markup-change diagnostic dump on
    an empty result -- use_title_fallback is left at its default (False)
    since TJX's headline itself is the link and this script has never
    needed the Paramount-style CTA fallback.

    Confirmed end-to-end against a live --debug-dump-html fetch of TJX (see
    module docstring): correct item count for the requested year, correct
    dates (including across the Mar/Nov DST boundary), and hrefs correctly
    resolved to absolute URLs.
    """
    return _shared_parse_listing_page(
        html, base_url, slug, ticker,
        is_detail_url=is_detail_url,
        news_item_cls=NewsItem,
        extract_date_and_time_from_row=extract_date_and_time_from_row,
        on_empty_result=log_empty_result_diagnostics,
    )


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def scrape_year(base_url: str, year: int, slug: str, ticker: str, timeout: int = 30,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS, form_id: str = FORM_ID,
                 debug_dump_html: Optional[Path] = None,
                 polite_delay: float = 15.0,
                 extra_params: Optional[dict[str, str]] = None) -> list[NewsItem]:
    """Scrape one gated-Notified site's press releases for *year*.

    1. get_year_url() -- the one Playwright touchpoint (see its docstring).
    2. fetch_listing_html() + parse_listing_page() -- plain HTTP GET and
       BeautifulSoup parse of page 0, same shape as scrape_notified.py.
    3. Paginate through the rest of the year's results the same way
       scrape_notified.py's scrape_one_pass() does: read the 'last »' link
       to learn the total page count, then walk page=1, 2, ... appending
       &page=N to the year-filtered URL (see add_page_param()), stopping at
       the last page, on an empty page, or if a page returns nothing new.

    The year filter itself only narrows *which* year's releases are
    returned -- it does NOT disable the site's normal 10-items-per-page
    listing pager. Some sites' filtered result sets happen to fit on one
    page (TJX, at least for the years this was tested against); others
    (confirmed: Robinhood) still paginate within a single year, so without
    this loop only the newest 10 items for that year would be returned.

    extra_params (see build_year_url()) is forwarded to get_year_url() so
    it isn't silently dropped -- add_page_param() then preserves it
    automatically on every subsequent page, since it keeps every existing
    query param except "page".
    """
    year_url = get_year_url(
        base_url, year, timeout_ms=timeout_ms, form_id=form_id, extra_params=extra_params
    )
    logger.info("Year-filtered URL for %d: %s", year, year_url)

    all_items: list[NewsItem] = []
    seen_urls: set[str] = set()
    last_page: Optional[int] = None  # discovered from page 0

    for page_num_offset in range(MAX_PAGES):
        page_idx = page_num_offset
        url = year_url if page_idx == 0 else add_page_param(year_url, page_idx)

        logger.info("Fetching listing page %d (page=%d): %s", page_num_offset + 1, page_idx, url)
        html = fetch_listing_html(url, timeout=timeout)

        if debug_dump_html and page_num_offset == 0:
            debug_dump_html.write_text(html, encoding="utf-8")
            logger.info("Saved fetched HTML to %s", debug_dump_html)

        soup = BeautifulSoup(html, "lxml")

        if last_page is None:
            last_page = find_last_page(soup)
            if last_page is not None:
                logger.info("Last page index: %d (%d total pages) for %d", last_page, last_page + 1, year)
            else:
                logger.info(
                    "No pager found for %d; assuming a single page of results.", year
                )

        page_items = parse_listing_page(html, base_url=url, slug=slug, ticker=ticker)

        new_items = [item for item in page_items if item.url.rstrip("/") not in seen_urls]
        for item in new_items:
            seen_urls.add(item.url.rstrip("/"))
        all_items.extend(new_items)

        logger.info(
            "Page %d (page=%d) for %d: %d item(s) found, %d new",
            page_num_offset + 1, page_idx, year, len(page_items), len(new_items),
        )

        if last_page is not None and page_idx >= last_page:
            logger.info("Reached last page (page=%d) for %d. Done.", last_page, year)
            break

        if not page_items:
            logger.info("Empty page at page=%d for %d. Done.", page_idx, year)
            break

        if not new_items and page_items:
            logger.warning(
                "Page %d (page=%d) for %d: all %d item(s) already seen -- stopping to avoid loop.",
                page_num_offset + 1, page_idx, year, len(page_items),
            )
            break

        time.sleep(polite_delay)

    return dedupe_by_url(all_items)


def scrape(base_url: str, slug: str, ticker: str, years: Optional[set[int]],
           timeout: int = 30, timeout_ms: int = DEFAULT_TIMEOUT_MS, form_id: str = FORM_ID,
           debug_dump_html: Optional[Path] = None, polite_delay: float = 15.0,
           extra_params: Optional[dict[str, str]] = None) -> list[NewsItem]:
    """Scrape one or more years, tolerating a per-year failure so one bad
    year (e.g. a transient block or timeout) doesn't abort the whole run.

    extra_params (see build_year_url()) is forwarded to scrape_year() for
    every year scraped.
    """
    years_to_scrape = sorted(years) if years else [datetime.now().year]

    all_items: list[NewsItem] = []
    for year in years_to_scrape:
        logger.info("Scraping %s press releases for %d from %s", slug, year, base_url)
        try:
            items = scrape_year(
                base_url, year, slug, ticker,
                timeout=timeout, timeout_ms=timeout_ms, form_id=form_id,
                debug_dump_html=debug_dump_html, polite_delay=polite_delay,
                extra_params=extra_params,
            )
        except RuntimeError as exc:
            logger.error("Scraping error for %d: %s", year, exc)
            continue
        except Exception as exc:
            logger.error("HTTP error scraping %d: %s", year, exc)
            continue
        logger.info("Found %d item(s) for %d.", len(items), year)
        all_items.extend(items)

    return dedupe_by_url(all_items)


# ---------------------------------------------------------------------------
# Source resolution (sources.yaml integration)
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str], slug: Optional[str], ticker: Optional[str],
    news_releases_path: Optional[str] = None,
) -> tuple[str, str, str, str, dict[str, str]]:
    """Resolve (listing_url, slug, ticker, news_releases_path,
    extra_query_params) from CLI args and sources.yaml.

    The site root (sources.yaml's ir_url) is looked up here, same as
    before. The listing path now follows the same precedence
    scrape_notified.py uses (highest wins):
      1. the news_releases_path argument (i.e. --news-releases-path on the CLI)
      2. the "news_releases_path" field on the matched sources.yaml record
      3. DEFAULT_NEWS_RELEASES_PATH ("investors/press-releases", tuned for TJX)

    form id (FORM_ID) is still this script's own hardcoded constant for now
    (see "Site-specific config" in the module docstring) -- NOT yet a
    sources.yaml field or CLI flag. That's future work, once this has been
    tested against other gated-Notified sites.

    When --url is provided with a path, the path is stripped so only the
    site root is retained (matching scrape_notified.py's convention),
    before news_releases_path is joined onto it.

    extra_query_params holds any query string that was present on --url
    (e.g. ?category=788) before resolve_source_identity() stripped --url
    down to its site root -- see that function's docstring. Pass it to
    scrape()/scrape_year()/get_year_url()/build_year_url() so it isn't
    silently lost -- the same bug fixed for scrape_investorroom.py and
    scrape_notified.py.
    """
    from utils.sources_utils import resolve_field_precedence, resolve_source_identity

    url, slug, ticker, record, extra_query_params = resolve_source_identity(
        url, slug, ticker,
        default_slug=DEFAULT_SLUG, default_ticker=DEFAULT_TICKER, default_url=DEFAULT_BASE_URL,
        strip_url_to_root=True, logger=logger,
    )

    news_releases_path = resolve_field_precedence(
        news_releases_path, record, "news_releases_path", DEFAULT_NEWS_RELEASES_PATH
    )

    listing_url = join_url_path(url, news_releases_path)
    return listing_url, slug, ticker, news_releases_path, extra_query_params


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Shared: --url/--slug/--ticker (site root only -- see resolve_source()),
    # year/date filters, and the --format/--output/--dry-run output trinity.
    add_common_args(parser)

    parser.add_argument(
        "--news-releases-path", default=None, metavar="PATH",
        help=(
            "Path segment for the press-releases listing page, joined onto "
            "the site root (e.g. 'press-releases' or 'news/press-releases'). "
            "Overrides sources.yaml's news_releases_path field for this run; "
            f"defaults to {DEFAULT_NEWS_RELEASES_PATH!r} if neither is set."
        ),
    )

    # Override --data-dir default (matches scrape_q4_ir.py, which historically
    # exposed this explicitly; the other scrapers previously hardcoded DATA_DIR).
    out = parser.add_argument_group("output")
    out.add_argument(
        "--data-dir", type=Path, default=DATA_DIR,
        help=f"Root of the data/ tree for --format csv (default: {DATA_DIR})",
    )

    # Shared: --polite-delay/--timeout/--debug-dump-html/--verbose, same as
    # scrape_notified.py. --polite-delay now spaces out requests between
    # pagination pages within a year (see scrape_year()'s pagination loop).
    add_network_and_debug_args(parser, default_polite_delay=15.0)

    browser = parser.add_argument_group("browser")
    browser.add_argument(
        "--browser-timeout", type=int, default=DEFAULT_TIMEOUT_MS // 1000,
        metavar="SECONDS",
        help=(
            "Timeout for the one-time headed-browser step that reads the "
            "year-filter form tokens (default: %(default)ss). Separate from "
            "--timeout, which governs the plain-HTTP listing fetch."
        ),
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    base_url, slug, ticker, news_releases_path, extra_query_params = resolve_source(
        args.url, args.slug, args.ticker, args.news_releases_path,
    )
    logger.info("Scraping %s (%s) from %s (news_releases_path=%r)", slug, ticker, base_url, news_releases_path)

    years = parse_year_args(args)

    all_items = scrape(
        base_url,
        slug=slug,
        ticker=ticker,
        years=years,
        timeout=args.timeout,
        timeout_ms=args.browser_timeout * 1000,
        debug_dump_html=args.debug_dump_html,
        polite_delay=args.polite_delay,
        extra_params=extra_query_params,
    )
    logger.info("Scraped %d item(s) total (before filtering).", len(all_items))

    finalize_and_output(
        all_items,
        years=years,
        since=args.since,
        until=args.until,
        limit=None,
        format=args.format,
        output=args.output,
        dry_run=args.dry_run,
        data_dir=args.data_dir,
        default_json_path=REPO_ROOT / "notified_gated_news.json",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())