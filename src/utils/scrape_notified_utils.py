"""
utils/scrape_notified_utils.py

Shared helpers for the Notified/Drupal IR-site scraper family:
scrape_notified.py (open sites) and scrape_notified_gated.py (sites behind
bot mitigation strict enough to need a one-time Playwright step to obtain a
year-filtered URL). Both scripts scrape the exact same underlying listing
markup (Notified/Drupal Views tables or card layouts), so the low-level
HTTP-fetch, pagination, and date/time-extraction logic that used to be
duplicated (in some cases verbatim) between the two scripts lives here
instead.

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

import re
import sys
from datetime import date
from typing import Optional

try:
    from curl_cffi import requests
except ImportError:
    sys.exit(
        "Missing dependency: curl_cffi is required (plain requests does not "
        "work -- Notified/Drupal IR sites enforce TLS fingerprinting and "
        "will reject connections from it).\nInstall with: pip install curl_cffi"
    )

from utils.scrape_utils import parse_date, parse_time

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