#!/usr/bin/env python3
"""
scrape_tjx.py

Scrape The TJX Companies' investor-relations press-release listing for a
given year and print the release links with their publish times, following
the same conventions as scrape_notified.py.

IMPORTANT CAVEAT (read before relying on this): TJX's IR site sits behind
Akamai bot-mitigation that returns 403 to headless HTTP clients -- this is
documented in tjx_yearly_url.py, and is why that script drives a *headed*
(non-headless) Chromium via Playwright rather than a plain HTTP GET. This
script reuses that same headed browser session for both steps (getting the
year-filtered URL, and then loading it) rather than handing the URL off to
a bare HTTP client like curl_cffi -- a second, unauthenticated HTTP request
to an Akamai-protected origin has no reason to succeed just because the
first (browser) request did. That also means this script inherits
tjx_yearly_url.py's environment requirement: it needs a display (a desktop
machine, or a VM with Xvfb) and will not run as-is on a headless server/CI
box.

The exact HTML structure of the rendered press-release listing (row markup,
detail-page URL shape, whether/how a time-of-day is published alongside the
date) was not verified against a live fetch -- this environment's network
egress does not include investor.tjx.com, so the parsing logic below is a
best-effort adaptation of scrape_notified.py's Notified/Drupal parsing
(TJX's exposed year-filter form -- widget_form_base -- is the same kind of
Drupal Views exposed filter that Notified sites use). If TJX's actual detail
links don't match DETAIL_URL_RE below, run with --debug-dump-html and adjust
the regex against the real markup.

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
  pip install playwright beautifulsoup4 lxml
  playwright install chrome   # if Playwright can't find your Chrome install
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
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4 lxml")

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeoutError
except ImportError:
    sys.exit("Missing dependency. Install with: pip install playwright && playwright install chrome")

from tjx_yearly_url import BASE_URL, DEFAULT_TIMEOUT_MS, build_year_url, get_form_tokens
from utils.scrape_utils import (
    NewsItem as _BaseNewsItem,
    add_common_args,
    configure_logging,
    dedupe_by_url,
    finalize_and_output,
    parse_date,
    parse_time,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

SLUG = "tjx"
TICKER = "TJX"

# Best-effort match for press-release detail links on TJX's IR site. Not
# verified against a live fetch (see module docstring) -- broad enough to
# catch the common Notified/Drupal-style "news-release-details" slug path
# as well as a flatter "/investors/press-releases/<slug>" shape, while still
# excluding the bare listing/section landing pages themselves.
DETAIL_URL_RE = re.compile(
    r"/investors/(?:news-releases|press-releases)/"
    r"(?:news-release-details/)?[^/#?]+/?$",
    re.IGNORECASE,
)

SHORT_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b")
# M/D/YY date format seen on Notified/Drupal-family IR listings (e.g.
# "6/26/26"; two-digit years assumed to be in the 2000s). Not confirmed for
# TJX specifically (see module docstring), but included as a fallback
# alongside the long-form parse_date() from scrape_utils, since TJX's
# exposed-filter widget is the same Drupal Views mechanism used by
# scrape_notified.py's sites, whose listing tables use this short format.

logger = logging.getLogger("scrape_tjx")


class NewsItem(_BaseNewsItem):
    """TJX press-release item. Inherits fields from scrape_utils.NewsItem."""


def parse_short_date(text: str):
    """Parse M/D/YY dates like '6/26/26' (2000s assumed).

    Not confirmed against TJX's actual markup (see module docstring); added
    as a fallback since scrape_notified.py's Drupal-family sites use this
    short format. Returns (date, raw_match) or (None, "").
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
# Getting the year-filtered URL (delegates to tjx_yearly_url.py's logic)
# ---------------------------------------------------------------------------

def get_year_url(page, year: int) -> str:
    """Read the exposed-filter form tokens off *page* and build the
    year-filtered press-releases URL, using tjx_yearly_url.py's own
    get_form_tokens()/build_year_url() so the URL-building logic lives in
    exactly one place.
    """
    tokens = get_form_tokens(page)
    return build_year_url(year, tokens)


# ---------------------------------------------------------------------------
# Listing-page parsing
# ---------------------------------------------------------------------------

def is_detail_url(href: str) -> bool:
    return bool(DETAIL_URL_RE.search(href))


def extract_date_and_time_from_row(anchor) -> tuple[Optional[date], str, str]:
    """Find the publish date/time near a press-release link.

    Adapted from scrape_notified.py's extract_date_and_time_from_row(): try
    the enclosing <tr>'s first <td> first (classic table listing), then fall
    back to scanning nearby ancestor text, in both cases excluding the
    anchor's own (headline) text so a date mentioned in the headline itself
    isn't mistaken for the publish date/time.
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


def parse_listing_page(html: str, base_url: str) -> list[NewsItem]:
    """Parse one rendered listing page; return the NewsItems found."""
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

    return items


# ---------------------------------------------------------------------------
# Driving the headed browser
# ---------------------------------------------------------------------------

def scrape_year(year: int, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 debug_dump_html: Optional[Path] = None) -> list[NewsItem]:
    """Launch a headed Chromium session, build the year-filtered URL via
    tjx_yearly_url.py's logic, load it, and parse out press releases.

    Headed (not headless) for the same Akamai bot-mitigation reason
    documented in tjx_yearly_url.py -- see this module's docstring.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        page = browser.new_page()
        page.set_default_timeout(timeout_ms)

        page.goto(BASE_URL, wait_until="networkidle")
        year_url = get_year_url(page, year)
        logger.info("Year-filtered URL for %d: %s", year, year_url)

        # Navigate to the year-filtered URL with an explicit Referer set to
        # the base press-releases page. Playwright does NOT set a Referer
        # by default on page.goto(); a real user picking a year from the
        # exposed-filter form would generate this navigation *with* one, and
        # this site's Akamai bot-mitigation is aggressive enough (see
        # tjx_yearly_url.py's docstring) that a refererless follow-up
        # request from the same session is a plausible trigger for a
        # connection-level reset. Retried once, since this could also just
        # be a transient network blip rather than anything bot-mitigation
        # related -- either way a bare retry is cheap and harmless.
        last_exc = None
        for attempt in range(2):
            try:
                page.goto(year_url, wait_until="networkidle", referer=BASE_URL)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 -- deliberately broad; see comment above
                last_exc = exc
                logger.warning("Navigation to year-filtered URL failed (attempt %d/2): %s",
                                attempt + 1, exc)
        if last_exc is not None:
            raise last_exc

        html = page.content()

        browser.close()

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

    network = parser.add_argument_group("network")
    network.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS // 1000,
                          metavar="SECONDS", help="Per-navigation timeout (default: %(default)ss).")

    debug = parser.add_argument_group("debug")
    debug.add_argument("--debug-dump-html", type=Path, default=None, metavar="PATH",
                        help="Save the fetched (rendered) listing page HTML to PATH.")
    debug.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG-level logging.")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    # --year is a repeatable list on the shared parser (add_common_args), so
    # a bare "--year 2024" gives [2024]; default to the current year if none
    # was given at all.
    if args.year:
        years_to_scrape = list(dict.fromkeys(args.year))
    else:
        years_to_scrape = [datetime.now().year]

    all_items: list[NewsItem] = []
    for year in years_to_scrape:
        logger.info("Scraping TJX press releases for %d from %s", year, BASE_URL)
        try:
            items = scrape_year(
                year,
                timeout_ms=args.timeout * 1000,
                debug_dump_html=args.debug_dump_html,
            )
        except PWTimeoutError as exc:
            logger.error("Timed out scraping %d: %s", year, exc)
            continue
        except PWError as exc:
            logger.error("Browser/navigation error scraping %d: %s", year, exc)
            continue
        except RuntimeError as exc:
            logger.error("Scraping error for %d: %s", year, exc)
            continue
        logger.info("Found %d item(s) for %d.", len(items), year)
        all_items.extend(items)

    all_items = dedupe_by_url(all_items)

    years_filter = set(years_to_scrape)
    finalize_and_output(
        all_items,
        years=years_filter,
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