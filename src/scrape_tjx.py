#!/usr/bin/env python3
"""
scrape_tjx.py

Build the year-filtered press-releases URL for TJX Companies' investor
relations site.

Why Playwright, and why headed: TJX's IR site sits behind Akamai
bot-mitigation, which returns a 403 Access Denied specifically to headless
browser sessions -- confirmed by testing headless vs. headed Chrome against
this site. A visible (headed) browser window passes; headless does not. So
this script always launches headed. Running it requires an environment
with a display (a desktop machine or a VM with a virtual display like
Xvfb) -- it will not work on a headless server/CI box as-is.

How it works:
    1. Launch a headed Chromium instance and load the base press-releases
       page.
    2. Read the exposed year-filter form's hidden fields directly out of
       the live DOM (widget hash + form_build_id -- form_build_id is a
       one-time Drupal form-cache token, regenerated on every page load,
       so it has to be read fresh each run).
    3. Build the year-filtered URL (same query-string shape as TJX's own
       "pick a year" links) and print it.

Usage:
    python scrape_tjx.py 2025

Setup (one-time):
    pip install playwright
    # Uses your machine's existing Chrome install (channel="chrome") rather
    # than Playwright's bundled Chromium. Chrome must already be installed
    # normally on this machine. If Playwright can't find it, register it
    # explicitly with:
    #     playwright install chrome
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE_URL = "https://investor.tjx.com/investors/press-releases"
FORM_ID = "widget_form_base"
DEFAULT_TIMEOUT_MS = 45_000  # per-navigation timeout

DEBUG_HTML_PATH = "tjx_debug_page.html"


@dataclass
class FormTokens:
    """The dynamic bits of the exposed-filter form we need to resubmit it."""
    widget_hash: str      # e.g. "3a25328c5338...845ec"
    form_build_id: str    # e.g. "form-49Stth9OoGllrf5hEHjBfQRqZlJy2MD7DPcs-I1nQFs"


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


def build_year_url(year: int, tokens: FormTokens) -> str:
    """Construct the filtered press-releases URL for a given year."""
    params = {
        f"{tokens.widget_hash}_year[value]": str(year),
        f"{tokens.widget_hash}_widget_id": tokens.widget_hash,
        "form_build_id": tokens.form_build_id,
        "form_id": FORM_ID,
    }
    query = urlencode(params)
    return f"{BASE_URL}?{query}#widget-form-base"


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} YEAR", file=sys.stderr)
        sys.exit(2)
    year = int(sys.argv[1])

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        page = browser.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        page.goto(BASE_URL, wait_until="networkidle")
        tokens = get_form_tokens(page)
        print(build_year_url(year, tokens))
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except PWTimeoutError as exc:
        print(f"Timed out waiting on the page: {exc}", file=sys.stderr)
        print("Make sure Chrome is installed on this machine.", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"Scraping error: {exc}", file=sys.stderr)
        sys.exit(1)