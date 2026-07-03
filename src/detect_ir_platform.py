#!/usr/bin/env python3
"""
detect_ir_platform.py

Detect which IR (investor relations) platform powers each company's IR site
by fetching the page and inspecting its HTML for documented fingerprints.
No hardcoded hostname lists — every classification is evidence-based.

Supported platforms (and their fingerprints, as documented by each scraper)
---------------------------------------------------------------------------

q4  (scrape_q4_ir.py)
  * Links with href matching /news/news-details/<year>/<slug>[/default.aspx]
  * Static assets or links containing /news/default.aspx

investorroom  (scrape_investorroom.py)
  * Static assets / PDFs served from filecache.investorroom.com
  * Page source contains the string "investorroom"
  * Links matching /news-releases?item=NNNNN  OR  /<YYYY-MM-DD>-<slug>

notified  (scrape_notified.py)
  * <meta name="Generator" content="Drupal 10 ..."> in the page <head>
  * Links matching /news-releases/news-release-details/<slug>

Priority when multiple signals fire: notified > investorroom > q4
(Notified and InvestorRoom share some URL shapes; Drupal meta tag is definitive.)

unknown
  * No signal matched.

Usage
-----
  # Single lookup
  python src/detect_ir_platform.py --slug costco
  python src/detect_ir_platform.py --ticker CMG
  python src/detect_ir_platform.py --url https://investors.abbvie.com/

  # Scan everything in sources.yaml (parallel fetches)
  python src/detect_ir_platform.py --all

  # Custom sources file
  python src/detect_ir_platform.py --all --sources /path/to/sources.yaml

  # Redirect-friendly: output is plain fixed-width text, no ANSI
  python src/detect_ir_platform.py --all > platforms.tsv

  # Control concurrency and per-request timeout
  python src/detect_ir_platform.py --all --workers 8 --timeout 15

Output
------
Fixed-width table: slug | ticker | platform | ir_url
Column widths auto-size to content. Header + separator on the first two lines.

Requires
--------
  pip install curl_cffi beautifulsoup4 lxml pandas ruamel.yaml
  (requests is used as a fallback if curl_cffi is not available, but curl_cffi
  is required for sites with TLS fingerprinting such as AbbVie/Notified.)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import pandas as pd
from bs4 import BeautifulSoup

# curl_cffi impersonates Chrome's TLS fingerprint (JA3/JA4), which is required
# for IR sites that enforce TLS fingerprinting (Notified/Drupal sites like
# AbbVie silently drop or timeout connections from the standard Python stack).
# scrape_notified.py documents this explicitly and mandates curl_cffi.
# We use it for all fetches here — it handles all three platform types fine.
try:
    from curl_cffi import requests
    _HTTP_BACKEND = "curl_cffi"
except ImportError:
    import requests  # type: ignore[no-redef]
    _HTTP_BACKEND = "requests"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("detect_ir_platform")

# ---------------------------------------------------------------------------
# Repository layout
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES_YAML = REPO_ROOT / "sources" / "sources.yaml"

# ---------------------------------------------------------------------------
# Fingerprint regexes — taken verbatim from each scraper's source
# ---------------------------------------------------------------------------

# Q4 (scrape_q4_ir.py, line 127):
#   NEWS_LINK_RE = re.compile(r"/news/news-details/\d{4}/[^/]+/?(?:default\.aspx)?", re.IGNORECASE)
Q4_NEWS_LINK_RE = re.compile(
    r"/news/news-details/\d{4}/[^/]+/?(?:default\.aspx)?",
    re.IGNORECASE,
)

# InvestorRoom (scrape_investorroom.py, lines 143–144):
#   DETAIL_URL_LEGACY_RE = re.compile(r"[?&]item=\d+", re.IGNORECASE)
#   DETAIL_URL_MODERN_RE = re.compile(r"/\d{4}-\d{2}-\d{2}-[^/#]+/?$", re.IGNORECASE)
IR_DETAIL_LEGACY_RE = re.compile(r"[?&]item=\d+", re.IGNORECASE)
IR_DETAIL_MODERN_RE = re.compile(r"/\d{4}-\d{2}-\d{2}-[^/#]+/?$", re.IGNORECASE)

# Notified/Drupal (scrape_notified.py, lines 143–147):
#   DETAIL_URL_RE = re.compile(
#       r"/(?:news-releases|press-releases|financial-releases)/[^/#?]+/[^/#?]+",
#       re.IGNORECASE,
#   )
NOTIFIED_DETAIL_RE = re.compile(
    r"/(?:news-releases|press-releases|financial-releases)/[^/#?]+/[^/#?]+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

_SESSION = None


def get_session():
    """Return a shared HTTP session.

    Uses curl_cffi with Chrome impersonation when available — required for
    IR sites (particularly Notified/Drupal) that enforce TLS fingerprinting
    and silently drop or time out connections from the standard Python stack.
    scrape_notified.py documents this requirement explicitly.
    Falls back to a plain requests.Session if curl_cffi is not installed,
    which works for sites without TLS fingerprint checks.
    """
    global _SESSION
    if _SESSION is None:
        if _HTTP_BACKEND == "curl_cffi":
            # impersonate="chrome124" sets JA3/JA4 + HTTP/2 SETTINGS to match
            # a real Chrome 124 client, bypassing TLS-fingerprint blocks.
            _SESSION = requests.Session(impersonate="chrome124")
            logger.debug("HTTP backend: curl_cffi (Chrome impersonation)")
        else:
            logger.warning(
                "curl_cffi not installed — falling back to plain requests. "
                "Sites with TLS fingerprinting (e.g. AbbVie/Notified) may "
                "timeout or be misclassified. Install with: pip install curl_cffi"
            )
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


def fetch_html(url: str, timeout: int) -> tuple[str, str]:
    """GET *url* and return (final_url, html).

    Follows redirects. Returns the final URL after redirects alongside the
    page HTML so callers can log where the request actually landed.
    Raises on HTTP errors.
    """
    resp = get_session().get(url, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp.url, resp.text

# ---------------------------------------------------------------------------
# Platform detection: signal tests on parsed HTML
# ---------------------------------------------------------------------------

def _check_q4(soup: BeautifulSoup, html: str) -> bool:
    """Q4 fingerprints (from scrape_q4_ir.py docstring and constants):

    1. Any <a href> matches the Q4 news-details URL shape.
    2. Any link/script/src reference to /news/default.aspx.
    """
    # Signal 1: news-details link pattern
    for tag in soup.find_all("a", href=True):
        if Q4_NEWS_LINK_RE.search(tag["href"]):
            logger.debug("Q4 signal: news-details link → %s", tag["href"])
            return True

    # Signal 2: /news/default.aspx in the raw HTML (covers <link>, <script src>, etc.)
    if "/news/default.aspx" in html.lower():
        logger.debug("Q4 signal: /news/default.aspx in page source")
        return True

    return False


def _check_investorroom(soup: BeautifulSoup, html: str) -> bool:
    """InvestorRoom fingerprints (from scrape_investorroom.py docstring):

    1. filecache.investorroom.com appears anywhere in the page source (CDN for
       static assets and PDFs).
    2. The string "investorroom" appears in the page source.
    3. Any link matches the legacy (?item=NNNN) or modern (YYYY-MM-DD-slug) URL shape.
    """
    lower_html = html.lower()

    # Signal 1: CDN hostname
    if "filecache.investorroom.com" in lower_html:
        logger.debug("InvestorRoom signal: filecache.investorroom.com in source")
        return True

    # Signal 2: platform name string
    if "investorroom" in lower_html:
        logger.debug("InvestorRoom signal: 'investorroom' string in source")
        return True

    # Signal 3: detail-page URL patterns
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if IR_DETAIL_LEGACY_RE.search(href):
            logger.debug("InvestorRoom signal: legacy ?item= link → %s", href)
            return True
        if IR_DETAIL_MODERN_RE.search(href):
            logger.debug("InvestorRoom signal: modern date-slug link → %s", href)
            return True

    return False


def _check_notified(soup: BeautifulSoup, html: str) -> bool:
    """Notified/Drupal fingerprints (from scrape_notified.py docstring):

    1. <meta name="Generator" content="Drupal 10 ..."> in <head>.
    2. Any link matches /news-releases/news-release-details/<slug> shape.
    """
    # Signal 1: Drupal generator meta tag
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"^generator$", re.I)}):
        content = meta.get("content", "")
        if "drupal" in content.lower():
            logger.debug("Notified signal: Drupal generator meta → %s", content)
            return True

    # Signal 2: news-release-details path pattern
    for tag in soup.find_all("a", href=True):
        if NOTIFIED_DETAIL_RE.search(tag["href"]):
            logger.debug("Notified signal: detail link → %s", tag["href"])
            return True

    return False


def detect_platform_from_html(html: str) -> str:
    """Classify the IR platform from page HTML using documented fingerprints.

    Priority: notified > investorroom > q4
    Notified is checked first because its Drupal meta tag is definitive, and
    some Notified sites share link-path patterns with InvestorRoom.
    """
    soup = BeautifulSoup(html, "lxml")

    if _check_notified(soup, html):
        return "notified"
    if _check_investorroom(soup, html):
        return "investorroom"
    if _check_q4(soup, html):
        return "q4"
    return "unknown"


def detect_platform(ir_url: str, timeout: int) -> str:
    """Fetch *ir_url* and return the detected platform name.

    Returns 'unknown' on any network or HTTP error so callers always get a
    string rather than an exception.
    """
    if not ir_url:
        return "unknown"
    try:
        final_url, html = fetch_html(ir_url, timeout=timeout)
        if final_url != ir_url:
            logger.debug("Redirected: %s → %s", ir_url, final_url)
        return detect_platform_from_html(html)
    except Exception as exc:
        logger.warning("fetch failed for %s: %s", ir_url, exc)
        return "unknown"

# ---------------------------------------------------------------------------
# sources.yaml helpers
# ---------------------------------------------------------------------------

def load_sources(yaml_path: Path) -> pd.DataFrame:
    """Load sources.yaml and return a DataFrame (slug, name, ticker, ir_url)."""
    try:
        from ruamel.yaml import YAML
        yaml = YAML()
        with yaml_path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
    except ImportError:
        import yaml  # type: ignore[import]
        with yaml_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

    records = data.get("sources", [])
    return pd.DataFrame(
        [
            {
                "slug":   rec.get("slug", ""),
                "name":   rec.get("name", ""),
                "ticker": rec.get("ticker", ""),
                "ir_url": rec.get("ir_url", ""),
            }
            for rec in records
        ],
        columns=["slug", "name", "ticker", "ir_url"],
    )


def find_row(df: pd.DataFrame, query: str) -> Optional[pd.Series]:
    """Find a row matching *query* as slug, ticker, or ir_url hostname."""
    q = query.strip().lower()

    mask = df["slug"].str.lower() == q
    if mask.any():
        return df[mask].iloc[0]

    mask = df["ticker"].str.lower() == q
    if mask.any():
        return df[mask].iloc[0]

    # URL match: compare by hostname (strip www.)
    try:
        query_host = urlparse(query).netloc.lower().lstrip("www.")
    except Exception:
        query_host = ""

    if query_host:
        def host_match(url: str) -> bool:
            try:
                return urlparse(url).netloc.lower().lstrip("www.") == query_host
            except Exception:
                return False

        mask = df["ir_url"].apply(host_match)
        if mask.any():
            return df[mask].iloc[0]

    return None

# ---------------------------------------------------------------------------
# Parallel detection over a DataFrame
# ---------------------------------------------------------------------------

def detect_platforms_parallel(df: pd.DataFrame, workers: int, timeout: int) -> pd.DataFrame:
    """Detect IR platform for every row in *df*, using a thread pool.

    Returns a new DataFrame with columns: slug, ticker, platform, ir_url.
    Rows retain the same order as *df*.
    """
    rows = df[["slug", "ticker", "ir_url"]].to_dict("records")

    # Pre-allocate results list so we can fill by index (preserves order)
    results = [None] * len(rows)

    def detect_one(idx_row: tuple[int, dict]) -> tuple[int, str]:
        idx, row = idx_row
        platform = detect_platform(row["ir_url"], timeout=timeout)
        return idx, platform

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(detect_one, (i, r)): i for i, r in enumerate(rows)}
        for future in concurrent.futures.as_completed(futures):
            idx, platform = future.result()
            results[idx] = platform

    result_df = df[["slug", "ticker", "ir_url"]].copy()
    result_df["platform"] = results
    return result_df[["slug", "ticker", "platform", "ir_url"]]

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_table(df: pd.DataFrame) -> None:
    """Print *df* as a clean fixed-width table to stdout.

    Plain ASCII, no ANSI codes — trivially redirectable with >, tee, etc.
    """
    cols = list(df.columns)
    widths = {col: len(col) for col in cols}
    for _, row in df.iterrows():
        for col in cols:
            widths[col] = max(widths[col], len(str(row[col])))

    def fmt_row(values: list[str]) -> str:
        return "  ".join(v.ljust(widths[col]) for col, v in zip(cols, values))

    print(fmt_row(cols))
    print("  ".join("-" * widths[col] for col in cols))
    for _, row in df.iterrows():
        print(fmt_row([str(row[col]) for col in cols]))

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--slug", metavar="SLUG",
        help="Look up a company by its sources.yaml slug (e.g. costco).",
    )
    target.add_argument(
        "--ticker", metavar="TICKER",
        help="Look up a company by its stock ticker (e.g. COST).",
    )
    target.add_argument(
        "--url", metavar="URL",
        help="Detect the platform for an arbitrary IR URL. Looked up in "
             "sources.yaml by hostname; if not found, fetched directly.",
    )
    target.add_argument(
        "--all", action="store_true",
        help="Detect the platform for every entry in --sources.",
    )

    parser.add_argument(
        "--sources", metavar="PATH", type=Path, default=DEFAULT_SOURCES_YAML,
        help=f"Path to sources.yaml (default: {DEFAULT_SOURCES_YAML}).",
    )
    parser.add_argument(
        "--workers", type=int, default=5, metavar="N",
        help="Number of parallel HTTP workers for --all (default: 5).",
    )
    parser.add_argument(
        "--timeout", type=int, default=20, metavar="SECONDS",
        help="Per-request HTTP timeout in seconds (default: 20).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging (shows which signals fired).",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if not any([args.slug, args.ticker, args.url, args.all]):
        parser.error(
            "Specify one of: --slug SLUG, --ticker TICKER, --url URL, or --all"
        )

    # Load sources.yaml (needed for slug/ticker/url lookups and --all)
    if not args.sources.exists():
        print(f"error: sources file not found: {args.sources}", file=sys.stderr)
        return 1

    try:
        df = load_sources(args.sources)
    except Exception as exc:
        print(f"error: could not load {args.sources}: {exc}", file=sys.stderr)
        return 1

    # --all: parallel detection across every row
    if args.all:
        result = detect_platforms_parallel(df, workers=args.workers, timeout=args.timeout)
        print_table(result)
        return 0

    # Single-target lookups
    query = args.slug or args.ticker or args.url
    row = find_row(df, query)

    if row is not None:
        ir_url = row["ir_url"]
        slug   = row.get("slug", "")
        ticker = row.get("ticker", "")
    elif args.url:
        # URL not in sources.yaml — detect directly
        ir_url = args.url
        slug   = ""
        ticker = ""
    else:
        print(f"error: no sources.yaml record found for '{query}'", file=sys.stderr)
        return 1

    platform = detect_platform(ir_url, timeout=args.timeout)
    result = pd.DataFrame([{
        "slug":     slug,
        "ticker":   ticker,
        "platform": platform,
        "ir_url":   ir_url,
    }])
    print_table(result[["slug", "ticker", "platform", "ir_url"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())