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

_SESSION = None


def get_session():
    """Return a persistent HTTP session.

    Uses curl_cffi to impersonate Chrome's TLS fingerprint (JA3/JA4), which
    is required for Notified/Drupal IR sites that reject the standard
    Python requests/TLS stack.
    """
    global _SESSION
    if _SESSION is None:
        # impersonate="chrome124" sets the TLS fingerprint + HTTP/2 SETTINGS
        # to match a real Chrome 124 client, bypassing TLS-fingerprint blocks.
        _SESSION = requests.Session(impersonate="chrome124")
    return _SESSION


def fetch_html(url: str, timeout: int = 30) -> str:
    """Fetch a URL and return its HTML. Raises on HTTP errors."""
    resp = get_session().get(url, timeout=timeout)
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
        if use_title_fallback and (not title or GENERIC_LINK_TEXT_RE.match(title)):
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