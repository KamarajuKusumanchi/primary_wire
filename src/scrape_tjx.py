#!/usr/bin/env python3
"""
tjx_press_releases_playwright.py

Fetch TJX Companies investor-relations press releases for a given year,
using a real headless browser (Playwright) instead of `requests`.

Why Playwright, and why headed by default: TJX's IR site sits behind
Akamai bot-mitigation, which returns a 403 Access Denied specifically to
headless browser sessions -- confirmed by testing headless vs. headed
Chrome against this site. A visible (headed) browser window passes;
headless does not. So this script launches headed by default. Running it
this way requires an environment with a display (a desktop machine or a
VM with a virtual display like Xvfb) -- it will not work on a headless
server/CI box as-is.

How it works:
    1. Launch a headless Chromium instance and load the base
       press-releases page.
    2. Read the exposed year-filter form's hidden fields directly out of
       the live DOM (widget hash + form_build_id -- form_build_id is a
       one-time Drupal form-cache token, regenerated on every page load,
       so it has to be read fresh each run).
    3. Build the year-filtered URL (same query-string shape as TJX's own
       "pick a year" links) and navigate to it in the same browser
       context, so cookies/session state carry over.
    4. Scrape the rendered press-release table (date + title + link).

Usage:
    python tjx_press_releases_playwright.py 2025
    python tjx_press_releases_playwright.py 2025 --json
    python tjx_press_releases_playwright.py 2025 --url-only
    python tjx_press_releases_playwright.py 2025 --headless  # not recommended:
                                                              # TJX's Akamai
                                                              # bot-mitigation
                                                              # blocks headless
                                                              # Chrome with a
                                                              # 403

Setup (one-time):
    pip install playwright
    # Uses your machine's existing Chrome install (channel="chrome") rather
    # than Playwright's bundled Chromium. Chrome must already be installed
    # normally on this machine. If Playwright can't find it, register it
    # explicitly with:
    #     playwright install chrome
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE_URL = "https://investor.tjx.com/investors/press-releases"
FORM_ID = "widget_form_base"
DEFAULT_TIMEOUT_MS = 45_000  # per-navigation timeout


@dataclass
class FormTokens:
    """The dynamic bits of the exposed-filter form we need to resubmit it."""
    widget_hash: str      # e.g. "3a25328c5338...845ec"
    form_build_id: str    # e.g. "form-49Stth9OoGllrf5hEHjBfQRqZlJy2MD7DPcs-I1nQFs"


@dataclass
class PressRelease:
    date: str
    title: str
    url: str


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
            f"structure, or send me that file's relevant snippet."
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


DEBUG_HTML_PATH = "tjx_debug_page.html"


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


def build_year_url(year: int, tokens: FormTokens) -> str:
    """
    Construct the filtered press-releases URL for a given year. Used for
    --url-only (just report the URL, don't navigate) and as a fallback if
    the UI-driven year select can't be found on the page.
    """
    params = {
        f"{tokens.widget_hash}_year[value]": str(year),
        f"{tokens.widget_hash}_widget_id": tokens.widget_hash,
        "form_build_id": tokens.form_build_id,
        "form_id": FORM_ID,
    }
    query = urlencode(params)
    return f"{BASE_URL}?{query}#widget-form-base"


def select_year_via_ui(page, year: int) -> bool:
    """
    Interact with the actual year filter control like a real user would:
    select the year in its <select>, then submit the form. This avoids
    hand-building the filter URL and navigating straight to it, which
    Akamai's edge appears to treat as suspicious (connection-level reset /
    ERR_HTTP2_PROTOCOL_ERROR) since it skips the normal in-page interaction
    a browser would generate (referer, sequencing, exact encoding).

    Returns True if a year control was found and used, False otherwise
    (caller can fall back to the direct-URL approach).
    """
    # Try the specific naming pattern first, then loosen if needed.
    select_locator = page.locator('select[name$="_year[value]"]').first
    if select_locator.count() == 0:
        select_locator = page.locator('select[name*="_year"]').first
    if select_locator.count() == 0:
        return False

    select_locator.select_option(str(year))

    # Look for a submit control inside the same <form> as the select.
    form = select_locator.locator("xpath=ancestor::form[1]")
    submit = form.locator('button[type="submit"], input[type="submit"]').first

    with page.expect_navigation(wait_until="networkidle", timeout=DEFAULT_TIMEOUT_MS):
        if submit.count() > 0:
            submit.click()
        else:
            # Some Drupal exposed filters auto-submit on 'change' via JS
            # rather than exposing a separate submit button.
            select_locator.dispatch_event("change")

    return True


def scrape_press_release_rows(page) -> list[PressRelease]:
    """Extract (date, title, url) for every press release link on the page."""
    links = page.locator('a[href*="/news-releases/news-release-details/"]')
    count = links.count()
    releases: list[PressRelease] = []

    for i in range(count):
        link = links.nth(i)
        title = (link.inner_text() or "").strip()
        href = link.get_attribute("href") or ""
        if href.startswith("/"):
            href = f"https://investor.tjx.com{href}"

        # Walk up to the containing row and grab its first cell as the date.
        date_text = link.evaluate(
            """(el) => {
                const row = el.closest('tr');
                if (!row) return '';
                const cell = row.querySelector('td, th');
                return cell ? cell.textContent.trim() : '';
            }"""
        )
        releases.append(PressRelease(date=date_text, title=title, url=href))

    return releases


def fetch_press_releases_for_year(
    year: int, headed: bool = True, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> tuple[str, list[PressRelease]]:
    """
    Launch a browser, build the year-filtered URL, load it, and scrape the
    resulting press-release table. Returns (url, list_of_press_releases).
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not headed)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        page.goto(BASE_URL, wait_until="networkidle")

        if select_year_via_ui(page, year):
            url = page.url
        else:
            # Fallback: no year <select> found on the page (markup changed,
            # or it's not a plain <select>). Build the filter URL by hand
            # and navigate directly -- less reliable, since Akamai appears
            # to flag direct navigation to this URL shape more readily than
            # an in-page form submission. Passing `referer` at least makes
            # it look like it came from the base page rather than nowhere.
            tokens = get_form_tokens(page)
            url = build_year_url(year, tokens)
            page.goto(url, wait_until="networkidle", referer=BASE_URL)
        try:
            page.wait_for_selector(
                'a[href*="/news-releases/news-release-details/"]',
                timeout=timeout_ms,
            )
        except PWTimeoutError:
            # No matching links appeared -- possibly a year with zero
            # releases, or the page structure differs from what we expect.
            pass

        releases = scrape_press_release_rows(page)
        browser.close()

    return url, releases


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch TJX investor press-release links for a given year "
        "using a real headless browser (Playwright)."
    )
    parser.add_argument("year", type=int, help="Year to filter by, e.g. 2025")
    parser.add_argument("--json", action="store_true", help="Print results as JSON")
    parser.add_argument(
        "--url-only",
        action="store_true",
        help="Only print the constructed filter URL, don't scrape results",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless instead of the default visible browser window. "
        "TJX's site blocks headless Chrome via bot-mitigation (Akamai), so "
        "this will likely fail -- only use it if that's changed.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_MS,
        help=f"Per-navigation timeout in ms (default: {DEFAULT_TIMEOUT_MS})",
    )
    args = parser.parse_args()

    if args.url_only:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=args.headless)
            page = browser.new_page()
            page.set_default_timeout(args.timeout)
            page.goto(BASE_URL, wait_until="networkidle")
            tokens = get_form_tokens(page)
            print(build_year_url(args.year, tokens))
            browser.close()
        return

    url, releases = fetch_press_releases_for_year(
        args.year, headed=not args.headless, timeout_ms=args.timeout
    )

    if args.json:
        print(json.dumps(
            {"year": args.year, "url": url, "releases": [asdict(r) for r in releases]},
            indent=2,
        ))
    else:
        print(f"{args.year} filter URL:\n{url}\n")
        if not releases:
            print("No press releases found (or page structure has changed).")
        for r in releases:
            print(f"{r.date:>20}  {r.title}\n{'':>22}{r.url}")


if __name__ == "__main__":
    try:
        main()
    except PWTimeoutError as exc:
        print(f"Timed out waiting on the page: {exc}", file=sys.stderr)
        print(
            "Make sure you're not passing --headless (TJX's Akamai "
            "protection blocks headless Chrome with a 403), and that Chrome "
            "is installed on this machine.",
            file=sys.stderr,
        )
        sys.exit(1)
    except RuntimeError as exc:
        print(f"Scraping error: {exc}", file=sys.stderr)
        sys.exit(1)