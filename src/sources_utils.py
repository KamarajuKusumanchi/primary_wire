#!/usr/bin/env python3
"""
sources_utils.py

Shared utilities for reading sources/sources.yaml.

Imported by get_source.py, update_source.py, scrape_q4_ir.py,
scrape_investorroom.py, scrape_notified.py, and any company-specific
scraper wrappers (scrape_cdw.py, scrape_costco.py, ...).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("Missing dependency. Install with: pip install ruamel.yaml")

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"


def load_sources(sources_path: Path = SOURCES_PATH) -> list[dict]:
    """Return all records from sources.yaml as a list of dicts.

    Preserves YAML comments and ordering via ruamel.yaml round-trip mode,
    so callers that write the file back (e.g. update_source.py) won't mangle
    it. Read-only callers can ignore that detail.
    """
    if not sources_path.exists():
        sys.exit(f"sources.yaml not found at {sources_path}")
    yaml = YAML()
    with open(sources_path) as f:
        data = yaml.load(f)
    return data.get("sources", [])


def find_source(sources: list[dict], query: str) -> Optional[dict]:
    """Return the first record matching *query* as a slug or ticker.

    *query* is matched case-insensitively. Returns None if not found.
    """
    q = query.strip().lower()
    for record in sources:
        if record.get("slug", "").lower() == q:
            return record
        if record.get("ticker", "").lower() == q:
            return record
    return None


def find_source_by_ir_url(sources: list[dict], url: str) -> Optional[dict]:
    """Return the first record whose ir_url matches the host of *url*.

    Matching is by hostname only (scheme-insensitive, www-stripped) so that
    ``https://investor.cdw.com/news/default.aspx`` finds the record whose
    ir_url is ``https://investor.cdw.com/``. Returns None if not found.
    """

    def _host(u: str) -> str:
        return urlparse(u).netloc.lower().lstrip("www.")

    target = _host(url)
    if not target:
        return None
    for record in sources:
        ir_url = record.get("ir_url", "")
        if ir_url and _host(ir_url) == target:
            return record
    return None


def load_source_record(slug: str, sources_path: Path = SOURCES_PATH) -> dict:
    """Return the sources.yaml record for *slug*, exiting on failure.

    Convenience wrapper used by scraper scripts that require exactly one
    record by slug and treat a missing entry as a fatal misconfiguration.
    """
    sources = load_sources(sources_path)
    record = find_source(sources, slug)
    if record is None:
        sys.exit(f"No record with slug '{slug}' found in {sources_path}")
    return record


def resolve_source_identity(
    url: Optional[str],
    slug: Optional[str],
    ticker: Optional[str],
    *,
    default_slug: str,
    default_ticker: str,
    default_url: str,
    listing_path_suffix: str = "",
    strip_url_to_root: bool = False,
    sources_path: Path = SOURCES_PATH,
    logger: "Optional[logging.Logger]" = None,
) -> tuple[str, str, str, Optional[dict]]:
    """Resolve (url, slug, ticker, matched sources.yaml record) from CLI args.

    This is the shared core of every scraper's own ``resolve_source()``
    (scrape_q4_ir.py, scrape_investorroom.py, scrape_notified.py). Each
    caller wraps this to layer on its own platform-specific fields (e.g.
    news_releases_path, first_page_index) using the returned record.

    Priority, mirroring the original per-scraper implementations:

      1. slug or ticker given  -> look up the sources.yaml record by it,
         and fill in any of url/slug/ticker the caller didn't supply.
      2. only url given        -> look up the record by the URL's host,
         and fill in slug/ticker from it.
      3. nothing given         -> fall back to (default_slug, default_ticker,
         default_url) so a bare invocation with no flags keeps working.

    listing_path_suffix is appended to a URL derived from a record's
    ir_url (case 1 above, when the caller didn't pass --url) -- e.g.
    scrape_q4_ir.py passes NEWS_PATH so it ends up with one complete
    listing URL. Scrapers that keep the site root and listing path separate
    (scrape_investorroom.py, scrape_notified.py) leave this as "".

    strip_url_to_root, when True, reduces an explicitly-passed --url to just
    its scheme+host (case 2 above) before it is matched and returned --
    used by scrapers whose listing path is appended separately elsewhere.

    Returns (url, slug, ticker, record). record is None when no sources.yaml
    entry matched (or the file could not be loaded). Warns (via `logger`,
    defaulting to this module's logger) for any field that could not be
    resolved, matching the original scrapers' behavior.
    """
    log = logger or globals()["logger"]

    try:
        sources = load_sources(sources_path)
    except Exception as exc:
        log.warning("Could not load sources.yaml (%s); slug/ticker lookup disabled.", exc)
        sources = []

    url = url or ""
    slug = slug or ""
    ticker = ticker or ""
    record: Optional[dict] = None

    if slug or ticker:
        query = slug or ticker
        record = find_source(sources, query) if sources else None
        if record is None:
            log.warning(
                "No sources.yaml record found for '%s'. Using provided values as-is.", query
            )
        else:
            slug = slug or record.get("slug", "")
            ticker = ticker or record.get("ticker", "")
            if not url:
                ir_url = record.get("ir_url", "")
                if ir_url:
                    url = ir_url.rstrip("/") + listing_path_suffix
                else:
                    log.warning(
                        "Record '%s' has no ir_url; cannot derive --url automatically.", query
                    )
    elif url:
        if strip_url_to_root:
            parsed = urlparse(url)
            url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        record = find_source_by_ir_url(sources, url) if sources else None
        if record is None:
            log.warning(
                "No sources.yaml record matched the host of '%s'. "
                "Slug and ticker will be empty unless passed explicitly.", url,
            )
        else:
            slug = record.get("slug", "")
            ticker = record.get("ticker", "")
    else:
        slug, ticker, url = default_slug, default_ticker, default_url

    if not slug:
        log.warning("Slug is empty; CSV rows will have an empty slug column.")
    if not ticker:
        log.warning("Ticker is empty; CSV rows will have an empty ticker column.")

    return url, slug, ticker, record