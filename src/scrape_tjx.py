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
script reuses that same headed browser session throughout. That also means
this script inherits tjx_yearly_url.py's environment requirement: it needs
a display (a desktop machine, or a VM with Xvfb) and will not run as-is on
a headless server/CI box.

How the year filter is actually applied: an earlier version of this script
took tjx_yearly_url.py's build_year_url() output and hard-navigated
(page.goto()) straight to it. In practice that gets killed mid-request
(net::ERR_HTTP2_PROTOCOL_ERROR), with or without a Referer header, on every
attempt. The working theory: form_id=widget_form_base is a Drupal
exposed-filter widget whose year selection is applied via an in-page
AJAX/JS submission, with the URL's query string only ever used for
history/bookmarking -- a real user's browser never issues a fresh top-level
GET navigation to that exact URL, so a script doing so looks anomalous
enough to the origin/Akamai to get reset. This script therefore instead
fills in the real on-page year field and submits the real form/control,
the way a human would, and then reads the resulting DOM -- see
submit_year_filter() and scrape_year(). tjx_yearly_url.py's
get_form_tokens()/build_year_url() are still used, but only to log the
"bookmark" URL for reference, not to fetch data.

None of the following has been verified against a live fetch -- this
environment's network egress does not include investor.tjx.com -- so all of
it is a best-effort guess, adapted from scrape_notified.py's Notified/Drupal
parsing:
  - the exact HTML structure of the rendered press-release listing (row
    markup, detail-page URL shape, whether/how a time-of-day is published
    alongside the date);
  - the year field's element type (assumed to be a <select> or text
    <input> named "..._year[value]") and how the form is actually
    submitted (assumed to be a nearby submit button, falling back to
    pressing Enter in the field).
If this still comes back with 0 items, run with --debug-dump-html and send
me (or read yourself) the saved HTML -- both DETAIL_URL_RE and
submit_year_filter() below will need adjusting to match the real markup.

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
# Getting the year-filtered URL (delegates to tjx_yearly_url.py's logic,
# used only for logging -- see module docstring for why we don't navigate
# to it directly)
# ---------------------------------------------------------------------------

def get_year_url(page, year: int) -> str:
    """Read the exposed-filter form tokens off *page* and build the
    year-filtered press-releases URL, using tjx_yearly_url.py's own
    get_form_tokens()/build_year_url() so the URL-building logic lives in
    exactly one place. Used only to log a human-readable "bookmark" URL;
    we don't page.goto() it (see module docstring).
    """
    tokens = get_form_tokens(page)
    return build_year_url(year, tokens)


def submit_year_filter(page, year: int, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    """Fill in and submit the real on-page year-filter form/control, the
    way a human would, instead of hard-navigating to a hand-built URL (see
    module docstring for why that approach fails).

    Guesses, unverified against the live site:
      - the year field is named "..._year[value]" (matches the field name
        tjx_yearly_url.py's build_year_url() uses in its query string) and
        is either a <select> or a text <input>;
      - submission happens via a nearby <button type="submit"> /
        <input type="submit"> inside the same <form>; if none is found,
        falls back to pressing Enter in the field itself.
    """
    year_field = None
    for frame in page.frames:
        candidate = frame.locator('[name$="_year\\[value\\]"]').first
        try:
            count = candidate.count()
        except Exception:
            count = 0
        if count > 0:
            year_field = candidate
            break

    if year_field is None:
        raise RuntimeError(
            "Could not locate the year filter field (looked for an element "
            "whose name ends in '_year[value]'). Run with --debug-dump-html "
            "and inspect the saved HTML to find the real field, then update "
            "submit_year_filter()."
        )

    # Confirmed against a live run: the year field is a real <select>, but
    # it is NOT visible (almost certainly a native <select> hidden behind a
    # custom-styled dropdown widget, a common pattern with JS-enhanced
    # selects). Playwright's normal select_option()/click()/fill() refuse
    # to act on invisible elements, so force the interaction and dispatch
    # the events directly instead of relying on Playwright's
    # visibility-gated actions.
    tag_name = year_field.evaluate("el => el.tagName.toLowerCase()")
    if tag_name == "select":
        year_field.select_option(str(year), force=True, timeout=timeout_ms)
    else:
        year_field.evaluate(
            """(el, val) => {
                el.focus();
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            str(year),
        )

    # select_option()/the manual dispatch above already fire 'change', which
    # is enough to trigger an auto-submit-on-change handler if the site has
    # one (common for Drupal exposed-filter year selects). Also try an
    # explicit submit control in case it doesn't auto-submit, and fall back
    # to a synthetic Enter keydown (dispatched via JS, so it isn't blocked
    # by the same invisibility issue) if no submit control is found.
    submitted = False
    try:
        form = year_field.locator("xpath=ancestor::form[1]")
        if form.count() > 0:
            submit_btn = form.locator(
                'button[type="submit"], input[type="submit"], button:not([type])'
            ).first
            if submit_btn.count() > 0:
                submit_btn.click(force=True, timeout=timeout_ms)
                submitted = True
    except Exception as exc:  # noqa: BLE001 -- fall through to Enter-key fallback
        logger.debug("Submit-button lookup/click failed, falling back to Enter key: %s", exc)

    if not submitted:
        page.wait_for_timeout(500)  # give an auto-submit-on-change handler a moment to fire
        try:
            year_field.evaluate(
                "el => el.dispatchEvent(new KeyboardEvent('keydown', "
                "{ key: 'Enter', code: 'Enter', bubbles: true }))"
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort fallback
            logger.debug("Enter-key dispatch fallback failed: %s", exc)

    # Give the resulting AJAX update (or full navigation) time to settle.
    # See module docstring: we don't know whether this is a full page
    # reload or an in-place AJAX swap, so networkidle covers both cases.
    page.wait_for_load_state("networkidle", timeout=timeout_ms)


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


def log_empty_result_diagnostics(soup: "BeautifulSoup") -> None:
    """DETAIL_URL_RE is an unverified guess (see module docstring). If it
    matches nothing, print the actual hrefs seen on the page so they can be
    pasted back directly -- much faster to act on than a full HTML dump.
    """
    all_anchors = soup.find_all("a", href=True)
    logger.warning(
        "No press-release links matched DETAIL_URL_RE out of %d total <a> "
        "tag(s) on the page. DETAIL_URL_RE is an unverified guess (see "
        "module docstring) and needs fixing. Candidate hrefs below -- "
        "paste these (and their link text) back so the regex can be "
        "corrected against the real markup:",
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
            "'news-release', or 'investor' either -- the year filter "
            "submission likely isn't actually updating the listing. "
            "Try --debug-dump-html to inspect the full page.)",
            len(seen),
        )
        return

    for href, text in candidates[:40]:
        logger.warning("  href=%r text=%r", href, text)
    if len(candidates) > 40:
        logger.warning("  ... and %d more", len(candidates) - 40)


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

    if not items:
        log_empty_result_diagnostics(soup)

    return items


# ---------------------------------------------------------------------------
# Driving the headed browser
# ---------------------------------------------------------------------------

def scrape_year(year: int, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 debug_dump_html: Optional[Path] = None) -> list[NewsItem]:
    """Launch a headed Chromium session, load the base press-releases page,
    submit the real on-page year filter (see submit_year_filter() and the
    module docstring for why we don't hard-navigate to a built URL), and
    parse out press releases from the resulting DOM.

    Headed (not headless) for the same Akamai bot-mitigation reason
    documented in tjx_yearly_url.py -- see this module's docstring.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        page = browser.new_page()
        page.set_default_timeout(timeout_ms)

        page.goto(BASE_URL, wait_until="networkidle")

        # Logged for reference/debugging only -- not navigated to directly.
        try:
            bookmark_url = get_year_url(page, year)
            logger.info("Year-filtered URL for %d (reference only): %s", year, bookmark_url)
        except RuntimeError as exc:
            logger.debug("Could not compute reference bookmark URL: %s", exc)
            bookmark_url = BASE_URL

        submit_year_filter(page, year, timeout_ms=timeout_ms)

        html = page.content()
        result_url = page.url

        browser.close()

    if debug_dump_html:
        debug_dump_html.write_text(html, encoding="utf-8")
        logger.info("Saved fetched HTML to %s", debug_dump_html)

    items = parse_listing_page(html, base_url=result_url or bookmark_url)
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