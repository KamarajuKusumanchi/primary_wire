#!/usr/bin/env python3
"""
src/utils/sources_utils.py

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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"


def join_url_path(base_url: str, path: str) -> str:
    """Join an IR site root with a listing/news path.

    Tolerates the presence or absence of a leading slash on *path* and a
    trailing slash on *base_url*, so ``join_url_path("https://ir.x.com/", "news")``
    and ``join_url_path("https://ir.x.com", "/news")`` both produce
    ``"https://ir.x.com/news"``.

    This replaces the naive ``base_url.rstrip("/") + path`` concatenation
    used historically across the scrapers, which silently produced a broken
    URL like ``"https://ir.x.comnews"`` whenever *path* arrived without its
    leading slash (e.g. a shell -- Git Bash/MSYS2 in particular -- rewrote a
    ``--news-releases-path=/news`` CLI argument into a filesystem path before
    Python ever saw it, or a caller simply forgot the slash).

    *path* may be "" (the default for callers like
    ``resolve_source_identity``'s ``listing_path_suffix``), in which case
    *base_url* is returned unchanged apart from trailing-slash stripping.
    """
    base = base_url.rstrip("/")
    if not path:
        return base
    return base + "/" + path.lstrip("/")


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


def find_source(
    sources: list[dict], query: str, field: Optional[str] = None
) -> Optional[dict]:
    """Return the first record matching *query* as a slug or ticker.

    *query* is matched case-insensitively. Returns None if not found.

    *field* restricts which record field is checked:
      - "slug"   -> only the record's "slug" field is compared
      - "ticker" -> only the record's "ticker" field is compared
      - None (default) -> either field may match, in that order

    Use the default (None) only for genuinely ambiguous lookups where the
    caller doesn't know (or care) whether *query* is a slug or a ticker --
    e.g. get_source.py's "SLUG_OR_TICKER" CLI argument. Callers that DO know
    which one they have (e.g. a --slug or --ticker flag was passed
    explicitly) must pass the matching *field* so that a value which happens
    to collide with the *other* field isn't silently accepted -- passing
    --slug cost should not match Costco's ticker "COST".
    """
    if field not in (None, "slug", "ticker"):
        raise ValueError(f"field must be 'slug', 'ticker', or None, got {field!r}")
    q = query.strip().lower()
    check_slug = field in (None, "slug")
    check_ticker = field in (None, "ticker")
    for record in sources:
        if check_slug and record.get("slug", "").lower() == q:
            return record
        if check_ticker and record.get("ticker", "").lower() == q:
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


def resolve_field_precedence(
    cli_value: object,
    record: Optional[dict],
    field_name: str,
    default: object,
) -> object:
    """Resolve a config field via CLI > sources.yaml record > default.

    Used for the small "explicit CLI flag beats sources.yaml, which beats a
    hardcoded default" precedence block that scrape_investorroom.py,
    scrape_notified.py, and scrape_notified_gated.py each apply to
    news_releases_path.

    *cli_value* wins if truthy. Otherwise *record*[*field_name*] wins if
    *record* is not None and the value is truthy. Otherwise *default*.

    This truthiness-based precedence is only correct for fields where "not
    set" and "falsy" are the same thing (e.g. an empty-string path). It is
    NOT correct for a field like first_page_index, where 0 is a valid,
    meaningful value that must not be treated as unset -- such fields need
    their own "is not None" precedence check instead of this helper.
    """
    if cli_value:
        return cli_value
    record_value = record.get(field_name) if record else None
    if record_value:
        return record_value
    return default


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

      1. slug or ticker given  -> look up the sources.yaml record strictly by
         that field (a --slug value is only ever matched against records'
         slug field, a --ticker value only against ticker -- so --slug cost
         will NOT match a record whose ticker happens to be COST), then
         overwrite slug/ticker/url with the matched record's canonical
         values. If both slug and ticker were given, the slug lookup takes
         priority and a mismatched --ticker triggers a warning (the record
         wins). Fills in url only if the caller didn't pass --url.
      2. only url given        -> look up the record by the URL's host,
         and fill in slug/ticker from it.
      3. nothing given         -> fall back to (default_slug, default_ticker,
         default_url) so a bare invocation with no flags keeps working.

    listing_path_suffix is appended to a URL derived from a record's
    ir_url (case 1 above, when the caller didn't pass --url) -- e.g.
    scrape_q4_ir.py passes NEWS_PATH so it ends up with one complete
    listing URL. Scrapers that keep the site root and listing path separate
    (scrape_investorroom.py, scrape_notified.py) leave this as "".

    strip_url_to_root, when True, reduces the resolved URL to just its
    scheme+host before it is returned -- whether that URL came from an
    explicitly-passed --url (case 2 above, stripped before matching) or was
    derived from a sources.yaml record's ir_url (case 1 above, stripped
    before listing_path_suffix is joined onto it). This is for scrapers
    whose listing path is appended separately elsewhere, and whose
    sources.yaml ir_url may point at a specific IR sub-page rather than the
    site root (e.g. ir_url: https://www.genpt.com/overview) -- without the
    strip, listing_path_suffix would be joined onto that sub-page path
    instead of the site root.

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
        # Look up strictly by whichever field the caller actually supplied --
        # a --slug value must match a record's slug field, never its ticker
        # field (and vice versa). If both were given, slug takes priority for
        # the lookup itself (matching the original "slug or ticker" priority)
        # but the other one is still checked below.
        if slug:
            query, field = slug, "slug"
        else:
            query, field = ticker, "ticker"
        record = find_source(sources, query, field=field) if sources else None
        if record is None:
            log.warning(
                "No sources.yaml record found with %s '%s'. Using provided values as-is.",
                field, query,
            )
        else:
            # Trust the matched record's canonical values rather than the
            # raw CLI strings -- the lookup above only guarantees *field*
            # matched (case-insensitively); the other identifier, and the
            # exact casing of *field* itself, should come from sources.yaml.
            if slug and ticker and ticker.strip().lower() != record.get("ticker", "").lower():
                log.warning(
                    "--slug '%s' resolved to sources.yaml record '%s', but its ticker "
                    "(%s) does not match --ticker '%s'. Using the record's values.",
                    slug, record.get("slug", ""), record.get("ticker", ""), ticker,
                )
            slug = record.get("slug", "")
            ticker = record.get("ticker", "")
            if not url:
                ir_url = record.get("ir_url", "")
                if ir_url:
                    if strip_url_to_root:
                        parsed = urlparse(ir_url)
                        ir_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
                    url = join_url_path(ir_url, listing_path_suffix)
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