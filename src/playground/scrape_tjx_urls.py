#!/usr/bin/env python3
"""
scrape_tjx.py

Scrape The TJX Companies' investor-relations press-release listing for a
given year and print the release links with their publish times, following
the same conventions as scrape_notified.py.

Design: Playwright is used for exactly one thing -- getting the
year-filtered listing URL out of tjx_yearly_url.py (build_year_url() /
get_form_tokens()), because that requires reading a one-time
form_build_id token out of the live, JS-rendered page. Once that URL is
in hand, this script drops Playwright entirely and fetches + parses the
listing with curl_cffi + BeautifulSoup, the same shape as
scrape_notified.py: curl_cffi impersonates Chrome's TLS/JA3 fingerprint,
which is what gets the year-filtered listing page past TJX's Akamai
bot-mitigation.

CONFIRMED against a live --debug-dump-html fetch (2025-07-10): the
year-filtered URL's server-rendered response contains the filtered rows
directly -- no client-side JS render/AJAX call needed. The rendered
markup is a classic Notified/Drupal table:

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

Detail links are served at
  /news-releases/news-release-details/<slug>
with NO "/investors/" prefix -- despite the base listing page itself
living at /investors/press-releases. (The /investors/... paths seen in
the page's nav menu, e.g. /investors/tjx-stock/stock-quote, are a
different, unrelated URL space and must NOT be matched.) DETAIL_URL_RE
and parse_listing_page() below have been updated to match this and are
confirmed working end-to-end against a real fetch: correct item count,
correct dates (including across the Mar/Nov DST boundary, e.g. "1:29 PM
EDT" vs. "3:35 PM EST"), and correctly resolved absolute URLs.

If TJX changes their markup in the future and this starts returning 0
items again, run with --debug-dump-html and inspect the saved HTML --
DETAIL_URL_RE and parse_listing_page() below will need re-adjusting to
match whatever the new real markup is.

Usage
-----
  # Default: current year, print-only preview
  python src/scrape_tjx.py

  # Specific year
  python src/scrape_tjx.py --year 2024

  # Also write CSV/JSON, same as scrape_notified.py
  python src/scrape_tjx.py --year 2024 --format json --output tjx_2024.json

Requires
--------
  pip install playwright curl_cffi beautifulsoup4 lxml
  playwright install chrome   # if Playwright can't find your Chrome install

  The listing fetch uses curl_cffi (Chrome TLS/JA3 impersonation) to get
  past TJX's Akamai bot-mitigation. See fetch_listing_html()'s docstring
  for details.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

try:
    from curl_cffi import requests
except ImportError:
    sys.exit("Missing dependency. Install with: pip install curl_cffi")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

from tjx_yearly_url import BASE_URL, DEFAULT_TIMEOUT_MS, build_year_url, get_form_tokens
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

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

SLUG = "tjx"
TICKER = "TJX"

# Match for press-release detail links on TJX's IR site. CONFIRMED against
# a live --debug-dump-html fetch (see module docstring): real detail links
# look like
#   /news-releases/news-release-details/tjx-companies-inc-announces-...
# with NO "/investors/" prefix. (An earlier version of this regex assumed
# an "/investors/" prefix by analogy with the site's nav-menu links, e.g.
# /investors/tjx-stock/stock-quote -- that was wrong and matched zero of
# the real detail links; fixed here after inspecting the actual markup.)
DETAIL_URL_RE = re.compile(
    r"/news-releases/news-release-details/[^/#?]+/?$",
    re.IGNORECASE,
)

SHORT_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b")
# M/D/YY date format (e.g. "12/09/25"; two-digit years assumed to be in the
# 2000s). CONFIRMED for TJX against a live --debug-dump-html fetch (see
# module docstring): the "col-date" cell reads e.g. "12/09/25 - 3:35 PM
# EST", and this pattern correctly parses it, including across the Mar/Nov
# DST boundary. Kept as a fallback alongside the long-form parse_date() from
# scrape_utils for consistency with scrape_notified.py's other Drupal-family
# sites, but for TJX itself this short format is the one actually in use.

logger = logging.getLogger("scrape_tjx")


class NewsItem(_BaseNewsItem):
    """TJX press-release item. Inherits fields from scrape_utils.NewsItem."""


def parse_short_date(text: str):
    """Parse M/D/YY dates like '12/09/25' (2000s assumed).

    Confirmed against TJX's actual markup (see module docstring) -- this is
    the format TJX's "col-date" cells actually use. Returns (date,
    raw_match) or (None, "").
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
# Step 1: get the year-filtered URL (Playwright, one-time, headed browser)
# ---------------------------------------------------------------------------

def get_year_url(year: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> str:
    """Return the year-filtered press-releases URL for *year*.

    This is the ONLY function in this module that touches Playwright. It
    launches a headed Chromium session (required -- see tjx_yearly_url.py's
    docstring for why headless gets 403'd), loads the base listing page just
    long enough to read the exposed-filter form's tokens, builds the URL via
    tjx_yearly_url.build_year_url(), and closes the browser immediately.
    Everything downstream of this call is plain HTTP.
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

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=False)
            try:
                page = browser.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(BASE_URL, wait_until="networkidle")
                tokens = get_form_tokens(page)
                return build_year_url(year, tokens)
            finally:
                browser.close()
    except PWTimeoutError as exc:
        raise RuntimeError(f"Timed out loading {BASE_URL} to read form tokens: {exc}") from exc
    except PWError as exc:
        raise RuntimeError(f"Browser/navigation error reading form tokens: {exc}") from exc


# ---------------------------------------------------------------------------
# Step 2: fetch + parse the listing page (plain HTTP, no Playwright)
# ---------------------------------------------------------------------------

_SESSION = None


def get_session():
    """Return a persistent HTTP session.

    Uses curl_cffi to impersonate Chrome's TLS fingerprint (JA3/JA4), which
    is what gets the year-filtered listing page past TJX's Akamai
    bot-mitigation (see module docstring).
    """
    global _SESSION
    if _SESSION is None:
        # impersonate="chrome124" sets the TLS fingerprint + HTTP/2 SETTINGS
        # to match a real Chrome 124 client, bypassing TLS-fingerprint blocks.
        _SESSION = requests.Session(impersonate="chrome124")
    return _SESSION


def fetch_listing_html(url: str, timeout: int = 30) -> str:
    """Fetch *url* via curl_cffi and return its HTML. Raises on HTTP errors."""
    resp = get_session().get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def is_detail_url(href: str) -> bool:
    return bool(DETAIL_URL_RE.search(href))


def extract_date_and_time_from_row(anchor) -> tuple[Optional[date], str, str]:
    """Find the publish date/time near a press-release link.

    Adapted from scrape_notified.py's extract_date_and_time_from_row(): try
    the enclosing <tr>'s first <td> first (classic table listing), then fall
    back to scanning nearby ancestor text, in both cases excluding the
    anchor's own (headline) text so a date mentioned in the headline itself
    isn't mistaken for the publish date/time.

    CONFIRMED against a live --debug-dump-html fetch (see module docstring):
    TJX's real markup is exactly this classic-table shape -- each <tr> has a
    first <td class="col-date"> holding the "M/D/YY - H:MM AM/PM TZ" text,
    and a second <td class="col-title"> holding the headline <a>. The
    walk-up-to-<tr>-then-find("td") branch below hits the date cell directly
    (a few parent hops up from the anchor); the ancestor-text fallback loops
    exist for robustness but aren't needed for TJX's own listing.
    """
    anchor_text = anchor.get_text(separator=" ", strip=True)

    def _without_anchor_text(text: str) -> str:
        if anchor_text and anchor_text in text:
            text = text.replace(anchor_text, " ")
        return text

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
                d, raw = parse_date(cell_text)
                if d:
                    return d, raw, parse_time(cell_text)
            row_text = _without_anchor_text(node.get_text(separator=" ", strip=True))
            d, raw = parse_short_date(row_text)
            if d:
                return d, raw, parse_time(row_text)
            d, raw = parse_date(row_text)
            if d:
                return d, raw, parse_time(row_text)
            break

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


def log_empty_result_diagnostics(soup: "BeautifulSoup") -> None:
    """DETAIL_URL_RE is confirmed against a live fetch (see module
    docstring), but TJX could change their markup in the future. If the
    regex ever matches nothing again, print the actual hrefs seen on the
    page so they can be pasted back directly -- much faster to act on than
    a full HTML dump.
    """
    all_anchors = soup.find_all("a", href=True)
    logger.warning(
        "No press-release links matched DETAIL_URL_RE out of %d total <a> "
        "tag(s) on the page. DETAIL_URL_RE was confirmed working against a "
        "live fetch (see module docstring), so this likely means TJX has "
        "changed their markup. Candidate hrefs below -- paste these (and "
        "their link text) back so the regex can be corrected against the "
        "new real markup:",
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


def parse_listing_page(html: str, base_url: str) -> list[NewsItem]:
    """Parse one fetched listing page; return the NewsItems found.

    Link discovery uses BeautifulSoup (not pd.read_html()) because the
    press-release links themselves -- the hrefs -- are what's needed, and
    pd.read_html() discards hrefs, keeping only the visible cell text.
    pd.read_html() would only help here if TJX's dates/titles live in a
    plain <table> with no useful links, which isn't the shape we need.

    CONFIRMED end-to-end against a live --debug-dump-html fetch (see module
    docstring): correct item count for the requested year, correct dates
    (including across the Mar/Nov DST boundary), and hrefs correctly
    resolved to absolute investor.tjx.com URLs via urljoin(site_root, href).
    """
    parsed = urlparse(base_url)
    site_root = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
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
        if not title:
            logger.debug("Skipping link with no title text: %s", full_url)
            continue

        seen_urls.add(norm_url)

        publish_date, raw_date_text, publish_time = extract_date_and_time_from_row(anchor)

        items.append(NewsItem(
            slug=SLUG,
            ticker=TICKER,
            title=title,
            url=full_url,
            publish_date=publish_date,
            raw_date_text=raw_date_text,
            publish_time=publish_time,
        ))

    if not items:
        log_empty_result_diagnostics(soup)

    return items


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def scrape_year(year: int, timeout: int = 30, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 debug_dump_html: Optional[Path] = None) -> list[NewsItem]:
    """Scrape TJX's press releases for *year*.

    1. get_year_url() -- the one Playwright touchpoint (see its docstring).
    2. fetch_listing_html() -- plain HTTP GET of that URL.
    3. parse_listing_page() -- BeautifulSoup parse, same shape as
       scrape_notified.py.
    """
    year_url = get_year_url(year, timeout_ms=timeout_ms)
    logger.info("Year-filtered URL for %d: %s", year, year_url)

    html = fetch_listing_html(year_url, timeout=timeout)

    if debug_dump_html:
        debug_dump_html.write_text(html, encoding="utf-8")
        logger.info("Saved fetched HTML to %s", debug_dump_html)

    items = parse_listing_page(html, base_url=year_url)
    return dedupe_by_url(items)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Shared: year/date filters (--year etc.) and the --format/--output/
    # --dry-run output trinity. --url/--slug/--ticker are also added by this
    # but unused here (TJX's source is fixed), left in for CLI consistency
    # with the other scrapers.
    add_common_args(parser)

    # Shared: --polite-delay/--timeout/--debug-dump-html/--verbose, same as
    # scrape_notified.py. --polite-delay isn't used by this script (there's
    # no pagination loop to space out), but is accepted for CLI consistency.
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

    years = parse_year_args(args)
    # --year is a repeatable list on the shared parser (add_common_args); if
    # neither --year/--start-year/--end-year was given, default to the
    # current year only for the *scrape* (years stays None so finalize_and_
    # output()'s own filtering step doesn't additionally restrict anything).
    years_to_scrape = sorted(years) if years else [datetime.now().year]

    all_items: list[NewsItem] = []
    for year in years_to_scrape:
        logger.info("Scraping TJX press releases for %d from %s", year, BASE_URL)
        try:
            items = scrape_year(
                year,
                timeout=args.timeout,
                timeout_ms=args.browser_timeout * 1000,
                debug_dump_html=args.debug_dump_html,
            )
        except RuntimeError as exc:
            logger.error("Scraping error for %d: %s", year, exc)
            continue
        except Exception as exc:
            logger.error("HTTP error scraping %d: %s", year, exc)
            continue
        logger.info("Found %d item(s) for %d.", len(items), year)
        all_items.extend(items)

    all_items = dedupe_by_url(all_items)

    finalize_and_output(
        all_items,
        years=years,
        since=args.since,
        until=args.until,
        limit=None,
        format=args.format,
        output=args.output,
        dry_run=args.dry_run,
        data_dir=DATA_DIR,
        default_json_path=REPO_ROOT / "tjx_news.json",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())