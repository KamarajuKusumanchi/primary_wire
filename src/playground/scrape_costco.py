#!/usr/bin/env python3
"""
scrape_costco_news.py

Scrape press-release listings from Costco's investor relations "News" page
(https://investor.costco.com/news/default.aspx) and merge them into
primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

The page is rendered client-side (it is a Q4 Inc. IR website, like a number
of other sources/sources.yaml entries -- nvidia, qualcomm, corning, etc.).
A plain `requests.get()` only returns a "Loading..." placeholder, so this
script drives the locally installed Chrome browser via Playwright
(see src/fetch_aspx_page_v1.py for the pattern this borrows: channel="chrome",
wait_until="networkidle") and then parses the rendered DOM with
BeautifulSoup. No internal/private API is reverse-engineered -- this reads
exactly what a human visiting the page in Chrome would see.

Because --url, --slug, and --ticker are all overridable, this same script
can scrape any other Q4-powered IR news page in sources.yaml (nvidia,
qualcomm, corning, on-semiconductor, ...) by pointing it elsewhere -- the
news-details URL pattern (`/news/news-details/<year>/<slug>/default.aspx`)
is common across Q4 IR sites.

Examples:
    # Preview what's on the page right now, without writing anything
    python src/scrape_costco_news.py --dry-run

    # Scrape a specific year and merge into data/YYYY/YYYY-MM-DD.csv
    python src/scrape_costco_news.py --year 2025

    # Scrape a range of years, writing a single combined JSON file instead
    python src/scrape_costco_news.py --start-year 2023 --end-year 2025 \\
        --format json --output costco_2023_2025.json --dry-run

    # Debug: watch the browser and save the rendered HTML for inspection
    python src/scrape_costco_news.py --show-browser --debug-dump-html /tmp/costco.html --dry-run

Requires:
    pip install playwright beautifulsoup4
(Chrome itself is assumed to already be installed -- channel="chrome" reuses
it directly, no `playwright install` download needed.)

Per README.txt's "Guidelines for automated contributions": run this at most
once a day, and the default --polite-delay already spaces out repeated
in-page interactions (year/category switches, "load more" clicks) so the
site isn't hammered.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4")

try:
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except ImportError:
    sys.exit("Missing dependency. Install with: pip install playwright")

# Assumes this script lives in <repo_root>/src/scrape_costco_news.py
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"
DATA_DIR = REPO_ROOT / "data"

DEFAULT_URL = "https://investor.costco.com/news/default.aspx"
DEFAULT_SLUG = "costco"
DEFAULT_TICKER = "COST"

CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_datetime"]
SORT_FIELDS = ["publish_datetime", "slug", "ticker", "title", "url"]

CATEGORY_CHOICES = ["All News", "Sales Releases", "Earnings Releases", "Other Company Releases"]

# News-details links look like:
#   https://investor.costco.com/news/news-details/2026/Costco-Wholesale-Corporation-Reports-May-Sales-Results/default.aspx
# This pattern is shared by many other Q4-powered IR sites, just with a
# different hostname, so matching on path shape (not domain) keeps the
# script reusable for other sources.yaml entries.
NEWS_LINK_RE = re.compile(r"/news/news-details/\d{4}/[^/]+/?(?:default\.aspx)?", re.IGNORECASE)

MONTHS = (
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
DATE_PATTERNS = [
    # "Jun 18, 2026" / "June 18, 2026"
    (re.compile(rf"\b(?:{MONTHS})\.?\s+\d{{1,2}},\s*\d{{4}}\b"), ["%b %d, %Y", "%B %d, %Y"]),
    # "06/18/2026"
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"), ["%m/%d/%Y"]),
    # "2026-06-18"
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), ["%Y-%m-%d"]),
]

logger = logging.getLogger("scrape_costco_news")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
    slug: str
    ticker: str
    title: str
    url: str
    publish_date: Optional[date]
    category: str = ""
    raw_date_text: str = ""

    @property
    def publish_datetime(self) -> str:
        return self.publish_date.isoformat() if self.publish_date else ""

    def to_csv_row(self) -> dict:
        return {
            "slug": self.slug,
            "ticker": self.ticker,
            "title": self.title,
            "url": self.url,
            "publish_datetime": self.publish_datetime,
        }

    def to_json_dict(self) -> dict:
        d = asdict(self)
        d["publish_date"] = self.publish_datetime
        return d


# ---------------------------------------------------------------------------
# Browser automation
# ---------------------------------------------------------------------------

def _try_select_year(page: Page, year: int, timeout_ms: int) -> bool:
    """Best-effort: set the page's "Select year" control to `year`.

    Q4 IR sites typically render this as a native <select>, but it could
    also be a custom listbox. Try a native <select> first; fall back to a
    generic "open dropdown, click matching option" interaction. Returns
    True if a year control was found and interacted with, False if no year
    control could be located at all (caller should decide whether that's
    fatal).
    """
    year_str = str(year)

    # Strategy 1: native <select> whose options look like years.
    for select in page.locator("select").all():
        try:
            options = [o.strip() for o in select.locator("option").all_inner_texts()]
        except Exception:
            continue
        if any(re.fullmatch(r"\d{4}", o) for o in options):
            if year_str not in options:
                logger.warning(
                    "Year %s not found among available options (%s).", year_str, ", ".join(options)
                )
                return False
            select.select_option(label=year_str)
            return True

    # Strategy 2: custom dropdown -- find something that looks like the
    # year filter trigger and click through to the matching option text.
    candidates = page.get_by_text(re.compile(r"select year", re.IGNORECASE))
    if candidates.count() > 0:
        try:
            candidates.first.click(timeout=timeout_ms)
            option = page.get_by_text(re.compile(rf"^\s*{year_str}\s*$"))
            if option.count() > 0:
                option.first.click(timeout=timeout_ms)
                return True
        except PlaywrightTimeoutError:
            logger.warning("Timed out trying to click a custom year-dropdown option for %s.", year_str)

    return False


def _try_select_category(page: Page, category: str, timeout_ms: int) -> bool:
    """Best-effort: set the category filter. Same two-strategy approach as
    _try_select_year. Returns False (non-fatal) if no control is found --
    the script just proceeds with whatever the default category shows.
    """
    for select in page.locator("select").all():
        try:
            options = [o.strip() for o in select.locator("option").all_inner_texts()]
        except Exception:
            continue
        if any(c in options for c in CATEGORY_CHOICES):
            if category not in options:
                logger.warning("Category '%s' not found among options (%s).", category, ", ".join(options))
                return False
            select.select_option(label=category)
            return True

    candidates = page.get_by_text(re.compile(re.escape(category), re.IGNORECASE))
    if candidates.count() > 0:
        try:
            candidates.first.click(timeout=timeout_ms)
            return True
        except PlaywrightTimeoutError:
            pass
    return False


def _click_load_more(page: Page, timeout_ms: int) -> bool:
    """Click a "load more"/"show more"/pagination-next control if present.

    Returns True if something was clicked, False if no such control is
    visible (i.e. we've reached the end of the list).
    """
    button = page.get_by_role("button", name=re.compile(r"load more|show more|view more", re.IGNORECASE))
    if button.count() == 0:
        button = page.get_by_text(re.compile(r"load more|show more|view more", re.IGNORECASE))
    if button.count() == 0:
        return False
    try:
        if not button.first.is_visible():
            return False
        button.first.click(timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


NEWS_LINK_SELECTOR = "a[href*='/news/news-details/']"


def _current_news_hrefs(page: Page) -> set[str]:
    return set(page.locator(NEWS_LINK_SELECTOR).evaluate_all("els => els.map(e => e.getAttribute('href'))"))


def _wait_for_news_links(page: Page, timeout_ms: int) -> None:
    """Wait for at least one press-release link to attach to the DOM.

    Neither page-lifecycle event fits this site: 'domcontentloaded' fires
    before the Q4 widget has fetched/rendered anything (you'd just see the
    "Loading..." placeholder), and 'networkidle' is unreliable for SPA
    content -- background connections (analytics, chat widgets, consent
    scripts) can keep it from ever going idle, or it can report idle a
    beat before the widget's own XHR has finished rendering. Waiting
    directly for the content we actually want sidesteps both problems.
    """
    try:
        page.wait_for_selector(NEWS_LINK_SELECTOR, timeout=timeout_ms, state="attached")
    except PlaywrightTimeoutError:
        logger.warning(
            "Timed out after %dms waiting for any '%s' link to appear. The "
            "page may be slow/blocked, or its markup may not match the "
            "expected pattern -- continuing anyway so --debug-dump-html can "
            "still capture whatever did load.",
            timeout_ms, NEWS_LINK_SELECTOR,
        )


def _wait_for_list_change(
    page: Page,
    previous_hrefs: set[str],
    timeout_ms: int,
    poll_interval_ms: int = 200,
    settle_ms: int = 400,
) -> set[str]:
    """Poll until the set of rendered press-release links differs from
    `previous_hrefs`, then return the new set.

    This is what actually replaces networkidle after an in-page action
    (year/category switch, load-more click): the question we care about
    is "has the list content changed", not "has the network gone quiet" --
    those are not the same thing, and on this kind of AJAX-driven widget
    the gap between them is exactly where networkidle gives false
    positives/negatives. After a change is detected, pause briefly
    (settle_ms) in case dates/titles are still streaming in, then return.
    If nothing changes before the timeout (e.g. selecting the year that
    was already showing, or genuinely no more pages to load), log and
    return the most recent snapshot rather than raising.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    current = previous_hrefs
    while time.monotonic() < deadline:
        current = _current_news_hrefs(page)
        if current != previous_hrefs:
            time.sleep(settle_ms / 1000)
            return _current_news_hrefs(page)
        time.sleep(poll_interval_ms / 1000)
    logger.warning(
        "List of press-release links did not change within %dms after the last "
        "action. Proceeding with whatever is currently rendered (this can be "
        "expected -- e.g. re-selecting the same year, or no more pages to load).",
        timeout_ms,
    )
    return current


def render_news_page(
    url: str,
    year: Optional[int],
    category: str,
    headless: bool,
    browser_channel: str,
    timeout_ms: int,
    change_timeout_ms: int,
    polite_delay: float,
    max_load_more: int,
    debug_dump_html: Optional[Path],
) -> str:
    """Drive Chrome to the news page, apply filters, expand pagination, and
    return the fully rendered HTML.
    """
    with sync_playwright() as p:
        launch_kwargs = {"headless": headless}
        if browser_channel:
            launch_kwargs["channel"] = browser_channel
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        page.set_default_timeout(timeout_ms)

        logger.info("Loading %s ...", url)
        # domcontentloaded gets us past initial navigation quickly and
        # reliably; the actual press-release list is fetched/rendered by JS
        # afterwards, so we explicitly wait for it below rather than relying
        # on a page-lifecycle event to imply it's there.
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        _wait_for_news_links(page, timeout_ms)

        if category and category != "All News":
            logger.info("Selecting category: %s", category)
            before = _current_news_hrefs(page)
            if _try_select_category(page, category, timeout_ms):
                time.sleep(polite_delay)
                _wait_for_list_change(page, before, change_timeout_ms)
            else:
                logger.warning("Could not apply category filter '%s'; continuing with default view.", category)

        if year is not None:
            logger.info("Selecting year: %s", year)
            before = _current_news_hrefs(page)
            if _try_select_year(page, year, timeout_ms):
                time.sleep(polite_delay)
                _wait_for_list_change(page, before, change_timeout_ms)
            else:
                logger.warning(
                    "Could not find/apply a year filter for %s on the page. "
                    "Falling back to whatever the page shows by default, then "
                    "filtering scraped results by year client-side.",
                    year,
                )

        clicks = 0
        while clicks < max_load_more:
            before = _current_news_hrefs(page)
            if not _click_load_more(page, timeout_ms):
                break
            time.sleep(polite_delay)
            after = _wait_for_list_change(page, before, change_timeout_ms)
            clicks += 1
            logger.debug("Load-more click #%d: %d -> %d links", clicks, len(before), len(after))
            if len(after) <= len(before):
                break

        html = page.content()
        if debug_dump_html:
            debug_dump_html.parent.mkdir(parents=True, exist_ok=True)
            debug_dump_html.write_text(html, encoding="utf-8")
            logger.info("Saved rendered HTML to %s", debug_dump_html)

        browser.close()
        return html


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_date_near(text: str) -> tuple[Optional[date], str]:
    for pattern, formats in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(0).strip()
        cleaned = re.sub(r"\s+", " ", raw)
        for fmt in formats:
            try:
                return datetime.strptime(cleaned, fmt).date(), raw
            except ValueError:
                continue
    return None, ""


def _find_category_near(text: str) -> str:
    for cat in CATEGORY_CHOICES:
        if cat != "All News" and cat in text:
            return cat
    return ""


def parse_news_items(html: str, base_url: str, slug: str, ticker: str) -> list[NewsItem]:
    """Extract news items from rendered HTML.

    Strategy: find every <a> whose href matches the news-details URL shape,
    then look at the surrounding "card" (walking up a few ancestor levels)
    for a date and, optionally, a category label. This is deliberately
    link-pattern-based rather than CSS-class-based, since Q4 site themes
    change their markup/class names across clients and over time, but the
    news-details URL shape is stable.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not NEWS_LINK_RE.search(href):
            continue

        url = urljoin(base_url, href)
        if url in seen_urls:
            continue

        title = anchor.get_text(strip=True)
        if not title:
            # Some themes put the headline in a sibling/child element and
            # leave the <a> wrapping an icon/empty span; fall back to the
            # nearest ancestor's text.
            container = anchor.find_parent(["li", "article", "div"])
            title = container.get_text(" ", strip=True) if container else ""
        if not title:
            continue

        # Walk up a few ancestor levels looking for a date (and category) in
        # the surrounding card. Stop as soon as a container holds more than
        # one news-details link -- that means we've spanned past this item's
        # own card into the shared list wrapper, and its text would pull in
        # unrelated sibling items' dates/categories.
        publish_date, raw_date_text, category = None, "", ""
        node = anchor
        for _ in range(5):
            node = node.find_parent(["li", "article", "div", "section"])
            if node is None:
                break
            sibling_links = [a for a in node.find_all("a", href=True) if NEWS_LINK_RE.search(a["href"])]
            if len(sibling_links) > 1:
                break
            context_text = node.get_text(" ", strip=True)
            if publish_date is None:
                publish_date, raw_date_text = _parse_date_near(context_text)
            if not category:
                category = _find_category_near(context_text)
            if publish_date:
                break

        seen_urls.add(url)
        items.append(
            NewsItem(
                slug=slug,
                ticker=ticker,
                title=title,
                url=url,
                publish_date=publish_date,
                category=category,
                raw_date_text=raw_date_text,
            )
        )

    if not items:
        logger.warning(
            "No news items found. The page markup may not match the expected "
            "'/news/news-details/<year>/...' link pattern. Re-run with "
            "--debug-dump-html to inspect what was actually rendered."
        )
    else:
        missing_dates = sum(1 for i in items if i.publish_date is None)
        if missing_dates:
            logger.warning(
                "%d of %d items had no parseable date near their link.", missing_dates, len(items)
            )

    return items


def filter_items(
    items: Iterable[NewsItem],
    years: Optional[set[int]],
    since: Optional[date],
    until: Optional[date],
    limit: Optional[int],
) -> list[NewsItem]:
    result = []
    for item in items:
        if years is not None and (item.publish_date is None or item.publish_date.year not in years):
            continue
        if since is not None and (item.publish_date is None or item.publish_date < since):
            continue
        if until is not None and (item.publish_date is None or item.publish_date > until):
            continue
        result.append(item)
    result.sort(key=lambda i: (i.publish_date or date.min, i.title))
    if limit is not None:
        result = result[:limit]
    return result


# ---------------------------------------------------------------------------
# Output: CSV (primary_wire daily files) and JSON
# ---------------------------------------------------------------------------

def csv_path_for_date(data_dir: Path, d: date) -> Path:
    return data_dir / f"{d.year}" / f"{d.isoformat()}.csv"


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: tuple(r.get(k, "") for k in SORT_FIELDS))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def merge_into_daily_csvs(items: list[NewsItem], data_dir: Path, dry_run: bool) -> dict:
    """Group items by publish date and merge each group into its
    data/YYYY/YYYY-MM-DD.csv file, following the same read-drop-existing-
    append-sort-write approach as update_release.py. Items with no
    resolvable date are reported separately and never written, since the
    daily-file layout requires a date.

    Returns a summary dict for reporting.
    """
    by_date: dict[date, list[NewsItem]] = {}
    undated: list[NewsItem] = []
    for item in items:
        if item.publish_date is None:
            undated.append(item)
        else:
            by_date.setdefault(item.publish_date, []).append(item)

    summary = {"files_written": 0, "rows_added": 0, "rows_updated": 0, "undated": len(undated)}

    for d, day_items in sorted(by_date.items()):
        path = csv_path_for_date(data_dir, d)
        existing_rows = load_csv(path)
        existing_urls = {r["url"] for r in existing_rows}

        new_count, updated_count = 0, 0
        for item in day_items:
            row = item.to_csv_row()
            if row["url"] in existing_urls:
                updated_count += 1
            else:
                new_count += 1
            existing_rows = [r for r in existing_rows if r["url"] != row["url"]]
            existing_rows.append(row)

        summary["rows_added"] += new_count
        summary["rows_updated"] += updated_count

        if dry_run:
            logger.info(
                "[dry-run] Would write %s (%d new, %d updated, %d total rows)",
                path.relative_to(data_dir.parent) if data_dir.parent in path.parents else path,
                new_count,
                updated_count,
                len(existing_rows),
            )
            continue

        write_csv(path, existing_rows)
        summary["files_written"] += 1
        logger.info(
            "Wrote %s (%d new, %d updated, %d total rows)",
            path,
            new_count,
            updated_count,
            len(existing_rows),
        )

    if undated:
        logger.warning(
            "%d item(s) had no resolvable publish date and were NOT written to any "
            "daily CSV (the file layout is date-keyed). Use --debug-dump-html / "
            "--verbose to see why date parsing failed, or inspect them below:",
            len(undated),
        )
        for item in undated:
            logger.warning("  UNDATED: %s | %s", item.title, item.url)

    return summary


def write_json(items: list[NewsItem], path: Path, dry_run: bool) -> None:
    payload = [item.to_json_dict() for item in items]
    if dry_run:
        logger.info("[dry-run] Would write %d item(s) to %s", len(payload), path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info("Wrote %d item(s) to %s", len(payload), path)


def print_preview(items: list[NewsItem]) -> None:
    if not items:
        print("No items to preview.")
        return
    print(f"\n{len(items)} item(s):\n")
    for item in items:
        d = item.publish_datetime or "????-??-??"
        cat = f" [{item.category}]" if item.category else ""
        print(f"  {d}  {item.title}{cat}")
        print(f"             {item.url}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_year_list(args: argparse.Namespace) -> Optional[set[int]]:
    years: set[int] = set()
    if args.year:
        years.update(args.year)
    if args.start_year or args.end_year:
        start = args.start_year or args.end_year
        end = args.end_year or args.start_year
        if start > end:
            start, end = end, start
        years.update(range(start, end + 1))
    return years or None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    source = parser.add_argument_group("source")
    source.add_argument("--url", default=DEFAULT_URL, help=f"IR news page URL (default: {DEFAULT_URL})")
    source.add_argument("--slug", default=DEFAULT_SLUG, help=f"sources.yaml slug to tag rows with (default: {DEFAULT_SLUG})")
    source.add_argument("--ticker", default=DEFAULT_TICKER, help=f"Ticker to tag rows with (default: {DEFAULT_TICKER})")

    filt = parser.add_argument_group("filtering")
    filt.add_argument("--year", type=int, action="append", help="Year to scrape. Repeatable: --year 2024 --year 2025")
    filt.add_argument("--start-year", type=int, help="Start of an inclusive year range (with --end-year)")
    filt.add_argument("--end-year", type=int, help="End of an inclusive year range (with --start-year)")
    filt.add_argument("--since", type=lambda s: date.fromisoformat(s), help="Only keep items on/after this date (YYYY-MM-DD)")
    filt.add_argument("--until", type=lambda s: date.fromisoformat(s), help="Only keep items on/before this date (YYYY-MM-DD)")
    filt.add_argument("--category", default="All News", choices=CATEGORY_CHOICES, help="Category filter to apply on the page (default: All News)")
    filt.add_argument("--limit", type=int, help="Keep at most this many items (after sorting by date)")

    out = parser.add_argument_group("output")
    out.add_argument("--format", choices=["csv", "json", "both"], default="csv", help="csv = merge into primary_wire's data/YYYY/YYYY-MM-DD.csv files (default); json = single combined file")
    out.add_argument("--data-dir", type=Path, default=DATA_DIR, help=f"Root of the data/ tree for --format csv (default: {DATA_DIR})")
    out.add_argument("--output", type=Path, default=REPO_ROOT / "costco_news.json", help="Output path for --format json")
    out.add_argument("--dry-run", action="store_true", help="Scrape and show what would be written, but write nothing")

    browser = parser.add_argument_group("browser")
    browser.add_argument("--show-browser", dest="headless", action="store_false", default=True, help="Show the browser window instead of running headless (useful for debugging)")
    browser.add_argument("--browser-channel", default="chrome", help="Playwright browser channel, e.g. chrome, chromium, msedge (default: chrome, reusing the system install)")
    browser.add_argument("--timeout", type=int, default=30_000, help="Timeout in milliseconds for page navigation and element interactions (default: 30000)")
    browser.add_argument("--change-timeout", type=int, default=8_000, help="Timeout in milliseconds to wait for the release list to actually change after a year/category switch or load-more click, before assuming there's nothing new (default: 8000 -- kept short since these are fast AJAX updates, not full page loads)")
    browser.add_argument("--polite-delay", type=float, default=2.0, help="Seconds to pause after each in-page interaction (year/category change, load-more click) before reading the DOM, to be courteous to the server (default: 2.0)")
    browser.add_argument("--max-load-more", type=int, default=20, help="Safety cap on how many times to click 'load more'/pagination (default: 20)")
    browser.add_argument("--debug-dump-html", type=Path, help="Save the final rendered HTML to this path for inspection")

    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v for INFO, -vv for DEBUG (default: WARNING)")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    level = {0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG)
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    if args.start_year and args.end_year is None:
        args.end_year = args.start_year
    if args.end_year and args.start_year is None:
        args.start_year = args.end_year
    years = parse_year_list(args)

    # Scraping a multi-year span requires re-selecting the year control once
    # per year (the page only shows one year's worth of releases at a time),
    # so loop over each requested year and accumulate results. If no year
    # was requested, scrape whatever the page shows by default.
    years_to_visit: list[Optional[int]] = sorted(years) if years else [None]

    all_items: list[NewsItem] = []
    for y in years_to_visit:
        debug_path = args.debug_dump_html
        if debug_path and len(years_to_visit) > 1:
            debug_path = debug_path.with_name(f"{debug_path.stem}_{y}{debug_path.suffix}")
        html = render_news_page(
            url=args.url,
            year=y,
            category=args.category,
            headless=args.headless,
            browser_channel=args.browser_channel,
            timeout_ms=args.timeout,
            change_timeout_ms=args.change_timeout,
            polite_delay=args.polite_delay,
            max_load_more=args.max_load_more,
            debug_dump_html=debug_path,
        )
        items = parse_news_items(html, base_url=args.url, slug=args.slug, ticker=args.ticker)
        all_items.extend(items)
        if y is not None and len(years_to_visit) > 1:
            time.sleep(args.polite_delay)

    filtered = filter_items(all_items, years=years, since=args.since, until=args.until, limit=args.limit)

    if not filtered:
        logger.warning("No items matched the requested filters.")

    print_preview(filtered)

    if args.format in ("csv", "both"):
        summary = merge_into_daily_csvs(filtered, args.data_dir, args.dry_run)
        action = "Would write" if args.dry_run else "Wrote"
        print(
            f"{action} {summary['rows_added']} new + {summary['rows_updated']} updated row(s) "
            f"across {summary['files_written'] if not args.dry_run else len(set(i.publish_date for i in filtered if i.publish_date))} "
            f"daily CSV file(s) under {args.data_dir}"
            + (f" ({summary['undated']} undated item(s) skipped)" if summary["undated"] else "")
        )

    if args.format in ("json", "both"):
        write_json(filtered, args.output, args.dry_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())