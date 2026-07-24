#!/usr/bin/env python3
"""
scrape_investorroom.py

Scrape press-release listings from any IR site powered by the InvestorRoom
platform (sold by Notified, formerly Intrado/West) and merge them into
primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

InvestorRoom is a server-side-rendered IR platform used by a large number of
S&P 500 companies. Unlike Q4 Inc. sites (which require a headless browser),
these pages return full HTML to a plain HTTP request, so no Playwright is needed.

Platform fingerprints
---------------------
You can identify an InvestorRoom site by any of:

  * The listing URL ends with /news-releases (no .aspx extension)
  * Detail pages use ?item=NNNNN  OR  a date-prefixed slug
    e.g. /2025-10-29-chipotle-announces-q3-results
  * Static assets / PDFs served from filecache.investorroom.com
  * Page footer or source contains "investorroom" or "Notified"

URL structure
-------------
Listing page (paginated by offset):
  {ir_base}/news-releases
  {ir_base}/news-releases?l=100          (100 items per page)
  {ir_base}/news-releases?l=100&o=100    (next page)

  Parameters:
    ?l=<limit>   Number of listings per page (server default is 5; use 100)
    ?o=<offset>  Skip this many items (NOT a page number)

  Note: ?p=2 is NOT supported by InvestorRoom sites.

Press release detail pages come in three styles:
  Style A (legacy):    {ir_base}/news-releases?item=122457
  Style B (modern):    {ir_base}/2025-10-29-chipotle-announces-q3-results
  Style C (bare slug): {ir_base}/next-generation-ibm-flashsystem-portfolio
                       (observed on IBM's newsroom in 2026 -- no date prefix,
                       no ?item=; confirmed via a headline-length title plus
                       a real date on the listing page, since the URL alone
                       looks like an ordinary nav link -- see classify_link())

All three styles are handled. Date extraction:

  1. Listing-page parse (zero extra requests): InvestorRoom listing pages
     include the date near each link in the card HTML. This listing-page date
     is authoritative when present -- Style B/C URLs can also embed a date,
     but IBM's newsroom has been observed with a Style B slug date that
     doesn't match the article's real publish date, so the URL-embedded date
     is used only as a fallback (see resolve_publish_date()). The
     listing-page date itself is only trusted from a text node that is
     *entirely* a date and nothing else (see extract_date_near_link()) --
     press-release cards routinely mention other, unrelated dates in the
     headline or summary snippet (an upcoming earnings call, a future event),
     and naively taking "the first date-like text in the card" picks up
     whichever of those happens to be positioned first, which is often wrong.

  2. Detail-page fallback (opt-in via --fetch-detail-pages): for items where
     no date was found on the listing page (typically Style A), fetch the
     detail page and extract the date from the article header.

Some InvestorRoom sites (e.g. Centene) also render a publish time next to
the date on the listing page, as its own standalone text node, e.g.:

  January 13, 2026
  4:30 PM EST

When present, that raw time-with-timezone substring (e.g. "4:30 PM EST") is
captured verbatim into the publish_time CSV column -- see parse_time() in
utils/scrape_utils.py and extract_date_near_link() below. It is NOT
converted to any other timezone or format. Sites that don't publish a time
(or where the time isn't found on the listing page) leave this column
blank; there is currently no detail-page fallback for the time the way
there is for the date.

Usage
-----
  # Default: scrape Chipotle, dry-run (no files written)
  python src/scrape_investorroom.py --dry-run

  # Write real data for Chipotle
  python src/scrape_investorroom.py

  # Scrape any InvestorRoom site by slug or ticker
  python src/scrape_investorroom.py --slug chipotle --dry-run
  python src/scrape_investorroom.py --ticker CMG --dry-run

  # Scrape by URL directly
  python src/scrape_investorroom.py --url https://ir.chipotle.com/news-releases --dry-run

  # Override the news-releases listing path for an InvestorRoom site that
  # doesn't use the default (rare -- most InvestorRoom sites use
  # news-releases). Normally set once in sources.yaml's news_releases_path
  # field instead of passing this every time.
  python src/scrape_investorroom.py --slug SLUG --news-releases-path press-releases --dry-run

  # Restrict to a year or range
  python src/scrape_investorroom.py --year 2025 --dry-run
  python src/scrape_investorroom.py --start-year 2023 --end-year 2025 --dry-run

  # Date range
  python src/scrape_investorroom.py --since 2024-01-01 --until 2024-12-31 --dry-run

  # Control items per listing page (default 100; server default without ?l= is 5)
  python src/scrape_investorroom.py --page-limit 50 --dry-run

  # Fetch detail pages to resolve missing dates
  python src/scrape_investorroom.py --fetch-detail-pages --dry-run

  # Output as JSON
  python src/scrape_investorroom.py --format json --output out.json --dry-run

  # Save raw HTML for debugging
  python src/scrape_investorroom.py --debug-dump-html /tmp/chipotle.html --dry-run

Requires
--------
  pip install requests beautifulsoup4 lxml ruamel.yaml

Run at most once per day. Requests are spaced by --polite-delay (default 15 s).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urljoin, urlsplit

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Install with: pip install requests")

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
    extract_date_from_detail_html,
    fetch_missing_dates_via_http,
    finalize_and_output,
    parse_date,
    parse_time,
    parse_year_args,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DEFAULT_SLUG = "chipotle"
DEFAULT_TICKER = "CMG"
DEFAULT_BASE_URL = "https://ir.chipotle.com"

DEFAULT_NEWS_RELEASES_PATH = "news-releases"
# Actual path used for a given source resolves as (highest wins):
#   --news-releases-path CLI flag
#   > sources.yaml "news_releases_path" field for the matched source
#   > DEFAULT_NEWS_RELEASES_PATH
# See resolve_source(). All InvestorRoom sites currently in sources.yaml use
# the default; this exists for when a future one doesn't.

DEFAULT_PAGE_LIMIT = 100  # ?l=100 -- 100 items per page vs server default of 5
MAX_PAGES = 50            # safety cap on pagination loops

# Regex patterns to identify InvestorRoom detail-page URLs.
DETAIL_URL_LEGACY_RE = re.compile(r"[?&]item=\d+", re.IGNORECASE)
# Excludes fragment URLs (e.g. /2026-01-12-TITLE#assets_...) -- those are photo
# gallery anchors on the same page, not press-release detail pages.
DETAIL_URL_MODERN_RE = re.compile(r"/\d{4}-\d{2}-\d{2}-[^/#]+/?$", re.IGNORECASE)

# Style C: some InvestorRoom sites (observed on IBM's newsroom in 2026)
# publish press releases at a bare topic slug with neither a ?item= param
# nor a date-prefixed slug, e.g.:
#   https://newsroom.ibm.com/next-generation-ibm-flashsystem-portfolio
# Such a URL is indistinguishable from an ordinary nav/footer link (About,
# Subscribe, Media contact...) by shape alone, so is_bare_slug_url() only
# narrows to same-host/query-free/single-segment links; parse_listing_page()
# narrows further by requiring a real nearby publish date and a
# headline-length title before accepting one as a press release.
MIN_HEADLINE_TITLE_LEN = 20  # chars; real press-release titles clear this easily, nav labels don't

# Known non-article single-segment paths to rule out up front (belt-and-braces
# alongside the title-length + nearby-date checks above). Extend as needed for
# other InvestorRoom sites; harmless to leave in for sites that don't have them.
BARE_SLUG_EXCLUDE_PATHS = frozenset({
    "index.php", "subscribe", "contacts", "media-center", "b-roll",
    "global-news-room", "executive-bios", "about-ibm", "awards",
    "campaign", "announcements", "news-releases",
})
BARE_SLUG_EXCLUDE_PREFIXES = ("latest-news-", "latest-new-", "press-releases-")

logger = logging.getLogger("scrape_investorroom")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem(_BaseNewsItem):
    """InvestorRoom press-release item.

    Inherits slug, ticker, title, url, publish_date, raw_date_text, and
    publish_date_str from scrape_utils.NewsItem.  No extra fields needed for
    this platform; subclassing keeps isinstance() checks consistent and leaves
    room for future additions without touching shared code.
    """


# ---------------------------------------------------------------------------
# Date helpers (platform-specific)
# ---------------------------------------------------------------------------

def date_from_url(url: str) -> Optional[date]:
    """Extract a publish date from a modern InvestorRoom URL like /2025-10-29-title."""
    m = re.search(r"/(\d{4}-\d{2}-\d{2})-", url)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_SESSION: Optional[requests.Session] = None


def get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
    return _SESSION


def fetch_html(url: str, timeout: int = 30) -> str:
    """Fetch a URL and return its HTML. Raises on HTTP errors."""
    resp = get_session().get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def is_detail_url(href: str) -> bool:
    """Return True if ``href`` matches a *known* InvestorRoom detail-URL style
    (legacy ?item=NNN, or a modern date-prefixed slug)."""
    return bool(DETAIL_URL_LEGACY_RE.search(href) or DETAIL_URL_MODERN_RE.search(href))


def is_bare_slug_url(href: str, base_url: str) -> bool:
    """Return True if ``href`` *might* be a Style C bare-slug detail page.

    Necessary condition only, not sufficient: same host as ``base_url``, no
    query string or fragment, exactly one path segment, and not one of the
    known non-article paths. The caller must still confirm it's a real
    press release (see MIN_HEADLINE_TITLE_LEN / nearby-date check in
    parse_listing_page) since this alone can't distinguish a headline slug
    from a stray one-segment nav link.
    """
    full_url = urljoin(base_url, href)
    parsed = urlsplit(full_url)
    if parsed.netloc != urlsplit(base_url).netloc:
        return False
    if parsed.query or parsed.fragment:
        return False

    path = parsed.path.strip("/").lower()
    if not path or "/" in path:
        return False
    if path in BARE_SLUG_EXCLUDE_PATHS:
        return False
    if path.startswith(BARE_SLUG_EXCLUDE_PREFIXES):
        return False
    return True


def classify_link(href: str, base_url: str) -> Optional[str]:
    """Classify ``href`` as a detail-link candidate, or None to skip it outright.

    Returns "known" for the two well-established InvestorRoom detail-URL
    styles (legacy ?item=, modern date-prefixed slug), "bare-slug" for a
    same-host single-segment URL that needs further confirmation (title
    length + nearby date) before being accepted, or None for anything else
    (nav links, external links, paginated/query URLs, etc.).
    """
    if is_detail_url(href):
        return "known"
    if is_bare_slug_url(href, base_url):
        return "bare-slug"
    return None


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

def listing_page_url(
    base_url: str,
    offset: int = 0,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
    extra_params: Optional[dict[str, str]] = None,
) -> str:
    """Build a paginated listing URL using InvestorRoom's ?l= / ?o= parameters.

    ?l=<limit>   items per page (server default is 5 when omitted; always pass explicitly)
    ?o=<offset>  skip this many items (0-based; omit on the first page)

    news_releases_path defaults to "news-releases"; callers resolve the
    right value via resolve_source() / sources.yaml before calling this.

    extra_params carries any site-specific query string the user passed on
    --url (e.g. https://.../news-releases?category=788), threaded through
    from resolve_source()'s extra_query_params -- see resolve_source_identity()
    in sources_utils.py. Without this, --url's query string was silently
    dropped: resolve_source() strips --url down to scheme+host (so
    news_releases_path can be joined onto the site root instead of whatever
    path the user happened to pass), and this function then built ?l=/?o=
    from scratch, discarding anything else that had been on --url. Placed
    first in the dict so ?l=/?o=/?year= (set below/by year_filter_url) always
    win if a name collides, and so it renders first in the URL.
    """
    base = join_url_path(base_url, news_releases_path)
    params: dict[str, object] = {}
    if extra_params:
        params.update(extra_params)
    params["l"] = page_limit
    if offset > 0:
        params["o"] = offset
    return base + "?" + urlencode(params)


def year_filter_url(
    base_url: str,
    year: int,
    offset: int = 0,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
    extra_params: Optional[dict[str, str]] = None,
) -> str:
    """Build a year-filtered listing URL. See listing_page_url() for news_releases_path
    and extra_params."""
    base = join_url_path(base_url, news_releases_path)
    params: dict[str, object] = {}
    if extra_params:
        params.update(extra_params)
    params["year"] = year
    params["l"] = page_limit
    if offset > 0:
        params["o"] = offset
    return base + "?" + urlencode(params)


# ---------------------------------------------------------------------------
# Listing-page parsing
# ---------------------------------------------------------------------------

def is_bare_date_text(text: str, raw_match: str) -> bool:
    """True if ``text`` is (almost) entirely ``raw_match`` and not a longer
    sentence that merely happens to contain a date somewhere in it."""
    remainder = text.replace(raw_match, "", 1)
    return remainder.strip(" \t\r\n-\u2013\u2014|\u00b7\u2022.,:") == ""


def _normalize_href_for_dedup(href: str, base_url: str) -> str:
    """Absolute-ize ``href`` and strip its query/fragment/trailing slash.

    Used to tell whether two links on the page point at "the same article"
    (e.g. a headline link and a ?asPDF download link for that same release)
    versus two genuinely different items -- see the sibling-link check in
    extract_date_near_link().
    """
    full_url = urljoin(base_url, href)
    parsed = urlsplit(full_url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


def _other_candidate_hrefs(parent, anchor, base_url: str) -> set[str]:
    """Normalized hrefs of any *other* candidate link inside ``parent``.

    "Candidate" means classify_link() considers it a possible detail page
    (known style or bare-slug) -- plain nav/footer links that aren't even
    candidates don't count. The anchor's own href (and any other link that
    normalizes to the same target, e.g. a ?asPDF variant of the same
    article) is excluded, since that's still "one item", not a sibling.
    """
    own_normalized = _normalize_href_for_dedup(anchor["href"].strip(), base_url)
    others: set[str] = set()
    for other in parent.find_all("a", href=True):
        if other is anchor:
            continue
        href = other["href"].strip()
        if classify_link(href, base_url) is None:
            continue
        normalized = _normalize_href_for_dedup(href, base_url)
        if normalized != own_normalized:
            others.add(normalized)
    return others


def extract_date_near_link(anchor, base_url: str) -> tuple[Optional[date], str, str]:
    """Walk up to 5 ancestor elements of ``anchor`` looking for a standalone
    date label (and, if present, a standalone time label) near the link.

    Press-release cards routinely mention OTHER dates besides the actual
    publish date -- a headline naming a future event (GPC: "...Results on
    February 17, 2026", published Jan 27) or a truncated summary snippet
    doing the same (IBM: "...discuss its fourth-quarter 2025 financial
    results on Wednesday, January 28, 2026...", published Jan 14). Scanning
    the card's merged text for "the first date-like substring" -- the
    previous approach -- just returns whichever of these happens to sit
    first in reading order, which is often wrong and, worse, wrong in a new
    way each time a differently-worded card is encountered.

    Instead of merging text, this walks each individual text node under the
    ancestor and only accepts one whose ENTIRE (stripped) content is a bare
    date and nothing else (via is_bare_date_text()) -- which is how these
    platforms actually render the publish-date label, as opposed to a full
    sentence that merely contains a date. Text inside the anchor itself is
    skipped outright, since a headline's own wording is never the label.

    Stops climbing as soon as the ancestor contains another *distinct*
    candidate link (see _other_candidate_hrefs()) -- that means we've
    climbed out of this single item's card and into a shared wrapper (a nav
    menu, a sidebar "quick links" list, the whole page's item list...) that
    also holds other, unrelated items. Continuing to climb from there would
    happily attach some other item's date (often just whichever item's date
    happens to appear first in the wrapper) to this one -- observed on
    Danaher's site, where a handful of unrelated sidebar links (Events &
    Presentations, Annual Report & Proxy, etc.) all ended up dated with the
    top press release's date once the walk reached the shared layout
    container several levels up. A same-article duplicate link (e.g. a
    headline link plus a separate ?asPDF download link for that same
    release) does not count as "another" item and does not stop the climb.

    Some sites (e.g. Centene) render a publish time as its own separate bare
    text node right alongside the date (see module docstring), rather than
    appended to the date text itself. So at each ancestor level, every text
    node is also checked for a standalone "clock time + timezone" string
    (again via is_bare_date_text(), which despite its name is really just a
    "this whole node is the match and nothing else" check, so it works for
    time text just as well as date text). Returns the date found at the
    ancestor level where a date was first found, along with whatever bare
    time text (possibly "") was seen at that SAME level -- consistent with
    how these platforms actually pair a date node with an adjacent time
    node under one shared card wrapper.
    """
    node = anchor
    for _ in range(5):
        parent = node.parent
        if parent is None:
            break
        if _other_candidate_hrefs(parent, anchor, base_url):
            break
        found_date: Optional[date] = None
        found_raw = ""
        found_time = ""
        for text_node in parent.find_all(string=True):
            if any(p is anchor for p in text_node.parents):
                continue
            candidate = text_node.strip()
            if not candidate:
                continue
            if found_date is None:
                d, raw = parse_date(candidate)
                if d and is_bare_date_text(candidate, raw):
                    found_date, found_raw = d, raw
                    continue  # a date node is never also a time node
            if not found_time:
                t = parse_time(candidate)
                if t and is_bare_date_text(candidate, t):
                    found_time = t
        if found_date is not None:
            return found_date, found_raw, found_time
        node = parent
    return None, "", ""


def find_next_page_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """Find the 'Next' pagination link in a parsed listing page.

    Reading the href directly is more reliable than constructing it ourselves
    because the page size (?l=) may vary by site or theme.
    """
    for candidate in soup.find_all("a", href=True):
        text = candidate.get_text(strip=True).lower()
        aria = (candidate.get("aria-label") or "").lower()
        rel = " ".join(candidate.get("rel") or []).lower()
        is_next = (
            text in ("next", "›", "»", "next »", "next›")
            or "next" in aria
            or "next" in rel
        )
        if not is_next:
            continue
        href = candidate["href"].strip()
        if href and href not in ("#", "javascript:void(0)", "javascript:;"):
            url = urljoin(base_url, href)
            logger.debug("Next page link: %s", url)
            return url
    return None


def extract_link_title(anchor) -> str:
    """Return the display title for a listing-page anchor, or "" if it has none.

    Falls back to a nested <span> because some InvestorRoom themes wrap the
    visible title in one (the anchor's direct text is otherwise empty, e.g.
    when the link is really an image tile).
    """
    title = anchor.get_text(separator=" ", strip=True)
    if title:
        return title
    span = anchor.find("span")
    return span.get_text(strip=True) if span else ""


def is_confirmed_bare_slug_item(title: str, card_date: Optional[date]) -> bool:
    """Second-stage check for a "bare-slug" candidate (see classify_link()).

    A same-host single-segment URL is only accepted as a press release once
    it also has a headline-length title and a real date sitting next to it
    on the listing page -- both true for genuine articles, both false for
    nav/footer links like "Subscribe" or "Media contact".
    """
    return len(title) >= MIN_HEADLINE_TITLE_LEN and card_date is not None


def resolve_publish_date(
    card_date: Optional[date],
    card_raw_text: str,
    url_date: Optional[date],
    url_for_logging: str,
) -> tuple[Optional[date], str]:
    """Reconcile the two possible date sources for a listing-page item.

    The listing page's own displayed date (``card_date``) takes priority
    over one parsed out of the URL slug (``url_date``): IBM's newsroom has
    been observed publishing a URL whose date-prefixed slug doesn't match
    the article's real publish date (e.g. a slug dated 2025-04-21 for an
    article actually published 2026-04-21), so the URL can't be trusted as
    authoritative. It's kept only as a fallback for when the listing page
    doesn't expose a date at all.
    """
    if card_date is not None:
        if url_date is not None and url_date != card_date:
            logger.warning(
                "URL slug date (%s) disagrees with listing-page date (%s) "
                "for %s -- using the listing-page date.",
                url_date, card_date, url_for_logging,
            )
        return card_date, card_raw_text
    if url_date is not None:
        return url_date, url_date.isoformat()
    return None, ""


def parse_listing_page(
    html: str, base_url: str, slug: str, ticker: str
) -> tuple[list[NewsItem], Optional[str]]:
    """Parse one listing page.

    Returns (items, next_page_url).
    next_page_url is the absolute URL for the next page, or None on the last page.
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href: str = anchor["href"].strip()

        link_kind = classify_link(href, base_url)
        if link_kind is None:
            continue

        full_url = urljoin(base_url, href)
        norm_url = full_url.rstrip("/")
        if norm_url in seen_urls:
            continue

        title = extract_link_title(anchor)
        if not title:
            logger.debug("Skipping link with no title text: %s", full_url)
            continue

        # Date (and, if present, time) in the surrounding card HTML --
        # computed for every candidate, since a "bare-slug" link (see
        # classify_link()) needs the date both to confirm it's a real
        # article and to know its publish date.
        card_date, card_raw_text, card_time = extract_date_near_link(anchor, base_url)

        if link_kind == "bare-slug" and not is_confirmed_bare_slug_item(title, card_date):
            logger.debug("Skipping unconfirmed bare-slug link: %s", full_url)
            continue

        seen_urls.add(norm_url)

        url_date = date_from_url(href)
        publish_date, raw_date_text = resolve_publish_date(card_date, card_raw_text, url_date, full_url)
        # The time is only trustworthy alongside the card's own date (there's
        # no time embedded in the URL to fall back on) -- e.g. if the card
        # date disagreed with the URL date and the URL date won, or if there
        # was no card date at all, discard whatever card_time was found so
        # it isn't misattributed to a date it wasn't actually paired with.
        publish_time = card_time if publish_date == card_date and card_date is not None else ""

        items.append(NewsItem(
            slug=slug,
            ticker=ticker,
            title=title,
            url=full_url,
            publish_date=publish_date,
            raw_date_text=raw_date_text,
            publish_time=publish_time,
        ))

    next_url = find_next_page_url(soup, base_url)
    return items, next_url


# ---------------------------------------------------------------------------
# Detail-page date fallback
# ---------------------------------------------------------------------------

def fetch_date_from_detail_page(url: str, timeout: int = 30) -> tuple[Optional[date], str]:
    """Fetch a detail page and extract its publish date.

    Parsing heuristics live in scrape_utils.extract_date_from_detail_html(),
    shared with scrape_notified.py; this function owns only the fetch.
    """
    try:
        html = fetch_html(url, timeout=timeout)
    except Exception as exc:
        logger.warning("Failed to fetch detail page %s: %s", url, exc)
        return None, ""
    return extract_date_from_detail_html(html)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_one_pass(
    base_url: str,
    slug: str,
    ticker: str,
    start_url: str,
    polite_delay: float,
    timeout: int,
    debug_dump_html: Optional[Path] = None,
) -> list[NewsItem]:
    """Fetch all listing pages starting from ``start_url``, following Next links.

    Returns a deduplicated list of NewsItems.
    """
    next_url: Optional[str] = start_url
    all_items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for page_num in range(1, MAX_PAGES + 1):
        url = next_url
        logger.info("Fetching listing page %d: %s", page_num, url)

        try:
            html = fetch_html(url, timeout=timeout)
        except Exception as exc:
            logger.error("Failed to fetch listing page %s: %s", url, exc)
            break

        if debug_dump_html and page_num == 1:
            debug_dump_html.write_text(html, encoding="utf-8")
            logger.info("Saved HTML to %s", debug_dump_html)

        page_items, next_url = parse_listing_page(html, base_url=base_url, slug=slug, ticker=ticker)

        new_items = [
            item for item in page_items
            if item.url.rstrip("/") not in seen_urls
        ]
        for item in new_items:
            seen_urls.add(item.url.rstrip("/"))
        all_items.extend(new_items)

        logger.info(
            "Page %d: %d item(s) found, %d new%s",
            page_num, len(page_items), len(new_items),
            f"; next → {next_url}" if next_url else " [last page]",
        )

        if not new_items and page_items:
            logger.warning(
                "Page %d: all %d item(s) already seen -- stopping to avoid loop.",
                page_num, len(page_items),
            )
            break

        if not next_url:
            break

        time.sleep(polite_delay)

    return all_items


def scrape(
    base_url: str,
    slug: str,
    ticker: str,
    years: Optional[set[int]],
    polite_delay: float,
    timeout: int,
    page_limit: int,
    debug_dump_html: Optional[Path],
    news_releases_path: str = DEFAULT_NEWS_RELEASES_PATH,
    extra_params: Optional[dict[str, str]] = None,
) -> list[NewsItem]:
    """Scrape all years (or the default all-years view).

    When years are specified, one pass per year is made using the ?year= filter.
    Results are globally deduplicated before returning.

    extra_params is forwarded to listing_page_url()/year_filter_url() -- see
    those functions' docstrings; it's how a site-specific query filter
    passed via --url (e.g. ?category=788) survives into the first listing
    request instead of being silently dropped by resolve_source().
    """
    years_to_visit: list[Optional[int]] = sorted(years) if years else [None]
    all_items: list[NewsItem] = []

    for i, year in enumerate(years_to_visit):
        if i > 0:
            time.sleep(polite_delay)

        if year is not None:
            start_url = year_filter_url(
                base_url, year, page_limit=page_limit, news_releases_path=news_releases_path,
                extra_params=extra_params,
            )
        else:
            start_url = listing_page_url(
                base_url, page_limit=page_limit, news_releases_path=news_releases_path,
                extra_params=extra_params,
            )

        dump_path = debug_dump_html
        if dump_path and len(years_to_visit) > 1 and year is not None:
            dump_path = dump_path.with_name(f"{dump_path.stem}_{year}{dump_path.suffix}")

        items = scrape_one_pass(
            base_url=base_url,
            slug=slug,
            ticker=ticker,
            start_url=start_url,
            polite_delay=polite_delay,
            timeout=timeout,
            debug_dump_html=dump_path,
        )
        all_items.extend(items)

    # Global dedup across year passes.
    return dedupe_by_url(all_items)


# ---------------------------------------------------------------------------
# Output: daily CSVs
# ---------------------------------------------------------------------------

# CSV/JSON writing and the "Wrote N new + M updated ..." summary line are
# handled by scrape_utils.finalize_and_output(), shared with scrape_q4_ir.py
# and scrape_notified.py. Called directly from main() below.


# ---------------------------------------------------------------------------
# Source resolution (sources.yaml integration)
# ---------------------------------------------------------------------------

def resolve_source(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
    news_releases_path: Optional[str] = None,
) -> tuple[str, str, str, str, dict[str, str]]:
    """Resolve (base_url, slug, ticker, news_releases_path, extra_query_params)
    from CLI args and sources.yaml.

    base_url is the IR site root (e.g. https://ir.chipotle.com), NOT the
    news-releases listing URL.  Callers append news_releases_path themselves
    via listing_page_url() / year_filter_url().

    news_releases_path precedence (highest wins):
      1. the news_releases_path argument (i.e. --news-releases-path on the CLI)
      2. the "news_releases_path" field on the matched sources.yaml record
      3. DEFAULT_NEWS_RELEASES_PATH ("news-releases")

    extra_query_params holds any query string that was present on --url
    (e.g. ?category=788) before resolve_source_identity() stripped --url
    down to its site root -- see that function's docstring. Pass it to
    listing_page_url()/year_filter_url() (via scrape()'s extra_params
    argument) so it isn't silently lost.
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

    return url, slug, ticker, news_releases_path, extra_query_params


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Shared: --url/--slug/--ticker, year/date filters, --format/--output/--dry-run
    add_common_args(parser)

    source = parser.add_argument_group("source")
    source.add_argument(
        "--news-releases-path", default=None, metavar="PATH",
        help=(
            "Listing path appended to the IR site root, e.g. press-releases "
            "(default: news-releases). Overrides sources.yaml's "
            "news_releases_path field for this run; most sites don't need this."
        ),
    )

    detail = parser.add_argument_group("detail-page fetch")
    detail.add_argument(
        "--fetch-detail-pages", action="store_true",
        help=(
            "For items with no date found on the listing page, fetch each detail "
            "page to extract the date. Useful for legacy ?item=NNN URLs."
        ),
    )

    # Override --data-dir default (matches scrape_q4_ir.py, which historically
    # exposed this explicitly; the other scrapers previously hardcoded DATA_DIR).
    out = parser.add_argument_group("output")
    out.add_argument(
        "--data-dir", type=Path, default=DATA_DIR,
        help=f"Root of the data/ tree for --format csv (default: {DATA_DIR})",
    )

    # Shared: --polite-delay/--timeout/--debug-dump-html/--verbose
    network = add_network_and_debug_args(parser, default_polite_delay=15.0)
    network.add_argument(
        "--page-limit", type=int, default=DEFAULT_PAGE_LIMIT, metavar="N",
        dest="page_limit",
        help=(
            f"Items per listing page via ?l= (default: {DEFAULT_PAGE_LIMIT}). "
            "The server default without ?l= is 5, which causes many more requests."
        ),
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)

    base_url, slug, ticker, news_releases_path, extra_query_params = resolve_source(
        args.url, args.slug, args.ticker, args.news_releases_path
    )
    logger.info("Scraping %s (%s) from %s", slug, ticker, join_url_path(base_url, news_releases_path))

    years = parse_year_args(args)

    all_items = scrape(
        base_url=base_url,
        slug=slug,
        ticker=ticker,
        years=years,
        polite_delay=args.polite_delay,
        timeout=args.timeout,
        page_limit=args.page_limit,
        debug_dump_html=args.debug_dump_html,
        news_releases_path=news_releases_path,
        extra_params=extra_query_params,
    )
    logger.info("Scraped %d item(s) total (before filtering).", len(all_items))

    if args.fetch_detail_pages:
        fetch_missing_dates_via_http(
            all_items, fetch_date_from_detail_page, args.polite_delay, args.timeout
        )

    # Filters, always previews, and writes CSV/JSON per --format; see
    # finalize_and_output()'s docstring for the three behaviors this
    # standardizes across scrape_investorroom.py/scrape_notified.py/
    # scrape_q4_ir.py (preview-always, --format both, --output default path).
    finalize_and_output(
        all_items,
        years=years, since=args.since, until=args.until, limit=None,
        format=args.format, output=args.output, dry_run=args.dry_run,
        data_dir=args.data_dir,
        default_json_path=REPO_ROOT / "investorroom_news.json",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())