"""
utils/scrape_notified_utils.py

Shared helpers for the Notified/Drupal IR-site scraper family:
scrape_notified.py (open sites) and scrape_notified_gated.py (sites behind
bot mitigation strict enough to need a one-time Playwright step to obtain a
year-filtered URL). Both scripts scrape the exact same underlying listing
markup (Notified/Drupal Views tables or card layouts), so the low-level
HTTP-fetch, pagination, date/time-extraction, and row/listing-parsing logic
that used to be duplicated (in some cases verbatim, and prone to silently
drifting apart when only one copy got a bug fix) between the two scripts
lives here instead. See parse_listing_page() below for the shared row-parsing
core; genuinely site-specific bits (which hrefs count as a detail link, the
NewsItem subclass to build, TJX's diagnostic dump on an empty result) are
passed in by each caller rather than hardcoded here.

This is deliberately NOT folded into utils/scrape_utils.py: scrape_utils.py
is shared across the *whole* scraper family (scrape_notified.py,
scrape_notified_gated.py, scrape_investorroom.py, scrape_q4_ir.py) and holds
platform-agnostic helpers (NewsItem, parse_date, parse_time, CLI plumbing,
CSV/JSON output). Everything in this module is specific to the
Notified/Drupal platform's markup and its curl_cffi-based fetch strategy,
which only these two scripts use -- so it gets its own file rather than
bloating the generic one.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import date
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

try:
    from curl_cffi import requests
except ImportError:
    sys.exit(
        "Missing dependency: curl_cffi is required (plain requests does not "
        "work -- Notified/Drupal IR sites enforce TLS fingerprinting and "
        "will reject connections from it).\nInstall with: pip install curl_cffi"
    )

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

from utils.scrape_utils import parse_date, parse_time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Safety cap on pagination loops. Shared by scrape_notified.py's
# scrape_one_pass() and scrape_notified_gated.py's scrape_year().
MAX_PAGES = 100

# M/D/YY date format used in Notified/Drupal listing tables (e.g. "6/26/26",
# "12/09/25"). Two-digit years are assumed to be in the 2000s.
SHORT_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b")


def parse_short_date(text: str) -> tuple[Optional[date], str]:
    """Parse M/D/YY dates like '6/26/26' or '11/24/25' (2000s assumed).

    Returns (date, raw_match) or (None, '').
    """
    m = SHORT_DATE_RE.search(text)
    if m:
        month, day, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = 2000 + yy
        raw = m.group(0)
        try:
            return date(year, month, day), raw
        except ValueError:
            pass
    return None, ""


# ---------------------------------------------------------------------------
# HTTP session (curl_cffi, Chrome TLS/JA3 impersonation)
# ---------------------------------------------------------------------------

def new_session():
    """Build and return a fresh HTTP session.

    Uses curl_cffi to impersonate Chrome's TLS fingerprint (JA3/JA4), which
    is required for Notified/Drupal IR sites that reject the standard
    Python requests/TLS stack.

    Deliberately NOT cached behind a module-level singleton. This module is
    shared by scrape_notified.py and scrape_notified_gated.py, and
    scrape_all.py runs sources concurrently in a thread pool (one worker per
    source -- see its module docstring) -- a cached ``_SESSION`` would then
    be silently shared, unsynchronized, across every thread scraping any
    Notified/Drupal source at once. curl_cffi's Session wraps a single
    libcurl handle and is not safe to use from more than one thread at a
    time, and plain requests.Session is documented as thread-unsafe too, so
    "share one session across threads" was never a safe option here even
    before parallelization made it a live one.

    Call this once per source scrape (see each caller's scrape_and_filter())
    and thread the result through explicitly as the ``session`` argument
    below, rather than reaching for a global or a threading.local() -- a
    plain function argument is simpler, is impossible to accidentally share
    across an unrelated call, and needs no cleanup bookkeeping beyond the
    caller's own ``with new_session() as session:`` block.
    """
    # impersonate="chrome124" sets the TLS fingerprint + HTTP/2 SETTINGS
    # to match a real Chrome 124 client, bypassing TLS-fingerprint blocks.
    return requests.Session(impersonate="chrome124")


def fetch_html(url: str, session, timeout: int = 30) -> str:
    """Fetch a URL and return its HTML. Raises on HTTP errors.

    ``session`` is a session built by new_session() above (or an equivalent
    requests/curl_cffi Session) -- always required, and always the caller's
    own, never a shared/global one. See new_session()'s docstring for why.
    """
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def find_last_page(soup) -> Optional[int]:
    """Read the last page index from the 'last »' pagination link.

    Shared by scrape_notified.py (unfiltered listing) and
    scrape_notified_gated.py (year-filtered listing) -- both sit on top of
    the same Drupal Views pager markup, which exposes this same 'last »'
    link (or, failing that, the highest ?page= value among pagination
    links) to say how many pages a result set spans.

    Returns the 0-based page index, or None if not found (e.g. no pager is
    rendered because the result set fits on a single page).
    """
    for a in soup.find_all("a", href=True, title=True):
        title = a.get("title", "").lower()
        if "last page" in title or title == "go to last page":
            href = a["href"]
            m = re.search(r"[?&]page=(\d+)", href)
            if m:
                return int(m.group(1))
    # Fallback: scan all pagination links for the highest ?page= value
    max_page: Optional[int] = None
    for a in soup.find_all("a", href=True):
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m:
            val = int(m.group(1))
            if max_page is None or val > max_page:
                max_page = val
    return max_page


# ---------------------------------------------------------------------------
# "Items Per Page" widget (optional --page-size feature)
# ---------------------------------------------------------------------------
#
# Some (not all) Notified/Drupal IR sites render an "Items Per Page" filter
# widget above the listing table (visible options are typically 10/25/50)
# alongside a Year filter, in a small <form method="get"> block distinct
# from the ?page= pager. Submitting it does NOT change the site's
# ?page=N pagination scheme -- ?page=1 still means "second page", just of
# whatever page size was requested -- it only changes how many items each
# page holds, which cuts the number of HTTP requests needed to walk the
# full listing roughly proportionally (50/page needs 1/5 as many requests
# as the 10/page default).
#
# This widget's field names are namespaced with a long per-block hex
# prefix (e.g. "9ec0f40d...117162e3_items_per_page") that is NOT the same
# across sites and, as far as we've confirmed, is tied to that block's own
# Drupal configuration rather than to any individual visitor's session --
# but that isn't verified across sites/time, so it is always discovered
# fresh from that run's own fetched HTML (see discover_page_size_widget()
# below) rather than ever hardcoded, the same way find_last_page() reads
# the pager instead of assuming a page count.
#
# This is undocumented behavior (there's no public API contract for it,
# just a form that happens to work over GET), confirmed hands-on for one
# site (Skyworks) but not verified across the whole Notified/Drupal family
# or over time. Treat --page-size as a "try it, and don't be surprised if
# it needs revisiting" optimization, not a guaranteed feature -- callers
# should fall back to the default page size whenever the widget isn't
# found or doesn't offer the requested size (see scrape_notified.py's
# --page-size handling for the fallback logic).

ITEMS_PER_PAGE_NAME_RE = re.compile(r"^(?P<prefix>.+)_items_per_page$")


def discover_page_size_widget(html: str) -> Optional[dict]:
    """Look for an "Items Per Page" filter widget on a listing page.

    Returns a dict with keys:
      - "prefix": the widget's field-name prefix (see module note above),
        also used as its own "..._widget_id" value.
      - "sizes": sorted list of int page sizes the <select> offers
        (e.g. [10, 25, 50]).
      - "form_id" / "form_build_id": values of the surrounding form's
        matching hidden inputs, if present (may be None).

    Returns None if no such widget is found on this page -- not every
    Notified/Drupal site has one; callers must handle that by falling back
    to the site's default (usually 10/page) pagination.
    """
    soup = BeautifulSoup(html, "lxml")
    select = soup.find("select", attrs={"name": ITEMS_PER_PAGE_NAME_RE})
    if select is None:
        return None

    m = ITEMS_PER_PAGE_NAME_RE.match(select["name"])
    prefix = m.group("prefix")

    sizes: list[int] = []
    for opt in select.find_all("option"):
        raw = opt.get("value") or opt.get_text(strip=True)
        try:
            sizes.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not sizes:
        return None

    form = select.find_parent("form")
    form_id = form_build_id = None
    if form is not None:
        fid_input = form.find("input", attrs={"name": "form_id"})
        if fid_input is not None:
            form_id = fid_input.get("value")
        fbid_input = form.find("input", attrs={"name": "form_build_id"})
        if fbid_input is not None:
            form_build_id = fbid_input.get("value")

    return {
        "prefix": prefix,
        "sizes": sorted(set(sizes)),
        "form_id": form_id,
        "form_build_id": form_build_id,
    }


def page_size_extra_params(widget: dict, size: int) -> dict[str, str]:
    """Build the extra query params that request ``size`` items per page.

    ``widget`` is a dict returned by discover_page_size_widget(); caller is
    responsible for checking ``size in widget["sizes"]`` first. Merge the
    result into the ``extra_params`` already threaded through
    listing_page_url() (see scrape_notified.py) -- it composes with that
    mechanism rather than needing a new one.

    "_year[value]": "_none" is included to explicitly request "no year
    filter" (matching the shape seen when submitting the widget with Year
    left at its default "None" option) -- primary_wire always does its own
    year filtering client-side afterward regardless (see this module's
    Notified/Drupal platform note), so this is just to keep the request
    shape as close as possible to a confirmed-working one rather than
    omitting a field and hoping Drupal treats that the same way.
    """
    prefix = widget["prefix"]
    params = {
        f"{prefix}_items_per_page": str(size),
        f"{prefix}_year[value]": "_none",
        "op": "Filter",
        f"{prefix}_widget_id": prefix,
    }
    if widget.get("form_id"):
        params["form_id"] = widget["form_id"]
    if widget.get("form_build_id"):
        params["form_build_id"] = widget["form_build_id"]
    return params


def resolve_page_size_from_html(html: str, page_size: int) -> dict[str, str]:
    """Decide how to honor a requested ``page_size`` given one already-
    fetched listing page's HTML, and return the extra query params for it
    (or {}, meaning "use the site's own default page size").

    This is the fetch-agnostic core of the --page-size feature: it takes
    HTML in and returns params out, with no opinion on how that HTML was
    obtained. scrape_notified.py's resolve_page_size_params() wraps this
    with a plain curl_cffi GET (open sites only need that). The intent is
    for scrape_notified_gated.py to eventually reuse this exact function
    unchanged: its one-time headed-Playwright step (obtain_form_tokens(),
    which already reads this same widget's _widget_id/form_build_id
    fields out of the live DOM to build a year-filtered URL -- see that
    script's module docstring) would just need to also grab that page's
    outerHTML and pass it here, rather than needing its own copy of this
    decision logic. Not wired up there yet -- see the scrape_notified_gated.py
    module docstring for why (unconfirmed whether page-size pagination is
    even a bottleneck for the sites it targets).

    If the widget doesn't offer ``page_size`` exactly, falls back to the
    largest size it does offer that's still <= page_size (e.g. asking for
    100 on a site that only goes up to 50 uses 50, not the bare default)
    rather than giving up entirely -- this matters more now that callers
    may pass a generously-large default rather than a value the caller
    already confirmed the site supports.
    """
    widget = discover_page_size_widget(html)
    if widget is None:
        logger.warning(
            "No Items Per Page widget found on this site; --page-size %d "
            "ignored, using the site's default page size.", page_size,
        )
        return {}

    sizes = widget["sizes"]
    if page_size in sizes:
        chosen = page_size
    else:
        smaller_or_equal = [s for s in sizes if s <= page_size]
        if not smaller_or_equal:
            logger.warning(
                "This site's Items Per Page widget only offers %s, all "
                "larger than the requested %d; using the site's default "
                "page size instead.", sizes, page_size,
            )
            return {}
        chosen = max(smaller_or_equal)
        logger.info(
            "This site's Items Per Page widget doesn't offer exactly %d "
            "(offers: %s); using %d instead.", page_size, sizes, chosen,
        )

    logger.info(
        "Found Items Per Page widget (prefix %s...%s); requesting %d "
        "items/page instead of the default.",
        widget["prefix"][:8], widget["prefix"][-4:], chosen,
    )
    return page_size_extra_params(widget, chosen)


# ---------------------------------------------------------------------------
# "Year" filter widget (server-side year filtering, when the site supports it)
# ---------------------------------------------------------------------------
#
# The module docstring at the top of scrape_notified.py claims the Year
# dropdown "reloads via a form POST that isn't reflected in the URL", so
# year-filtering is always done client-side after scraping the full,
# unfiltered (reverse-chronological) archive. That's true for most
# Notified/Drupal sites tested so far (e.g. AbbVie, Skyworks) -- but not
# all: some sites (confirmed on Virtu) expose this same Year dropdown as a
# proper GET-method "Views exposed filter" widget, in the very same
# <form method="get"> as the "Items Per Page" widget above (same
# per-block hex-prefixed field-name convention, e.g.
# "aac2c522...59015b_year[value]"), submittable via plain query params
# exactly like --page-size is.
#
# Discovering and using this widget when present isn't just an
# optimization the way --page-size is -- on a site like Virtu it's a
# correctness fix. Virtu's <select>'s own default-selected <option> is NOT
# "All" (value "_none"); it's the current year. So a plain, param-less
# fetch of the listing page -- which is exactly what this script's normal
# "no year filter" / binary-search-priming fetches do -- silently returns
# only the current year's press releases, not full history. For a request
# targeting a past year (e.g. --year 2025 fetched in 2026), every page the
# binary search inspects is scoped to the wrong year, so it can only ever
# find whatever *stray* past-year items happen to still be showing near
# the current year/date boundary -- which is exactly the symptom that
# prompted this: --year 2025 found only 1 stray item (a January 2026 post
# discussing Q4 2025 results) instead of the 18 real 2025 releases.
#
# Like the Items Per Page widget, this is confirmed hands-on for one site
# (Virtu) and not verified across the whole Notified/Drupal family, so it
# is always discovered fresh from that run's own fetched HTML rather than
# hardcoded, and callers must tolerate it being absent (the common case).

YEAR_FILTER_NAME_RE = re.compile(r"^(?P<prefix>.+)_year\[value\]$")


def discover_year_widget(html: str) -> Optional[dict]:
    """Look for a "Year" filter <select> widget on a listing page.

    Returns a dict with keys:
      - "prefix": the widget's field-name prefix (same convention as
        discover_page_size_widget() above; on sites that have both
        widgets, e.g. Skyworks, they share one prefix -- both selects live
        in the same widget-form-base form).
      - "years": sorted list of int years the <select> offers.
      - "has_none": whether the <select> also offers an explicit "All
        years" option (value "_none") -- true on every site seen so far,
        but checked rather than assumed.
      - "default": the value Drupal has pre-selected -- an int year, or
        "_none" if the "All" option is selected (or, as Drupal renders it
        when no option carries an explicit `selected` attribute, if
        nothing is marked selected at all; the first `<option>`, "All", is
        then the browser's own implicit default, so absence of a
        `selected` attribute is treated the same as "_none" is explicitly
        selected). This is what lets a caller detect the Virtu case: a
        plain fetch of this page is NOT "all years" whenever "default" is
        an int rather than "_none".
      - "form_id" / "form_build_id": values of the surrounding form's
        matching hidden inputs, if present (may be None).

    Returns None if no such widget is found on this page -- most
    Notified/Drupal sites don't have one; callers fall back to full-archive
    pagination with client-side year filtering (this script's original,
    still-default behavior).
    """
    soup = BeautifulSoup(html, "lxml")
    select = soup.find("select", attrs={"name": YEAR_FILTER_NAME_RE})
    if select is None:
        return None

    m = YEAR_FILTER_NAME_RE.match(select["name"])
    prefix = m.group("prefix")

    years: list[int] = []
    has_none = False
    default: Any = "_none"
    for opt in select.find_all("option"):
        raw = (opt.get("value") or "").strip()
        if raw == "_none":
            has_none = True
            if opt.has_attr("selected"):
                default = "_none"
            continue
        try:
            year = int(raw)
        except (TypeError, ValueError):
            continue
        years.append(year)
        if opt.has_attr("selected"):
            default = year

    if not years and not has_none:
        return None

    form = select.find_parent("form")
    form_id = form_build_id = None
    if form is not None:
        fid_input = form.find("input", attrs={"name": "form_id"})
        if fid_input is not None:
            form_id = fid_input.get("value")
        fbid_input = form.find("input", attrs={"name": "form_build_id"})
        if fbid_input is not None:
            form_build_id = fbid_input.get("value")

    return {
        "prefix": prefix,
        "years": sorted(years),
        "has_none": has_none,
        "default": default,
        "form_id": form_id,
        "form_build_id": form_build_id,
    }


def year_filter_extra_params(widget: dict, year: "int | str") -> dict[str, str]:
    """Build the extra query params that request a single ``year`` (an int
    from ``widget["years"]``, or the string "_none" for "All") through a
    Year filter widget discovered by discover_year_widget().

    Mirrors page_size_extra_params()'s shape (same op/widget_id/form_id/
    form_build_id fields) since it's the same underlying Drupal exposed-
    filter form -- on a site with both widgets, merging this dict with
    page_size_extra_params()'s is safe; the shared keys will just agree.
    """
    prefix = widget["prefix"]
    params = {
        f"{prefix}_year[value]": str(year),
        "op": "Filter",
        f"{prefix}_widget_id": prefix,
    }
    if widget.get("form_id"):
        params["form_id"] = widget["form_id"]
    if widget.get("form_build_id"):
        params["form_build_id"] = widget["form_build_id"]
    return params


def resolve_year_filter_from_html(
    html: Optional[str], target_years: Optional[set[int]]
) -> dict[str, str]:
    """Decide whether/how to force explicit server-side year filtering,
    given one already-fetched listing page's HTML (or None if that fetch
    failed) and the caller's target year(s). Returns extra query params to
    merge in, or {} to mean "no change; use this script's normal
    full-archive-pagination + client-side-filtering behavior".

    Two cases actively use the widget (when found):

      1. Exactly one target year is requested and the widget offers it:
         request that year explicitly. The listing then only ever
         contains that year's items, so full-archive pagination/binary
         search is skipped in favor of just walking this (typically much
         shorter) filtered result set -- see scrape()'s docstring.

      2. Multiple/no target years, but the widget's own unfiltered default
         is a specific year rather than "All" (see discover_year_widget()'s
         "default" key): explicitly request "_none" ("All"), so the
         "unfiltered" listing this script's own pagination logic then
         walks is actually unfiltered. Without this, a site like Virtu
         would silently limit even a *no-year-filter* run to whatever the
         <select>'s default happens to be (typically the current year).

    Falls back to {} (no-op) if ``html`` is None (the probe fetch itself
    failed), if no Year filter widget is found at all, or if a single
    requested year isn't one of the widget's own options and the widget's
    own default is already "_none" (nothing to fix).
    """
    if html is None:
        return {}

    widget = discover_year_widget(html)
    if widget is None:
        return {}

    if target_years and len(target_years) == 1:
        (only_year,) = tuple(target_years)
        if only_year in widget["years"]:
            logger.info(
                "Found Year filter widget (prefix %s...%s); requesting "
                "year %d directly instead of paginating the full archive.",
                widget["prefix"][:8], widget["prefix"][-4:], only_year,
            )
            return year_filter_extra_params(widget, only_year)
        logger.debug(
            "Year filter widget found but doesn't offer %d (offers: %s); "
            "falling back to full-archive pagination + client-side "
            "filtering.", only_year, widget["years"],
        )

    if widget.get("has_none") and widget.get("default") != "_none":
        logger.info(
            "Found Year filter widget (prefix %s...%s) whose unfiltered "
            "default is year %s, not \"All\"; explicitly requesting "
            "\"All\" years so full-archive pagination sees complete "
            "history.",
            widget["prefix"][:8], widget["prefix"][-4:], widget.get("default"),
        )
        return year_filter_extra_params(widget, "_none")

    return {}


# ---------------------------------------------------------------------------
# Date/time extraction near a listing-page link
# ---------------------------------------------------------------------------

def extract_date_and_time_from_row(
    anchor,
    *,
    try_long_date_in_cell: bool = False,
    try_short_date_in_row: bool = False,
) -> tuple[Optional[date], str, str]:
    """Extract the publish date and time for a press-release link on a
    Notified/Drupal listing page.

    Returns (publish_date, raw_date_text, publish_time). publish_time is a
    raw, unconverted "clock time + timezone" substring (e.g. "4:30 am EDT"),
    or "" if none is found near the date -- see parse_time() in
    utils/scrape_utils.py. It is extracted from the same candidate text used
    to find the date, since sites that publish a time put it immediately
    after the date in the same row/card text.

    Strategy 1: The listing table has a Date column as the first <td> in the
    same <tr> as (or an ancestor of) the link. Walk up to find the <tr> and
    read the first <td>'s text, trying the short M/D/YY format first and,
    if ``try_long_date_in_cell``, also the long-form "Month D, YYYY" format
    (via scrape_utils.parse_date()).

    Strategy 2: The row's summary text is scanned for a date -- the short
    M/D/YY format too if ``try_short_date_in_row``, and always the long-form
    date via scrape_utils.parse_date().

    Strategy 3: Walk up to 5 ancestors scanning all text for either date
    format (same as scrape_investorroom's extract_date_near_link).

    IMPORTANT: headlines themselves often contain a date that is NOT the
    publish date, e.g. "Apollo to Announce Second Quarter 2026 Financial
    Results on August 4, 2026" (published Jun 25, but mentions Aug 4). Sites
    that lay out releases as cards (a date label followed by an <h3><a>
    heading) rather than <table> rows have no <tr> ancestor, so Strategy 1
    never fires and the ancestor walk in Strategy 3 would otherwise match the
    headline's own embedded date on the very first iteration -- before ever
    reaching the sibling text that holds the real publish date. To avoid
    this, the anchor's own text is stripped out of every candidate string
    before searching it for a date (and, incidentally, before searching for
    a time -- a headline is very unlikely to contain a clock time, but this
    keeps the two extractions consistent).

    The two ``try_*`` keyword-only flags exist because scrape_notified.py
    and scrape_notified_gated.py were independently tuned against slightly
    different real markup (a plain Notified table vs. TJX's "col-date" /
    "col-title" table) and each caller is left with exactly the behavior it
    was tested against, rather than silently widening one for the other's
    sake:
      - scrape_notified.py calls this with both flags False (its original
        behavior: short-date-only in the cell, long-date-only in the row).
      - scrape_notified_gated.py calls this with both flags True (its
        original behavior: try both date formats in both places).
    """
    anchor_text = anchor.get_text(separator=" ", strip=True)

    # Strategy 0: a <time datetime="YYYY-MM-DD..."> element inside the
    # anchor itself. Some Notified/Drupal sites (e.g. GE Vernova) render
    # each press-release "card" as a *single* <a> wrapping the read-time
    # label, the date, the headline, and the summary all together -- there
    # is no separate ancestor holding just the date the way a <table> row
    # or a heading-plus-CTA card does. In that shape, Strategies 1-3 below
    # all rely on stripping the anchor's own text out of some surrounding
    # text before searching it for a date (see _without_anchor_text()), but
    # here the date lives *inside* that same anchor text, so it gets
    # stripped away too and the search ends up matching some unrelated
    # sibling card's date instead once it climbs high enough. Reading the
    # datetime attribute directly sidesteps all of that ambiguity, since it
    # is both inside the anchor and unambiguously machine-readable.
    time_tag = anchor.find("time")
    if time_tag is not None:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", time_tag.get("datetime", ""))
        if m:
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                raw = time_tag.get_text(strip=True) or m.group(0)
                return d, raw, parse_time(anchor_text)
            except ValueError:
                pass

    def _without_anchor_text(text: str) -> str:
        """Strip the anchor's own (title) text out of a larger text blob.

        Prevents a date mentioned inside the headline from being mistaken
        for the row's publish date.
        """
        if anchor_text and anchor_text in text:
            text = text.replace(anchor_text, " ")
        return text

    # Strategy 1: find the enclosing <tr> and read first <td>
    node = anchor
    for _ in range(10):
        node = node.parent
        if node is None:
            break
        if node.name == "tr":
            first_td = node.find("td")
            if first_td:
                cell_text = first_td.get_text(separator=" ", strip=True)
                d, raw = parse_short_date(cell_text)
                if d:
                    return d, raw, parse_time(cell_text)
                if try_long_date_in_cell:
                    d, raw = parse_date(cell_text)
                    if d:
                        return d, raw, parse_time(cell_text)
            # Also scan the full row text for dates (Strategy 2),
            # excluding the headline's own text.
            row_text = _without_anchor_text(node.get_text(separator=" ", strip=True))
            if try_short_date_in_row:
                d, raw = parse_short_date(row_text)
                if d:
                    return d, raw, parse_time(row_text)
            d, raw = parse_date(row_text)
            if d:
                return d, raw, parse_time(row_text)
            break

    # Strategy 3: walk ancestors, never searching inside the headline's own text
    node = anchor
    for _ in range(5):
        parent = node.parent
        if parent is None:
            break
        card_text = _without_anchor_text(parent.get_text(separator=" ", strip=True))
        d, raw = parse_short_date(card_text)
        if d:
            return d, raw, parse_time(card_text)
        d, raw = parse_date(card_text)
        if d:
            return d, raw, parse_time(card_text)
        node = parent

    return None, "", ""


# ---------------------------------------------------------------------------
# Listing-page parsing (shared row-parsing core)
# ---------------------------------------------------------------------------
#
# This used to be a separate parse_listing_page() reimplemented (with drift
# risk) in both scrape_notified.py and scrape_notified_gated.py. The two
# copies were identical in shape -- walk every <a href>, keep the ones that
# look like a press-release detail link, dedupe by normalized URL, pull a
# title and a date/time out of the row, build a NewsItem -- with only a
# handful of genuinely site-specific differences (which hrefs count as a
# detail link, whether to fall back to digging a real headline out of the
# row/card when the anchor text is just a generic "Read more" CTA, which
# NewsItem subclass to build, and what to do when nothing was found at all).
# Those differences are now explicit parameters/callbacks below instead of
# separately-maintained code, the same way extract_date_and_time_from_row()
# above takes try_long_date_in_cell/try_short_date_in_row rather than being
# copy-pasted per caller.

# Class-name substrings that, by Drupal's common "field--name-title" /
# "views-field-title" naming convention, typically mark the element holding
# a listing row's headline. Used by _find_title_in_container() below.
TITLE_HINT_CLASS_RE = re.compile(r"title|headline", re.IGNORECASE)

# Some Notified/Drupal sites (e.g. Paramount) lay out each release as a
# heading + summary + a separate "Read more" call-to-action link, rather
# than making the headline itself the link. When the *only* text inside the
# detail-page anchor is one of these generic CTAs (or nothing at all), the
# anchor's own text is useless as a title and we must look elsewhere in the
# row/card for the actual headline. See _row_container() / _find_title_in_container().
GENERIC_LINK_TEXT_RE = re.compile(
    r"^(?:read|learn|view|see|find\s+out)\s+more$|^(?:more|details?)$",
    re.IGNORECASE,
)


def _row_container(anchor, is_detail_url: Callable[[str], bool], max_up: int = 8):
    """Return the tightest ancestor of ``anchor`` that still contains only
    this one press-release detail link.

    Card/row layouts (no <table>) nest a release's heading, summary, and
    "Read more" link inside some shared container, but the exact tag/class
    varies by site and isn't worth hardcoding. What's true on every such
    site is that a row's container holds exactly one detail-page link (its
    own); the next ancestor up starts pulling in a sibling row's link too.
    So climb from the anchor while the ancestor still has exactly one
    matching link, and stop just before that would no longer hold.

    ``is_detail_url`` is the caller's own detail-URL predicate (each script
    has a slightly different DETAIL_URL_RE), so "one matching link" means
    what that specific site/script considers a press-release link.
    """
    container = anchor
    for _ in range(max_up):
        parent = container.parent
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        detail_links = [
            a for a in parent.find_all("a", href=True) if is_detail_url(a["href"])
        ]
        if len(detail_links) > 1:
            break
        container = parent
    return container


def _find_title_in_container(container, anchor_text: str) -> str:
    """Best-effort headline extraction from a row/card container, for sites
    (e.g. Paramount) where the only link in the row is a generic "Read
    more" CTA and the actual headline is a separate, non-linked text block.

    Tries, in order:
      1. A heading tag (h1-h6) inside the container.
      2. An element whose class name hints at a title/headline field,
         following Drupal's common "field--name-title" / "views-field-title"
         naming convention.
      3. The first substantial top-level text block in the container that
         isn't a date and isn't the (generic) anchor text itself.

    Returns "" if nothing plausible is found, so callers can fall back to
    their existing behavior.
    """
    heading = container.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if heading:
        text = heading.get_text(separator=" ", strip=True)
        if text and not GENERIC_LINK_TEXT_RE.match(text):
            return text

    for el in container.find_all(class_=True):
        classes = " ".join(el.get("class", []))
        if TITLE_HINT_CLASS_RE.search(classes):
            text = el.get_text(separator=" ", strip=True)
            if text and text != anchor_text and not GENERIC_LINK_TEXT_RE.match(text):
                return text

    for child in container.find_all(recursive=False):
        text = child.get_text(separator=" ", strip=True)
        if not text or text == anchor_text:
            continue
        if GENERIC_LINK_TEXT_RE.match(text):
            continue
        if parse_short_date(text)[0] or parse_date(text)[0]:
            continue
        return text

    return ""


def parse_listing_page(
    html: str,
    base_url: str,
    slug: str,
    ticker: str,
    *,
    is_detail_url: Callable[[str], bool],
    news_item_cls: type,
    extract_date_and_time_from_row: Callable[[Any], "tuple[Optional[date], str, str]"] = extract_date_and_time_from_row,
    use_title_fallback: bool = False,
    on_empty_result: Optional[Callable[[Any], None]] = None,
) -> list:
    """Parse one Notified/Drupal listing page into a list of news items.

    Shared core of scrape_notified.py's and scrape_notified_gated.py's
    parse_listing_page() (formerly two independently-maintained
    implementations of the same row-parsing logic). A parsing fix made here
    now applies to both callers instead of needing to be applied twice and
    risking drift.

    What's genuinely site/script-specific is passed in rather than
    hardcoded:
      - is_detail_url: which hrefs count as a press-release detail link.
        scrape_notified.py's DETAIL_URL_RE is deliberately broad (any
        multi-segment news/press/financial-releases path);
        scrape_notified_gated.py's is anchored to TJX's exact confirmed
        markup shape. See each script's own regex/docstring.
      - news_item_cls: the NewsItem dataclass to build. Each script defines
        its own trivial subclass of scrape_utils.NewsItem.
      - extract_date_and_time_from_row: defaults to this module's shared
        implementation (its own default flags: short-date-only in the
        cell, long-date-only in the row -- scrape_notified.py's original,
        tested behavior). scrape_notified_gated.py passes its own thin
        wrapper (try_long_date_in_cell=True, try_short_date_in_row=True) to
        keep its original, TJX-tuned behavior -- see that function's
        docstring for why the two differ.
      - use_title_fallback: scrape_notified.py's original behavior of
        digging into the row/card container for a real headline (via
        _row_container()/_find_title_in_container() above) when the
        detail-page anchor's own text is empty or just a generic "Read
        more" CTA (e.g. Paramount). scrape_notified_gated.py has never
        needed this (TJX's headline itself is the link), so it's left off
        (False) by default to preserve its original, tested behavior.
      - on_empty_result: optional callback (soup) -> None, invoked when no
        items were found at all. scrape_notified_gated.py passes
        log_empty_result_diagnostics() to dump candidate hrefs for
        diagnosing a markup change; scrape_notified.py has no equivalent
        yet and passes nothing.
    """
    parsed = urlparse(base_url)
    site_root = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    soup = BeautifulSoup(html, "lxml")
    items: list = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href: str = anchor["href"].strip()
        if not is_detail_url(href):
            continue

        full_url = urljoin(site_root, href)
        norm_url = full_url.rstrip("/")
        if norm_url in seen_urls:
            continue

        title = anchor.get_text(separator=" ", strip=True)
        # Some sites (e.g. GE Vernova) wrap an entire card -- read-time
        # label, date, headline, and summary -- in a single anchor, so the
        # anchor's own get_text() pulls in far more than the headline. If a
        # heading tag is nested inside the anchor, it is virtually always
        # the actual designed headline field, so prefer it over the full
        # concatenated text regardless of whether that full text also
        # happens to look non-generic.
        heading = anchor.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        if heading is not None:
            heading_text = heading.get_text(separator=" ", strip=True)
            if heading_text:
                title = heading_text
        elif use_title_fallback and (not title or GENERIC_LINK_TEXT_RE.match(title)):
            # The anchor itself is just a "Read more"-style CTA (e.g.
            # Paramount's IR site); the real headline is a separate,
            # non-linked text block elsewhere in the row/card.
            row_title = _find_title_in_container(
                _row_container(anchor, is_detail_url), title
            )
            if row_title:
                title = row_title
        if not title:
            span = anchor.find("span")
            title = span.get_text(strip=True) if span else ""
        if not title:
            logger.debug("Skipping link with no title text: %s", full_url)
            continue

        seen_urls.add(norm_url)

        publish_date, raw_date_text, publish_time = extract_date_and_time_from_row(anchor)

        items.append(news_item_cls(
            slug=slug,
            ticker=ticker,
            title=title,
            url=full_url,
            publish_date=publish_date,
            raw_date_text=raw_date_text,
            publish_time=publish_time,
        ))

    if not items and on_empty_result is not None:
        on_empty_result(soup)

    return items