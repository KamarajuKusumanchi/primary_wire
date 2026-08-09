#!/usr/bin/env python3
"""
src/utils/sources_utils.py

Shared utilities for reading sources/sources.yaml.

Imported by get_source.py, update_source.py, scrape_q4_ir.py,
scrape_investorroom.py, scrape_notified.py, scrape_notified_gated.py,
scrape_investis.py, src/reporting/detect_ir_platform.py, and
src/reporting/check_scraper_coverage.py.

This module also owns PLATFORMS, the single source of truth for which IR
platforms primary_wire knows about (name, implementing scraper module,
sources.yaml listing-path field/default). See PLATFORMS' own comment for
what adding a new platform should (and should not) require touching.

ruamel.yaml is only imported lazily, inside load_sources() (the one
function that actually needs it), rather than at module level -- so a
caller that only wants the pure URL-building helpers below (e.g.
resolve_scrape_url, join_url_path, resolve_listing_url) can import this
module without being forced to have ruamel.yaml installed.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlparse, urlunparse

from utils.q4_link_pattern import DEFAULT_NEWS_PATH, strip_year_placeholder

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
    try:
        from ruamel.yaml import YAML
    except ImportError:
        sys.exit("Missing dependency. Install with: pip install ruamel.yaml")
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


def find_source_by_url(sources: list[dict], url: str) -> Optional[dict]:
    """Return the first record whose ir_url or news_url matches the host of *url*.

    Matching is by hostname only (scheme-insensitive, www-stripped) so that
    ``https://investor.cdw.com/news/default.aspx`` finds the record whose
    ir_url is ``https://investor.cdw.com/``.

    Checks both "ir_url" and the optional "news_url" field (see
    resolve_scrape_url()'s docstring), because a caller may hand in either
    host -- e.g. Lockheed Martin's press releases live at
    news.lockheedmartin.com (news_url), but a caller could just as
    plausibly pass investors.lockheedmartin.com (ir_url). Either one must
    resolve to the same record. Returns None if neither field matches.
    """

    def _host(u: str) -> str:
        return urlparse(u).netloc.lower().lstrip("www.")

    target = _host(url)
    if not target:
        return None
    for record in sources:
        candidate_urls = (record.get("ir_url", ""), record.get("news_url", ""))
        if any(candidate and _host(candidate) == target for candidate in candidate_urls):
            return record
    return None


def resolve_scrape_url(record: dict) -> str:
    """Return the base URL scrapers should use to find press releases.

    For most sources, one host serves both the investor relations page and
    the press-release listing, so "ir_url" alone is enough. A few sources
    (e.g. IBM, Lockheed Martin) host their press releases on a different
    domain than their official investor relations site; those set the
    optional "news_url" field in sources.yaml to the press-release host,
    while "ir_url" keeps pointing at the real IR page for reference.

    Precedence: "news_url" wins if set and truthy; otherwise "ir_url".
    Returns "" if neither field is set.
    """
    return record.get("news_url") or record.get("ir_url", "")


# ---------------------------------------------------------------------------
# Platform registry -- THE single source of truth for "what IR platforms
# does primary_wire know about".
# ---------------------------------------------------------------------------
#
# Every other module that needs to enumerate, validate, or look up
# platform-specific details (detect_ir_platform.py's fingerprint checks,
# check_scraper_coverage.py's config-group mapping, each scraper's own
# news_releases_path default) reads PLATFORMS below rather than keeping its
# own hardcoded copy of the platform list. That's the actual fix for doc
# drift: there is exactly one place a new platform's name/scraper/listing
# default is *declared*, and every place that previously duplicated that
# list now either imports from here or has a runtime check (see
# detect_ir_platform.py's _assert_platforms_registered() and
# check_scraper_coverage.py's _assert_config_group_mapping_valid()) that
# fails loudly at import time if this registry and that module's own
# hardcoded platform-name literals fall out of sync.
#
# Adding platform #6 should only ever require:
#   1. One new entry below.
#   2. A new scrape_<platform>.py, importing its listing-path default and
#      field name from the entry you just added (see DEFAULT_NEWS_RELEASES_PATH
#      pattern in e.g. scrape_investis.py) instead of redefining them.
#   3. A new _check_<platform>() in detect_ir_platform.py, plus adding that
#      platform's name to its DETECTOR_PLATFORMS tuple -- forgetting this
#      step is exactly what _assert_platforms_registered() catches.
#   4. A new prose section in docs/scrapers.txt (this part genuinely can't
#      be generated -- it's the one place that documents *how* the platform
#      is scraped, not just its name).
# Nothing else in this codebase should need a matching edit; if you find a
# spot that does, it's a candidate to be pointed at PLATFORMS instead.
#
# Fields:
#   listing_field         sources.yaml field holding a per-source override
#                         of the listing path (mirrors what each scraper's
#                         resolve_source() reads).
#   default_listing_path  fallback when listing_field isn't set on a given
#                         record. This is the actual single source of truth
#                         for that default -- scraper modules import it from
#                         here (as DEFAULT_NEWS_RELEASES_PATH / equivalent)
#                         instead of each redefining their own copy, which is
#                         exactly the kind of duplication that let this value
#                         and a per-scraper constant silently drift apart
#                         before this registry existed.
#   scraper_module        module name (under src/) that implements scraping
#                         for this platform, or None if detection-only (no
#                         scraper exists yet).
#   description           one-line human summary, used by
#                         detect_ir_platform.py's --list-platforms output
#                         and safe to quote in docs instead of retyping.
@dataclass(frozen=True)
class Platform:
    listing_field: str
    default_listing_path: str
    scraper_module: Optional[str]
    description: str


# notified_gated's default is tuned for TJX; in practice every currently
# known gated slug sets its own news_releases_path in sources.yaml, so this
# default rarely applies. Q4 is the only platform whose listing-path field
# is "news_path" rather than "news_releases_path"; see resolve_listing_url()'s
# "{year}" handling below for the other Q4-specific wrinkle.
PLATFORMS: dict[str, Platform] = {
    "q4": Platform(
        listing_field="news_path",
        default_listing_path=DEFAULT_NEWS_PATH,
        scraper_module="scrape_q4_ir",
        description="Q4 IR sites (news-details link pattern)",
    ),
    "investorroom": Platform(
        listing_field="news_releases_path",
        default_listing_path="news-releases",
        scraper_module="scrape_investorroom",
        description="InvestorRoom sites (filecache.investorroom.com assets)",
    ),
    "notified": Platform(
        listing_field="news_releases_path",
        default_listing_path="news-releases",
        scraper_module="scrape_notified",
        description="Notified/Drupal sites (Drupal 10 generator meta tag)",
    ),
    "notified_gated": Platform(
        listing_field="news_releases_path",
        default_listing_path="investors/press-releases",
        scraper_module="scrape_notified_gated",
        description=(
            "Notified/Drupal sites also behind bot mitigation strict enough "
            "to need a headed-browser scrape; same platform as notified, "
            "just a different way of getting past the gate"
        ),
    ),
    "investis": Platform(
        listing_field="news_releases_path",
        default_listing_path="news-releases",
        scraper_module="scrape_investis",
        description="Investis Digital sites (\"Delivered by Investis Digital\" footer)",
    ),
    "aem": Platform(
        listing_field="aem_listing_path",
        default_listing_path="",
        # Not a single scraper module: AEM is a page-authoring platform,
        # not a prepackaged press-release-listing widget, so each AEM site
        # gets its own scraper (bny -> scrape_aem_bny, cme -> scrape_aem_cme
        # -- see those modules' docstrings for why one shared scraper.py
        # didn't hold up). describe_platforms() below prints this as-is
        # rather than appending ".py" the way it does for every other
        # platform's single scraper_module.
        scraper_module="scrape_aem_bny.py, scrape_aem_cme.py (one scraper per site; see PLATFORMS['aem'])",
        description="Adobe Experience Manager sites (/etc.clientlibs/, /content/dam/ asset paths)",
    ),
}


def platform_names() -> tuple[str, ...]:
    """Return all registered platform names, sorted.

    Use this instead of hardcoding a platform list anywhere new -- e.g. for
    an argparse ``choices=`` list or a validation check. "unknown" is
    deliberately not included: it isn't a real platform, just what
    detect_ir_platform.py returns when nothing matched.
    """
    return tuple(sorted(PLATFORMS))


def describe_platforms() -> str:
    """Return a human-readable table of the platform registry, one line each.

    Used by detect_ir_platform.py's ``--list-platforms`` flag. Intended as
    the thing to run (or point to) instead of hand-copying platform names
    into a doc -- the output always matches PLATFORMS exactly, so it can't
    drift the way retyped prose can.
    """
    lines = []
    for name in platform_names():
        p = PLATFORMS[name]
        if not p.scraper_module:
            scraper = "(no scraper -- detection only)"
        elif p.scraper_module.endswith(".py") or " " in p.scraper_module:
            # Already a fully-formed display string (e.g. "aem"'s
            # multi-scraper note above) -- don't mangle it by appending
            # another ".py".
            scraper = p.scraper_module
        else:
            scraper = p.scraper_module + ".py"
        lines.append(f"{name:15} {scraper:60} {p.description}")
    return "\n".join(lines)


# Back-compat aliases: a few scrapers historically imported their listing-
# path default directly by name (e.g. `from utils.sources_utils import
# NOTIFIED_DEFAULT_NEWS_RELEASES_PATH`). These now just point at the
# registry entries above rather than being independently maintained, so
# there is still only one real value per platform -- PLATFORMS[...] --
# even though it's reachable under either name.
INVESTORROOM_DEFAULT_NEWS_RELEASES_PATH = PLATFORMS["investorroom"].default_listing_path
NOTIFIED_DEFAULT_NEWS_RELEASES_PATH = PLATFORMS["notified"].default_listing_path
NOTIFIED_GATED_DEFAULT_NEWS_RELEASES_PATH = PLATFORMS["notified_gated"].default_listing_path
INVESTIS_DEFAULT_NEWS_RELEASES_PATH = PLATFORMS["investis"].default_listing_path

_LISTING_PATH_DEFAULTS: dict[str, tuple[str, str]] = {
    name: (p.listing_field, p.default_listing_path) for name, p in PLATFORMS.items()
}


def resolve_listing_url(record: dict, platform: str) -> str:
    """Return the full press-release *listing* URL a scraper would fetch.

    resolve_scrape_url() only returns the site root (news_url if set, else
    ir_url) -- e.g. "https://news.lockheedmartin.com/" for Lockheed Martin,
    not the actual listing page a human (or scraper) needs,
    "https://news.lockheedmartin.com/news-releases?category=788". This
    function fills in that gap by joining the platform-appropriate listing
    path onto the site root, so reports can show a URL that's directly
    pasteable into a browser and matches what the scraper actually parses.

    *platform* is one of PLATFORMS' keys (see platform_names()) -- currently
    "q4", "investorroom", "notified", "notified_gated", "investis", "aem" --
    or "unknown"/anything else not in that registry. It selects which
    sources.yaml field holds the listing path and what its platform-specific
    default is. This mirrors each scraper's own
    resolve_source()/resolve_field_precedence() field lookup, minus the
    CLI-override layer (reports have no CLI flags of their own to override
    with).

    For "q4", a "{year}" placeholder in the listing path (e.g. Netflix's
    news_path) is dropped rather than filled in -- like
    detect_ir_platform.py's own detection fetch, this just needs *a*
    browsable listing URL, not one pinned to a specific year.

    For "unknown" (or any platform not in PLATFORMS), there's no reliable
    field to join -- we don't know what shape this site's listing path
    takes -- so the site root from resolve_scrape_url() is returned
    unchanged.
    """
    base_url = resolve_scrape_url(record)
    if not base_url:
        return ""

    field_default = _LISTING_PATH_DEFAULTS.get(platform)
    if field_default is None:
        return base_url

    field_name, default_path = field_default
    path = record.get(field_name) or default_path
    if platform == "q4":
        path = strip_year_placeholder(path)
    return join_url_path(base_url, path)


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

    listing_path_suffix is appended to a URL derived from a record's scrape
    URL -- resolve_scrape_url()'s "news_url if set, else ir_url" (case 1
    above, when the caller didn't pass --url) -- e.g. scrape_q4_ir.py passes
    NEWS_PATH so it ends up with one complete listing URL. Scrapers that
    keep the site root and listing path separate (scrape_investorroom.py,
    scrape_notified.py) leave this as "".

    strip_url_to_root, when True, reduces the resolved URL to just its
    scheme+host before it is returned -- whether that URL came from an
    explicitly-passed --url (case 2 above, stripped before matching) or was
    derived from a sources.yaml record's scrape URL (case 1 above, stripped
    before listing_path_suffix is joined onto it). This is for scrapers
    whose listing path is appended separately elsewhere, and whose
    sources.yaml ir_url may point at a specific IR sub-page rather than the
    site root (e.g. ir_url: https://www.genpt.com/overview) -- without the
    strip, listing_path_suffix would be joined onto that sub-page path
    instead of the site root.

    Any query string on the URL being stripped (e.g. --url ".../news-releases
    ?category=788") is captured into the returned extra_query_params dict
    before it's discarded, rather than silently dropped. Callers that build
    their own listing URL afterwards (scrape_investorroom.py's
    listing_page_url()/year_filter_url(), scrape_notified.py's
    listing_page_url()) should merge extra_query_params back into whatever
    params they construct, so a site-specific filter like ?category=788
    survives alongside the scraper's own ?l=/?o=/?year= params.

    Returns (url, slug, ticker, record, extra_query_params). record is None
    when no sources.yaml entry matched (or the file could not be loaded).
    extra_query_params is {} when strip_url_to_root is False, or when the
    stripped URL had no query string. Warns (via `logger`, defaulting to
    this module's logger) for any field that could not be resolved, matching
    the original scrapers' behavior.
    """
    log = logger or globals()["logger"]
    extra_query_params: dict[str, str] = {}

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
                scrape_url = resolve_scrape_url(record)
                if scrape_url:
                    if strip_url_to_root:
                        parsed = urlparse(scrape_url)
                        if parsed.query:
                            extra_query_params.update(parse_qsl(parsed.query, keep_blank_values=True))
                        scrape_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
                    url = join_url_path(scrape_url, listing_path_suffix)
                else:
                    log.warning(
                        "Record '%s' has neither news_url nor ir_url; "
                        "cannot derive --url automatically.", query
                    )
    elif url:
        if strip_url_to_root:
            parsed = urlparse(url)
            if parsed.query:
                extra_query_params.update(parse_qsl(parsed.query, keep_blank_values=True))
            url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        record = find_source_by_url(sources, url) if sources else None
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

    return url, slug, ticker, record, extra_query_params