#!/usr/bin/env python3
"""
detect_ir_platform.py

Detect which IR (investor relations) platform powers each company's IR site
by fetching the page and inspecting its HTML for documented fingerprints.
No hardcoded hostname lists — every classification is evidence-based.

This module reports two related but distinct things per slug: a
*platform* (the page fingerprint actually found -- q4, notified, investis,
etc.) and a *strategy* (which specific scraper -- src/scrape_*.py --
should handle it). Most platforms have exactly one strategy of the same
name; a few (notified, aem) have more than one. See "Platform vs.
strategy" above GATED_SLUGS, further down this file, for the full
explanation.

Supported platforms (and their fingerprints, as documented by each scraper)
---------------------------------------------------------------------------
The canonical list of platform names and their scraper modules lives in
utils.sources_utils.PLATFORMS, not here -- run
`python src/reporting/detect_ir_platform.py --list-platforms` to print it.
That list is enforced to match this module's own detection logic by
_assert_platforms_registered(), which runs at import time (see
DETECTOR_PLATFORMS above) -- so it cannot silently drift the way a
hand-copied bullet list could. What's below is what the registry can't
capture automatically: the actual page fingerprint each platform is
detected by, and why they're checked in this particular order.

q4  (scrape_q4_ir.py)
  * Links with href matching /<news_details_segment>/<year>/<slug>[/default.aspx],
    where news_details_segment defaults to "news-details" (Costco/CDW's
    theme) but is overridable per-source via sources.yaml's
    "news_details_segment" field (e.g. Netflix uses
    "press-release-details"). No literal "/news/" prefix is assumed -- some
    Q4 themes nest their news-details links elsewhere (e.g. Travelers uses
    "/newsroom/press-releases/news-details/...").
  * The source's listing path (sources.yaml's "news_path" field, defaulting
    to "news/default.aspx") appearing anywhere in the page source. This is
    also the page fetched for detection -- see _join_news_path() -- since a
    source's own root page doesn't always carry the Q4 fingerprint.

investorroom  (scrape_investorroom.py)
  * Static assets / PDFs served from filecache.investorroom.com
  * Page source contains the string "investorroom"
  * Links matching /news-releases?item=NNNNN  OR  /<YYYY-MM-DD>-<slug>

notified  (scrape_notified.py)
  * <meta name="Generator" content="Drupal 10 ..."> in the page <head>
  * Links matching /news-releases/news-release-details/<slug>

investis  (scrape_investis.py)
  * EITHER of two independent signals, both unique to this platform:
    (a) the "Investis Sitecore common GTM" HTML comment in <head>
        (Investis's own build-tooling signature), or
    (b) the footer attribution string "Delivered by Investis" / "Delivered
        by Investis Digital", or a link to investis.com / investisdigital.com.
  * IMPORTANT: Investis's press-release detail URLs look like
    /news-releases/<year>/<mm-dd-yyyy>-<serial>, e.g. Home Depot's
    https://ir.homedepot.com/news-releases/2026/07-15-2026-130113180.
    That shape has two path segments after "news-releases", which is
    exactly what Notified's broad link-pattern heuristic
    (NOTIFIED_DETAIL_RE) looks for -- so this signal must be checked BEFORE
    that heuristic, or every Investis site would be misclassified as
    "notified" instead. See _check_investis() and the priority note below.

notified_gated  (scrape_notified_gated.py; strategy "notified_gated", still
                  platform "notified" -- see "Platform vs. strategy" above
                  GATED_SLUGS)
  * Same underlying markup/fingerprints as notified above -- these are
    Notified/Drupal sites that are ALSO protected by bot mitigation (e.g.
    Akamai) strict enough to block the year-filter widget for plain/headless
    requests, so they need scrape_notified_gated.py's headed-browser step
    instead of scrape_notified.py's plain-HTTP pagination.
  * There is no general-purpose signal yet to detect "gated" automatically
    (that would mean probing bot-mitigation behavior, not just page
    content) -- for now this is a hardcoded override by slug, applied AFTER
    the normal notified/investorroom/q4 signal checks below, and only
    changes the *strategy* column, not platform (see determine_strategy()).
    See GATED_SLUGS. Real sub-classification signals are future work.

aem  (scrape_aem_bny.py, scrape_aem_cme.py -- one scraper per site, not one
      shared module; strategy "aem_bny"/"aem_cme" per slug, still platform
      "aem" -- see below and "Platform vs. strategy" above GATED_SLUGS)
  * Page source containing an "/etc.clientlibs/" or "/content/dam/" asset
    path -- Adobe Experience Manager's own client-library and DAM asset
    conventions, present regardless of which component library or bespoke
    widget a given site's theme builds on top of them.
  * NOTE (2026-08): "aem" is a real, correct platform classification here
    -- both bny and cme genuinely carry this fingerprint -- but unlike
    every other platform above, it does NOT map to a single scraper
    module. AEM is a page-authoring platform, not a prepackaged
    press-release-listing widget: each AEM site's IR team builds its own
    bespoke listing markup, pagination control, and filter UI on top of
    it, and bny's and cme's turned out to share almost none of that (see
    scrape_aem_bny.py's and scrape_aem_cme.py's module docstrings for the
    specifics -- different card markup, different pagination widgets,
    BNY has a working in-page year filter and CME doesn't). This module's
    HTML-fingerprint detection logic is unaffected -- it only asserts the
    platform, not which scraper handles it -- but the *strategy* column
    goes further, via a hardcoded slug override (AEM_BNY_SLUGS/
    AEM_CME_SLUGS -- same manual-override pattern as GATED_SLUGS above,
    for the same reason: which bespoke AEM scraper applies isn't something
    a page fingerprint can tell you). config/scraper_config.yaml has two
    groups (aem_bny, aem_cme) for this platform instead of one, and
    src/reporting/check_scraper_coverage.py's STRATEGY_TO_PLATFORM maps
    both back to "aem" so this script's own consistency check still knows
    they're the same platform even while comparing on the finer-grained
    strategy.

Priority when multiple signals fire: notified (meta tag) > investis > investorroom
> q4 > notified (link pattern) > aem
The Drupal generator meta tag is definitive and is checked first. The Investis
footer string is also definitive (unique branding) and is checked right after,
BEFORE Notified's broad link-pattern heuristic gets a chance to misfire on it
-- see the "investis" entry above for why that ordering matters (Investis's
own URL shape would otherwise satisfy the Notified heuristic). Notified's
link-pattern signal, by contrast, is a deliberately broad heuristic (any
multi-segment path under news-releases/press-releases/financial-releases --
see scrape_notified.py's DETAIL_URL_RE) that can coincidentally match a Q4
(or InvestorRoom, or Investis) site's own links -- e.g. Netflix's Q4
news-details links nest under a "financial-releases" path segment, which also
satisfies the Notified heuristic. To avoid misclassifying such sites as
"notified", that heuristic is checked before Investis, InvestorRoom, and Q4
have all had a chance to claim the link via their own more specific
patterns. aem is checked last of all: its asset-path signal is Adobe's own
platform tooling rather than listing-page markup, so it's not expected to
overlap with any other platform's signal, but it's still ordered after
every listing-markup-specific check on general principle.
This priority order governs the *platform* column only. The *strategy*
column is derived from platform afterwards, by determine_strategy():
notified_gated overrides a "notified" platform result to strategy
"notified_gated" for slugs in GATED_SLUGS; aem_bny/aem_cme override an
"aem" platform result the same way for slugs in AEM_BNY_SLUGS/
AEM_CME_SLUGS; q4 always becomes strategy "q4_ir". See "Platform vs.
strategy" above GATED_SLUGS.

unknown
  * No signal matched.

Usage
-----
  # Print the registered platform names/scrapers/descriptions and exit
  python src/reporting/detect_ir_platform.py --list-platforms

  # Single lookup
  python src/reporting/detect_ir_platform.py --slug costco
  python src/reporting/detect_ir_platform.py --ticker CMG
  python src/reporting/detect_ir_platform.py --url https://investors.abbvie.com/

  # Scan everything in sources.yaml (parallel fetches).
  # This is also the default behavior when no target flag is given.
  python src/reporting/detect_ir_platform.py --all
  python src/reporting/detect_ir_platform.py

  # Custom sources file
  python src/reporting/detect_ir_platform.py --all --sources /path/to/sources.yaml

  # Dump the raw fetched HTML alongside the normal CSV output, for a site
  # this script gets wrong (or isn't in sources.yaml at all yet) -- hand
  # the file to someone else to eyeball the markup by hand:
  python src/reporting/detect_ir_platform.py \
      --url https://investors.palantir.com/news \
      --debug-dump-html palantir.html

  # Redirect-friendly: output is CSV, no ANSI
  python src/reporting/detect_ir_platform.py --all > platforms.csv

  # Control concurrency and per-request timeout
  python src/reporting/detect_ir_platform.py --all --workers 8 --timeout 15

  # --all also cross-checks every scraper_config.yaml-configured slug's
  # platform against what's freshly detected here, printing any mismatch to
  # stderr (never stdout, so the CSV above stays clean); --strict turns a
  # mismatch into a non-zero exit code instead of just a warning.
  python src/reporting/detect_ir_platform.py --all --strict

Output
------
CSV with header row: slug,ticker,platform,strategy,scrape_url

platform is what this module's page-fingerprint checks actually found
(q4, investorroom, notified, investis, aem, or unknown). strategy is
which specific scraper (src/scrape_*.py) should handle this slug -- for
most sources this is just *platform* again (one platform, one scraper),
but three cases split further: a bot-gated Notified slug (GATED_SLUGS)
gets strategy "notified_gated" while platform stays "notified"; every Q4
slug gets strategy "q4_ir" (matching the "q4_ir" scraper_config.yaml
group) while platform stays "q4"; and an AEM slug gets strategy
"aem_bny"/"aem_cme" once it's in AEM_BNY_SLUGS/AEM_CME_SLUGS (else just
"aem", meaning "known platform, no scraper assigned yet") while platform
stays "aem" throughout. See determine_strategy() and the "Platform vs.
strategy" comment above GATED_SLUGS for the full mapping.

scrape_url is the full press-release *listing* URL for the detected
platform/strategy -- the site root actually fetched for detection
(news_url if set, else ir_url; see resolve_scrape_url()) plus that
platform's listing path (sources.yaml's "news_path" for q4,
"news_releases_path" for investorroom/notified/notified_gated/investis;
see resolve_listing_url()/_resolve_listing_platform(), or run
--list-platforms for the current field/default per platform) -- so it's
directly pasteable into a browser to see the same page the platform was
detected from, e.g. https://news.lockheedmartin.com/news-releases?category=788
rather than just https://news.lockheedmartin.com/. For "unknown" rows,
there's no reliable listing path to join, so scrape_url falls back to the
bare site root. For the handful of sources where news_url differs from
ir_url (e.g. IBM, Lockheed Martin), ir_url itself is intentionally NOT in
this output; look it up in sources.yaml by slug if you need it.
To view this as a human-friendly fixed-width table, pipe it through the
companion script, e.g.:
  python src/reporting/detect_ir_platform.py --all | python src/print_csv_table.py
  python src/print_csv_table.py reports/latest/ir_platform.csv

With --all, any disagreement between config/scraper_config.yaml (which
slugs have a hand-verified scraper -- i.e. a strategy -- configured for
them) and this run's own freshly detected strategy for that slug is
printed to stderr as "warning: ..." lines -- see
check_scraper_config_consistency(). This never touches stdout, so
`invoke ir-platform` (which captures this script's stdout straight into
reports/latest/ir_platform.csv) still gets a clean
CSV; the warnings show up as tasks.py's separately-printed stderr output.

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.q4_link_pattern import (  # noqa: E402
    DEFAULT_NEWS_PATH,
    q4_news_link_re,
    strip_year_placeholder,
)
from utils.sources_utils import (  # noqa: E402
    PLATFORMS,
    describe_platforms,
    join_url_path,
    platform_names,
    resolve_listing_url,
    resolve_scrape_url,
)
# STRATEGY_TO_PLATFORM/load_scraper_config are check_scraper_coverage.py's
# (already battle-tested there) machinery for turning config/scraper_config.yaml
# into slug->strategy->platform facts, notably the "q4_ir" group -> "q4"
# platform-name translation -- reused here rather than re-derived, so the two
# scripts can't quietly disagree about what a scraper_config.yaml group name
# means. See check_scraper_config_consistency() below for why
# detect_ir_platform.py needs this too.
from reporting.check_scraper_coverage import (  # noqa: E402
    STRATEGY_TO_PLATFORM,
    SCRAPER_CONFIG_PATH as DEFAULT_SCRAPER_CONFIG_YAML,
    load_scraper_config,
)

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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SOURCES_YAML = REPO_ROOT / "sources" / "sources.yaml"

# ---------------------------------------------------------------------------
# Platform names this module's own fingerprint checks can produce
# ---------------------------------------------------------------------------
#
# Named constants instead of bare string literals scattered through
# detect_platform_from_html()/detect_platform() below, for two reasons:
#   1. A typo in a literal ("investsi") would silently create a brand new,
#      wrong platform value instead of erroring; a typo in a constant name
#      is a NameError.
#   2. DETECTOR_PLATFORMS -- the tuple of every value _check_*() /
#      detect_platform() can actually return (other than PLATFORM_UNKNOWN)
#      -- gives _assert_platforms_registered() below something concrete to
#      check against utils.sources_utils.PLATFORMS. Add a new _check_<x>()
#      function and a new PLATFORM_<X> constant here, but forget to add a
#      matching entry to PLATFORMS in utils/sources_utils.py, and the
#      assertion at the bottom of this block fails at import time --
#      instead of the mismatch just quietly sitting there until someone
#      notices resolve_listing_url() doing the wrong thing for it.
PLATFORM_Q4 = "q4"
PLATFORM_INVESTORROOM = "investorroom"
PLATFORM_NOTIFIED = "notified"
PLATFORM_NOTIFIED_GATED = "notified_gated"
PLATFORM_INVESTIS = "investis"
PLATFORM_AEM = "aem"
PLATFORM_UNKNOWN = "unknown"

DETECTOR_PLATFORMS = (
    PLATFORM_Q4,
    PLATFORM_INVESTORROOM,
    PLATFORM_NOTIFIED,
    PLATFORM_NOTIFIED_GATED,
    PLATFORM_INVESTIS,
    PLATFORM_AEM,
)


def _assert_platforms_registered() -> None:
    """Fail fast if DETECTOR_PLATFORMS and utils.sources_utils.PLATFORMS
    disagree about which platforms exist.

    This is the actual enforcement behind the "single source of truth"
    claim in utils/sources_utils.py's PLATFORMS comment: without it, adding
    a new _check_<platform>() here and forgetting to register it there (or
    vice versa) would be a silent gap, discovered only whenever someone
    happens to notice resolve_listing_url() or check_scraper_coverage.py
    behaving oddly for that platform -- possibly much later, by a different
    person, with no clear link back to the missing registration. Called
    once at import time (see bottom of this block) rather than left as a
    test someone has to remember to run.
    """
    known = set(platform_names())
    detected = set(DETECTOR_PLATFORMS)
    missing_from_registry = detected - known
    missing_from_detector = known - detected
    if missing_from_registry:
        raise AssertionError(
            f"detect_ir_platform.py can return platform(s) {sorted(missing_from_registry)} "
            "that utils.sources_utils.PLATFORMS doesn't know about. Add a matching "
            "entry to PLATFORMS in src/utils/sources_utils.py."
        )
    if missing_from_detector:
        raise AssertionError(
            f"utils.sources_utils.PLATFORMS registers platform(s) "
            f"{sorted(missing_from_detector)} that detect_ir_platform.py has no "
            "_check_<platform>() for and never returns. Either add detection "
            "support here, or remove the registry entry if it's not a real, "
            "independently-fingerprintable platform (e.g. notified_gated, which "
            "IS real and IS in DETECTOR_PLATFORMS -- see GATED_SLUGS below for why "
            "it doesn't get its own _check_*() function)."
        )


_assert_platforms_registered()

# ---------------------------------------------------------------------------
# Fingerprint regexes
# ---------------------------------------------------------------------------
# InvestorRoom's and Notified's regexes below are taken verbatim from each
# scraper's source. Q4's is the one exception: it's imported from
# utils/q4_link_pattern.py, shared with scrape_q4_ir.py -- see the comment
# just below.
# ---------------------------------------------------------------------------

# Q4 (scrape_q4_ir.py, _news_link_matcher()): the news-details link regex
# and DEFAULT_NEWS_DETAILS_SEGMENT ("news-details", overridable per-source
# via sources.yaml's "news_details_segment" field, e.g. Netflix uses
# "press-release-details") now live in utils/q4_link_pattern.py, shared with
# scrape_q4_ir.py, rather than being copied verbatim here.
#
# NOTE: the shared regex does NOT require a literal "/news/" path segment
# before the details segment -- only the Costco/CDW-style default theme
# happens to nest it under "news/". Other Q4 themes nest it elsewhere (e.g.
# Travelers uses "/newsroom/press-releases/news-details/<year>/<slug>/
# default.aspx"), so hardcoding "/news/" here would silently miss them.
# _check_q4() builds this regex per-source, using the matched sources.yaml
# record's "news_details_segment" when given.

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

# Investis: two independent signals, either of which is treated as
# definitive on its own (see _check_investis() below):
#
# 1. INVESTIS_SITECORE_COMMENT_RE -- the "Investis Sitecore common GTM"
#    HTML comment in <head>. This is Investis's own build tooling/template
#    signature (their Sitecore CMS's shared GTM snippet), not user-editable
#    marketing copy.
#
# 2. INVESTIS_FOOTER_RE -- the visible page-footer branding credit and/or
#    its outbound link: "Delivered by Investis" (plain branding) or
#    "Delivered by Investis Digital" (Digital branding), linking to
#    investis.com or investisdigital.com respectively.
INVESTIS_SITECORE_COMMENT_RE = re.compile(
    r"investis\s+sitecore\s+common\s+gtm", re.IGNORECASE
)
INVESTIS_FOOTER_RE = re.compile(
    r"delivered\s+by\s+investis"                 # visible footer credit text
    r"|investisdigital\.com"                     # "Digital" branding's link target
    r"|(?:https?:)?//(?:www\.)?investis\.com\b",  # plain branding's link target
    re.IGNORECASE,
)

# AEM (scrape_aem_bny.py's / scrape_aem_cme.py's module docstring
# "Fingerprint" sections): Adobe
# Experience Manager's own client-library and DAM asset path conventions,
# present on every AEM-rendered page regardless of the specific theme/
# component library built on top of it (BNY's press-release cards, for
# instance, are a bespoke widget, not Adobe's documented Core Components --
# this fingerprint doesn't depend on that markup at all).
AEM_ASSET_PATH_RE = re.compile(r"/etc\.clientlibs/|/content/dam/", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Platform vs. strategy
# ---------------------------------------------------------------------------
#
# A "platform" (PLATFORM_* above) is what the page-fingerprint checks in
# this module actually detect -- evidence read off the page itself, e.g.
# "this page carries a Drupal generator meta tag" -> platform "notified".
#
# A "strategy" is which specific scraper module (src/scrape_*.py) should be
# used to scrape a given slug. Most platforms have exactly one strategy,
# with the same name as the platform (e.g. "investorroom" -> "investorroom"
# -> scrape_investorroom.py). Two platforms currently have more than one:
#
#   - notified: GATED_SLUGS below need scrape_notified_gated.py's
#     headed-browser step instead of scrape_notified.py's plain-HTTP
#     pagination -- see module docstring's "notified_gated" entry. Strategy
#     "notified_gated" for those slugs, "notified" for everyone else.
#   - aem: bny and cme both fingerprint as "aem" but, as the module
#     docstring's "aem" entry explains, share no scraper module at all --
#     each site gets its own bespoke scraper (scrape_aem_bny.py,
#     scrape_aem_cme.py). Strategy "aem_bny" / "aem_cme" per the slug sets
#     below; any as-yet-unassigned AEM slug (most of them, today -- see
#     TODO below) just gets strategy "aem", meaning "known platform, no
#     scraper written for it yet".
#
# determine_strategy() below is the one place this platform -> strategy
# expansion happens; STRATEGY_TO_PLATFORM (imported from
# check_scraper_coverage.py) is its inverse, going from a
# scraper_config.yaml group name (== a strategy name) back down to the
# platform it rolls up to.
#
# None of GATED_SLUGS/AEM_BNY_SLUGS/AEM_CME_SLUGS below is derived from page
# content -- there's no general-purpose signal yet for "this Notified site
# is bot-gated" or "this AEM site is bny's vs. cme's bespoke markup", since
# both require something more than a static fingerprint check (probing bot
# mitigation, or writing/matching a scraper for the specific site). These
# are manual, temporary overrides keyed by slug; add to them once you've
# confirmed (by hand, or once a scraper is written) that a slug needs a
# given strategy.
#
# TODO: AEM_BNY_SLUGS/AEM_CME_SLUGS are the one hardcoded exception to this
# module's "no hardcoded hostname lists -- every classification is
# evidence-based" philosophy (see module docstring's opening line). For now
# that's fine (there are only two AEM scrapers to choose between), but if a
# third AEM site gets its own scraper, consider deriving the strategy from
# something in the codebase itself (e.g. which scrape_aem_*.py module a
# slug is configured under in scraper_config.yaml) rather than growing a
# third hardcoded set here.
STRATEGY_Q4_IR = "q4_ir"
STRATEGY_AEM_BNY = "aem_bny"
STRATEGY_AEM_CME = "aem_cme"

# Slugs known to be Notified/Drupal sites gated by bot mitigation strict
# enough to need scrape_notified_gated.py's headed-browser step (see module
# docstring's "notified_gated" entry). This is a manual, temporary list --
# there's no content-based signal yet to detect "gated" automatically, since
# that requires probing bot-mitigation behavior rather than page content.
# Confirmed gated: TJX. Add a slug here once you've confirmed (by testing
# headless vs. headed Chrome, as documented in scrape_notified_gated.py)
# that a site needs the gated variant.
GATED_SLUGS = {"tjx", "robinhood", "caseys"}

# Slugs scraped by scrape_aem_bny.py / scrape_aem_cme.py respectively --
# both are "aem" platform, but each site gets its own bespoke scraper (see
# the "Platform vs. strategy" note above and this module's "aem" docstring
# entry for why there's no single shared AEM scraper). An AEM slug not in
# either set below has strategy "aem" (known platform, no scraper written
# for it yet), not an error.
AEM_BNY_SLUGS = {"bny"}
AEM_CME_SLUGS = {"cme"}


def determine_strategy(platform: str, slug: str) -> str:
    """Map a detected *platform* + *slug* to the scraping strategy that
    should handle it -- see "Platform vs. strategy" above.

    Defaults to *platform* itself: the common case is one platform, one
    strategy, sharing a name (e.g. "investorroom" -> "investorroom",
    "unknown" -> "unknown"). The three platform-specific overrides:

      - notified + slug in GATED_SLUGS -> "notified_gated"
      - q4 -> always "q4_ir" (there's only one Q4 scraper today, but this
        keeps the strategy column visibly distinct from the platform
        column even in the single-strategy case, and leaves room for a
        second Q4 strategy later without a rename)
      - aem + slug in AEM_BNY_SLUGS -> "aem_bny"; slug in AEM_CME_SLUGS ->
        "aem_cme"; any other aem slug -> "aem" unchanged (no scraper
        assigned yet)
    """
    slug_l = slug.strip().lower()
    if platform == PLATFORM_NOTIFIED and slug_l in GATED_SLUGS:
        return PLATFORM_NOTIFIED_GATED
    if platform == PLATFORM_Q4:
        return STRATEGY_Q4_IR
    if platform == PLATFORM_AEM:
        if slug_l in AEM_BNY_SLUGS:
            return STRATEGY_AEM_BNY
        if slug_l in AEM_CME_SLUGS:
            return STRATEGY_AEM_CME
        return platform
    return platform


def _resolve_listing_platform(platform: str, strategy: str) -> str:
    """Return the value to hand utils.sources_utils.resolve_listing_url()
    for this row.

    resolve_listing_url() is keyed by utils.sources_utils.PLATFORMS, whose
    granularity mostly matches *platform* -- except for notified_gated,
    which has its own registry entry (a different default listing path
    than plain notified) despite being a *strategy* value here, not a
    platform value. Every other strategy (q4_ir, aem_bny, aem_cme) shares
    its listing-path field/default with its parent platform (there's no
    separate PLATFORMS entry for them), so passing the strategy there
    instead of the platform would silently lose the field lookup rather
    than finding a more specific one -- resolve_listing_url() falls back
    to the bare site root for any key it doesn't recognize.
    """
    if strategy == PLATFORM_NOTIFIED_GATED:
        return strategy
    return platform

# ---------------------------------------------------------------------------
# news_path handling
# ---------------------------------------------------------------------------
#
# Some Q4 sites' listing page lives at a sub-path of ir_url rather than at
# ir_url itself -- e.g. Travelers' listing page is
# https://investor.travelers.com/newsroom/press-releases/default.aspx, not
# https://investor.travelers.com/. sources.yaml records this sub-path in the
# "news_path" field (the same field scrape_q4_ir.py reads to build the URL it
# scrapes -- see that script's DEFAULT_NEWS_PATH / resolve_source()). If we
# only ever fetch ir_url itself, sites like this never show the Q4
# news-details links and get misclassified as "unknown".


def _join_news_path(ir_url: str, news_path: str) -> str:
    """Join *ir_url* with sources.yaml's "news_path" field, if any.

    Thin wrapper around utils.sources_utils.join_url_path() that also drops
9    a "{year}" placeholder (used by year-specific listing URLs, e.g.
    Netflix's) via strip_year_placeholder() first -- detection only needs
    *some* listing page to check for platform fingerprints, not a
    particular year, mirroring scrape_q4_ir.py's _resolve_year_url() when
    no --year is given. Returns *ir_url* unchanged if *news_path* is empty.
    """
    if not news_path:
        return ir_url
    return join_url_path(ir_url, strip_year_placeholder(news_path))

# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------

def new_session():
    """Build and return a fresh HTTP session.

    Uses curl_cffi with Chrome impersonation when available — required for
    IR sites (particularly Notified/Drupal) that enforce TLS fingerprinting
    and silently drop or time out connections from the standard Python stack.
    scrape_notified.py documents this requirement explicitly.
    Falls back to a plain requests.Session if curl_cffi is not installed,
    which works for sites without TLS fingerprint checks.

    Deliberately NOT cached behind a module-level singleton.
    detect_platforms_parallel() below fetches every source concurrently via
    ThreadPoolExecutor -- a cached ``_SESSION`` would then be silently shared,
    unsynchronized, across every worker thread at once. curl_cffi's Session
    wraps a single libcurl handle and is not safe to use from more than one
    thread at a time, and plain requests.Session is documented as
    thread-unsafe too, so "share one session across threads" was never
    actually safe here, even though the lightweight one-GET-per-source
    workload made that easy to not notice.

    Call this once per detection (see detect_one() inside
    detect_platforms_parallel(), and the single-target path in main()) and
    thread the result through explicitly as the ``session`` argument below,
    rather than reaching for a global or a threading.local() -- a plain
    function argument is simpler, is impossible to accidentally share across
    an unrelated call, and needs no cleanup bookkeeping beyond the caller's
    own ``with new_session() as session:`` block.
    """
    if _HTTP_BACKEND == "curl_cffi":
        # impersonate="chrome124" sets JA3/JA4 + HTTP/2 SETTINGS to match
        # a real Chrome 124 client, bypassing TLS-fingerprint blocks.
        logger.debug("HTTP backend: curl_cffi (Chrome impersonation)")
        return requests.Session(impersonate="chrome124")

    logger.warning(
        "curl_cffi not installed — falling back to plain requests. "
        "Sites with TLS fingerprinting (e.g. AbbVie/Notified) may "
        "timeout or be misclassified. Install with: pip install curl_cffi"
    )
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def fetch_html(url: str, session, timeout: int) -> tuple[str, str]:
    """GET *url* and return (final_url, html).

    Follows redirects. Returns the final URL after redirects alongside the
    page HTML so callers can log where the request actually landed.
    Raises on HTTP errors.

    ``session`` is always the caller's own (built by new_session()), never a
    shared/global one -- see new_session()'s docstring for why.
    """
    resp = session.get(url, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp.url, resp.text

# ---------------------------------------------------------------------------
# Platform detection: signal tests on parsed HTML
# ---------------------------------------------------------------------------

def _check_q4(
    soup: BeautifulSoup, html: str,
    news_details_segment: str = "", news_path: str = "",
) -> bool:
    """Q4 fingerprints (from scrape_q4_ir.py's _news_link_matcher() and
    DEFAULT_NEWS_PATH):

    1. Any <a href> matches this source's news-details URL shape (segment
       defaults to "news-details", overridable via sources.yaml's
       "news_details_segment" field -- see q4_news_link_re()).
    2. Static assets or links referencing this source's listing path
       (sources.yaml's "news_path" field, defaulting to "news/default.aspx"
       -- see DEFAULT_NEWS_PATH in scrape_q4_ir.py) appear in the raw HTML.
       A "{year}" placeholder is stripped first since the literal string
       won't include a resolved year.
    """
    link_re = q4_news_link_re(news_details_segment)

    # Signal 1: news-details link pattern
    for tag in soup.find_all("a", href=True):
        if link_re.search(tag["href"]):
            logger.debug("Q4 signal: news-details link → %s", tag["href"])
            return True

    # Signal 2: listing-page path referenced in the raw HTML (covers <link>,
    # <script src>, nav <a href>, etc.) -- this source's news_path if given,
    # else the Q4 default theme's DEFAULT_NEWS_PATH ("news/default.aspx").
    listing_path = strip_year_placeholder(news_path or DEFAULT_NEWS_PATH)
    if listing_path.strip("/").lower() in html.lower():
        logger.debug("Q4 signal: listing path %r in page source", listing_path)
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


def _check_investis(html: str) -> bool:
    """Investis fingerprint: EITHER of two independent signals appearing
    anywhere in the page source --

      1. The "Investis Sitecore common GTM" HTML comment
         (INVESTIS_SITECORE_COMMENT_RE), normally in <head>.
      2. The visible footer branding credit/link (INVESTIS_FOOTER_RE):
         "Delivered by Investis[ Digital]", or a link to investis.com /
         investisdigital.com.

    A match on either is treated as definitive, same spirit as the Drupal
    generator meta tag for Notified.

    This must be checked BEFORE Notified's broad link-pattern heuristic
    (_check_notified_links): Investis's press-release detail URLs look like
    /news-releases/<year>/<mm-dd-yyyy>-<serial> (e.g. Home Depot's
    .../news-releases/2026/07-15-2026-130113180, or Sysco's
    .../news-releases/2026/07-14-2026-130519584), which has two path
    segments after "news-releases" and so satisfies NOTIFIED_DETAIL_RE too.
    Without this earlier, more specific check, every Investis site gets
    misclassified as "notified".
    """
    lower_html = html.lower()
    if INVESTIS_SITECORE_COMMENT_RE.search(lower_html):
        logger.debug("Investis signal: 'Investis Sitecore common GTM' comment in <head>")
        return True
    if INVESTIS_FOOTER_RE.search(lower_html):
        logger.debug("Investis signal: footer branding credit/link in page source")
        return True
    return False


def _check_aem(html: str) -> bool:
    """AEM fingerprint: the page source references an ``/etc.clientlibs/``
    or ``/content/dam/`` asset path (AEM's own client-library and DAM asset
    conventions -- see scrape_aem_bny.py's/scrape_aem_cme.py's module
    docstrings). These are part of
    AEM's platform tooling, not a theme choice, so they show up regardless
    of which component library (or bespoke widget) a given site's press-
    release listing actually uses.
    """
    return bool(AEM_ASSET_PATH_RE.search(html))


def _check_notified_meta(soup: BeautifulSoup) -> bool:
    """Definitive Notified/Drupal signal: <meta name="Generator" content="Drupal 10 ...">
    in <head>. No other platform produces this tag, so a match here is
    conclusive on its own -- see detect_platform_from_html().
    """
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"^generator$", re.I)}):
        content = meta.get("content", "")
        if "drupal" in content.lower():
            logger.debug("Notified signal: Drupal generator meta → %s", content)
            return True
    return False


def _check_notified_links(soup: BeautifulSoup) -> bool:
    """Heuristic Notified signal: any link matches
    /news-releases/news-release-details/<slug> shape.

    NOTIFIED_DETAIL_RE is deliberately broad (copied verbatim from
    scrape_notified.py's DETAIL_URL_RE, whose own docstring calls it
    "deliberately broad": any multi-segment path under news-releases/
    press-releases/financial-releases). That breadth means it can also
    match a Q4 theme's own news-details link when that link happens to
    nest under a same-named parent segment -- e.g. Netflix's Q4 links look
    like ".../financial-releases/press-release-details/<year>/<slug>/...",
    and "/financial-releases/press-release-details/<year>" alone satisfies
    this regex even though the site is Q4, not Notified. This is a
    heuristic, not a definitive signal, so callers must only use it as a
    last resort after Q4 and InvestorRoom's own (more specific) link checks
    have had a chance to claim the link first -- see
    detect_platform_from_html().
    """
    for tag in soup.find_all("a", href=True):
        if NOTIFIED_DETAIL_RE.search(tag["href"]):
            logger.debug("Notified signal: detail link → %s", tag["href"])
            return True
    return False


def _check_notified(soup: BeautifulSoup, html: str) -> bool:
    """Either Notified signal firing (meta tag or link pattern). Kept for
    any external caller that wants a single yes/no Notified check; internal
    classification uses _check_notified_meta() and _check_notified_links()
    separately so the two can be weighted differently -- see
    detect_platform_from_html().
    """
    return _check_notified_meta(soup) or _check_notified_links(soup)


def detect_platform_from_html(
    html: str, news_details_segment: str = "", news_path: str = "",
) -> str:
    """Classify the IR platform from page HTML using documented fingerprints.

    Priority: notified (definitive) > investis > investorroom > q4
    > notified (heuristic) > aem

    The Drupal generator meta tag is checked first and, if present, decides
    the result immediately -- no other platform can produce it. The Investis
    footer credit is checked next for the same reason (unique branding, so
    definitive on its own) -- see _check_investis().

    Notified's *link-pattern* signal is checked before AEM's, rather than
    second as the platform-priority order might suggest. That signal is a
    deliberately broad heuristic (see _check_notified_links()) that can
    coincidentally match a Q4 (or InvestorRoom, or Investis) site's own
    links, e.g. Netflix's Q4 news-details links nest under a
    "financial-releases" path segment that also satisfies the Notified
    heuristic, and Investis's own /news-releases/<year>/<mm-dd-yyyy>-<serial>
    detail URLs do too (see Home Depot in the module docstring). Checking
    Investis/Q4/InvestorRoom's more specific signals first, and only
    falling back to Notified's broad heuristic if none of them claims the
    link, avoids misclassifying those sites as "notified".

    AEM is checked last of all: its signal (an ``/etc.clientlibs/`` or
    ``/content/dam/`` asset path anywhere in the page source -- see
    _check_aem()) is a platform-tooling fingerprint, not a listing-page
    markup pattern, so there's no reason to expect it to coincidentally
    fire on a Q4/InvestorRoom/Notified/Investis site the way Notified's
    broad link-pattern heuristic can. It's still ordered last on general
    principle: every other check here identifies its platform from
    listing-page-specific markup, which is inherently more specific than
    "this page was built with AEM" -- if a still-undiscovered overlap ever
    turns up, an already-matched, more specific platform should keep
    winning rather than being pre-empted by AEM's broader signal.

    *news_details_segment* and *news_path* customize the Q4 signal checks
    for sources whose Q4 theme deviates from the Costco/CDW default (see
    _check_q4()); both come from the matched sources.yaml record.
    """
    soup = BeautifulSoup(html, "lxml")

    if _check_notified_meta(soup):
        return PLATFORM_NOTIFIED
    if _check_investis(html):
        return PLATFORM_INVESTIS
    if _check_investorroom(soup, html):
        return PLATFORM_INVESTORROOM
    if _check_q4(soup, html, news_details_segment=news_details_segment, news_path=news_path):
        return PLATFORM_Q4
    if _check_notified_links(soup):
        return PLATFORM_NOTIFIED
    if _check_aem(html):
        return PLATFORM_AEM
    return PLATFORM_UNKNOWN


def detect_platform(
    ir_url: str, session, timeout: int, slug: str = "",
    news_path: str = "", news_details_segment: str = "",
    debug_dump_html: Optional[Path] = None,
) -> tuple[str, str]:
    """Fetch *ir_url* (or its news_path sub-page, if given) and return
    (platform, strategy) -- see "Platform vs. strategy" above GATED_SLUGS
    for what each means.

    Returns ('unknown', 'unknown') on any network or HTTP error so callers
    always get a pair of strings rather than an exception.

    ``session`` is a session built by new_session() -- always required, and
    always the caller's own, never a shared/global one. See new_session()'s
    docstring for why.

    *news_path*, if given (from sources.yaml's "news_path" field -- the same
    field scrape_q4_ir.py reads to build the listing URL it scrapes), is
    joined onto *ir_url* via _join_news_path() and that combined URL is
    fetched instead of ir_url alone. Some Q4 sites' listing page (where the
    news-details fingerprint links actually live) is a sub-path of ir_url
    rather than ir_url itself -- e.g. Travelers -- so fetching ir_url alone
    would never see those links and would misclassify the site as
    "unknown".

    *news_details_segment*, if given (from sources.yaml's
    "news_details_segment" field), is passed to the Q4 signal checks so
    Q4 themes that customize the "-details" path segment (e.g. Netflix's
    "press-release-details") are still recognized.

    *slug*, if given, is passed to determine_strategy() to resolve the
    returned *strategy* value -- e.g. promoting a "notified" platform
    result to a "notified_gated" strategy for slugs in GATED_SLUGS, or an
    "aem" platform result to "aem_bny"/"aem_cme" for slugs in
    AEM_BNY_SLUGS/AEM_CME_SLUGS (see module docstring and
    determine_strategy() -- there's no content-based signal for either of
    these yet, so both are manual overrides keyed by slug rather than
    something detect_platform_from_html can determine from the page
    alone).

    *debug_dump_html*, if given, saves the raw fetched HTML to that path
    (creating parent directories as needed) before classification, mirroring
    every scraper's own --debug-dump-html flag (scrape_q4_ir.py,
    scrape_investis.py, scrape_investorroom.py, scrape_notified.py,
    scrape_notified_gated.py, scrape_aem_bny.py, scrape_aem_cme.py). Useful for sites this script
    gets wrong (or a site not yet in sources.yaml at all, via --url): dump
    the page as fetched here -- same session/impersonation/redirect handling
    as the real detection run -- and hand the file to someone else to
    eyeball the markup by hand. Nothing is dumped on a failed fetch, since
    there's no HTML yet at that point.
    """
    if not ir_url:
        return PLATFORM_UNKNOWN, PLATFORM_UNKNOWN
    fetch_url = _join_news_path(ir_url, news_path)
    try:
        final_url, html = fetch_html(fetch_url, session, timeout=timeout)
        if final_url != fetch_url:
            logger.debug("Redirected: %s → %s", fetch_url, final_url)
        if debug_dump_html:
            debug_dump_html.parent.mkdir(parents=True, exist_ok=True)
            debug_dump_html.write_text(html, encoding="utf-8")
            logger.info("Saved HTML to %s", debug_dump_html)
        platform = detect_platform_from_html(
            html, news_details_segment=news_details_segment, news_path=news_path,
        )
    except Exception as exc:
        logger.warning("fetch failed for %s: %s", fetch_url, exc)
        return PLATFORM_UNKNOWN, PLATFORM_UNKNOWN

    strategy = determine_strategy(platform, slug)
    if strategy != platform:
        logger.debug("Slug override: %s → strategy %s", slug, strategy)
    return platform, strategy

# ---------------------------------------------------------------------------
# sources.yaml helpers
# ---------------------------------------------------------------------------

def load_sources(yaml_path: Path) -> pd.DataFrame:
    """Load sources.yaml and return a DataFrame (slug, name, ticker, ir_url,
    news_url, news_path, news_details_segment, news_releases_path).

    ir_url is the official investor relations page; news_url is optional
    and set only for sources whose press releases live on a different host
    (e.g. IBM, Lockheed Martin -- see resolve_scrape_url()).

    news_path (used by Q4 sites whose listing page is a sub-path of the
    scrape URL, e.g. Travelers, Netflix -- see scrape_q4_ir.py's
    DEFAULT_NEWS_PATH) and news_details_segment (used by Q4 sites whose
    "-details" path segment isn't the default "news-details", e.g.
    Netflix's "press-release-details") are carried through so
    detect_platform() can fetch the actual listing page and recognize its
    actual link shape, instead of assuming every Q4 site looks like the
    Costco/CDW default.

    news_releases_path (used by InvestorRoom/Notified/notified_gated sites,
    e.g. Lockheed Martin's "news-releases?category=788" -- see
    scrape_investorroom.py's / scrape_notified.py's DEFAULT_NEWS_RELEASES_PATH)
    is carried through so resolve_listing_url() can report the full
    listing URL for those platforms too, not just Q4's.
    """
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
                "slug":                  rec.get("slug", ""),
                "name":                  rec.get("name", ""),
                "ticker":                rec.get("ticker", ""),
                "ir_url":                rec.get("ir_url", ""),
                "news_url":              rec.get("news_url", ""),
                "news_path":             rec.get("news_path", ""),
                "news_details_segment":  rec.get("news_details_segment", ""),
                "news_releases_path":    rec.get("news_releases_path", ""),
            }
            for rec in records
        ],
        columns=[
            "slug", "name", "ticker", "ir_url", "news_url",
            "news_path", "news_details_segment", "news_releases_path",
        ],
    )


def find_row(df: pd.DataFrame, query: str) -> Optional[pd.Series]:
    """Find a row matching *query* as slug, ticker, or ir_url/news_url hostname.

    Checks both URL fields' hosts (mirroring
    utils.sources_utils.find_source_by_url()) so a query URL matches the
    record regardless of whether it's the official IR host or the
    press-release host.
    """
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
                return bool(url) and urlparse(url).netloc.lower().lstrip("www.") == query_host
            except Exception:
                return False

        mask = df["ir_url"].apply(host_match) | df["news_url"].apply(host_match)
        if mask.any():
            return df[mask].iloc[0]

    return None

# ---------------------------------------------------------------------------
# Parallel detection over a DataFrame
# ---------------------------------------------------------------------------

def detect_platforms_parallel(df: pd.DataFrame, workers: int, timeout: int) -> pd.DataFrame:
    """Detect IR platform (and strategy) for every row in *df*, using a
    thread pool.

    Returns a new DataFrame with columns: slug, ticker, platform, strategy,
    scrape_url -- see "Platform vs. strategy" above GATED_SLUGS for what
    the two columns mean and how they can differ (e.g. platform "notified"
    with strategy "notified_gated" for a bot-gated slug, or platform "aem"
    with strategy "aem_bny"/"aem_cme" once a slug's bespoke scraper is
    known). Rows retain the same order as *df*. Detection fetches each
    row's resolved scrape URL (news_url if set, else ir_url -- see
    resolve_scrape_url()), but the output's scrape_url column reports the
    full press-release *listing* URL for the detected platform/strategy
    instead -- see resolve_listing_url() and _resolve_listing_platform()
    -- so a reader can paste this column directly into a browser and land
    on the same listing page the platform was detected from (and that a
    scraper would parse), rather than just the site root. (Previously this
    reported the bare resolved scrape URL unconditionally, which for
    InvestorRoom/Notified sources with a non-default news_releases_path --
    e.g. Lockheed Martin's "news-releases?category=788" -- looked
    plausible but wasn't the actual listing page.)
    """
    rows = df[
        ["slug", "ticker", "ir_url", "news_url", "news_path",
         "news_details_segment", "news_releases_path"]
    ].to_dict("records")

    # Pre-allocate results list so we can fill by index (preserves order)
    results: list[Optional[tuple[str, str]]] = [None] * len(rows)

    def detect_one(idx_row: tuple[int, dict]) -> tuple[int, str, str]:
        idx, row = idx_row
        # One session per row, opened and closed right here -- see
        # new_session()'s docstring for why this can't be a session shared
        # across the pool's worker threads (or cached anywhere above this
        # function). Cheap to do per-row: each detection is a single GET,
        # not a paginated scrape, so there's no keep-alive benefit being
        # given up by not caching it.
        with new_session() as session:
            platform, strategy = detect_platform(
                resolve_scrape_url(row), session, timeout=timeout, slug=row.get("slug", ""),
                news_path=row.get("news_path", ""),
                news_details_segment=row.get("news_details_segment", ""),
            )
        return idx, platform, strategy

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(detect_one, (i, r)): i for i, r in enumerate(rows)}
        for future in concurrent.futures.as_completed(futures):
            idx, platform, strategy = future.result()
            results[idx] = (platform, strategy)

    result_df = df[["slug", "ticker"]].copy()
    result_df["platform"] = [platform for platform, _ in results]
    result_df["strategy"] = [strategy for _, strategy in results]
    result_df["scrape_url"] = [
        resolve_listing_url(r, _resolve_listing_platform(platform, strategy))
        for r, (platform, strategy) in zip(rows, results)
    ]
    return result_df[["slug", "ticker", "platform", "strategy", "scrape_url"]]

# ---------------------------------------------------------------------------
# Cross-check against config/scraper_config.yaml
# ---------------------------------------------------------------------------
#
# config/scraper_config.yaml is a curated list of slugs that already have a
# working, hand-verified scraper for a specific platform -- e.g. slug
# "abbvie" living under the "notified" group is someone asserting "abbvie's
# IR site runs Notified, and scrape_notified.py successfully scrapes it".
# That assertion should always agree with what this module's own
# evidence-based fingerprint checks find when scanning the same slug fresh.
# A disagreement is a real signal worth surfacing: either scraper_config.yaml
# is stale (the site migrated to a different IR platform since the scraper
# was written, so the configured scraper may now be silently failing or
# scraping the wrong markup) or detect_platform()'s own logic has regressed.
# It is deliberately a strict SUBSET check, not a full comparison: not every
# slug in sources.yaml has a configured scraper, so most of *result_df* will
# have no counterpart in scraper_config.yaml at all -- that's expected and
# not flagged.


def load_configured_platforms(
    scraper_config_path: Path = DEFAULT_SCRAPER_CONFIG_YAML,
) -> pd.DataFrame:
    """Return a (slug, strategy, platform) DataFrame of every slug configured
    in scraper_config.yaml.

    One row per source entry across every group in *scraper_config_path*.
    The scraper_config.yaml group name a slug is configured under (e.g.
    "notified", "notified_gated", "q4_ir", "aem_bny") IS the strategy name
    -- no translation needed for that column. "platform" is that same
    group name translated down to the matching detect_platform() platform
    name via check_scraper_coverage.STRATEGY_TO_PLATFORM (e.g. "q4_ir" ->
    "q4", "aem_bny"/"aem_cme" -> "aem", "notified_gated" -> "notified") --
    reused from there rather than re-derived here, so this translation
    can't quietly drift from check_scraper_coverage.py's copy. A slug
    appearing under more than one group (a scraper_config.yaml bug
    check_scraper_coverage.py already flags) contributes one row per group
    it's under; check_scraper_config_consistency() below will then report
    a mismatch for whichever of those rows doesn't match the detected
    strategy, which is a reasonable side effect rather than something this
    function needs to special-case.
    """
    config = load_scraper_config(scraper_config_path)
    rows = [
        {
            "slug": entry["slug"],
            "strategy": group_name,
            "platform": STRATEGY_TO_PLATFORM.get(group_name, group_name),
        }
        for group_name, group in (config or {}).items()
        for entry in group.get("sources", [])
        if entry.get("slug")
    ]
    return pd.DataFrame(rows, columns=["slug", "strategy", "platform"])


def check_scraper_config_consistency(
    result_df: pd.DataFrame,
    scraper_config_path: Path = DEFAULT_SCRAPER_CONFIG_YAML,
) -> list[str]:
    """Return human-readable mismatch messages between scraper_config.yaml and *result_df*.

    *result_df* is this run's freshly detected (slug, ticker, platform,
    strategy, scrape_url) DataFrame (detect_platforms_parallel()'s return
    value). Every slug configured in scraper_config.yaml is expected to
    appear in *result_df* with the SAME strategy value scraper_config.yaml
    asserts for it (see the module comment above for why) -- comparison is
    on strategy rather than platform because strategy is the finer-grained,
    actually-actionable fact ("is this slug configured under the scraper
    that will actually work for it"); a platform-only comparison would
    miss e.g. a bny-configured slug that would now fingerprint as needing
    the cme scraper instead, since both are platform "aem". Two kinds of
    problems are reported, each as one message:

      - a configured slug missing from *result_df* entirely (e.g. removed
        or renamed in sources.yaml since scraper_config.yaml was last
        updated -- this run has no opinion on its platform/strategy at all)
      - a configured slug present in *result_df* under a DIFFERENT
        strategy than scraper_config.yaml asserts (the platform each
        asserts is included in the message for context, even when the
        platforms themselves happen to agree)

    Returns an empty list when everything configured agrees, which is the
    expected steady state. This deliberately does not attempt the
    reverse -- flagging *result_df* rows with no scraper_config.yaml
    counterpart -- since most rows are expected to have none (that's the
    "not every slug has an automated scraper" side of the subset
    relationship, and check_scraper_coverage.py already reports on that
    gap in more detail).
    """
    configured = load_configured_platforms(scraper_config_path)
    if configured.empty:
        return []

    merged = configured.merge(
        result_df[["slug", "platform", "strategy"]], on="slug", how="left",
        suffixes=("_configured", "_detected"),
    )

    messages: list[str] = []
    for _, row in merged[merged["strategy_detected"].isna()].iterrows():
        messages.append(
            f"slug '{row['slug']}' is configured in scraper_config.yaml under "
            f"strategy '{row['strategy_configured']}' (platform "
            f"'{row['platform_configured']}') but was not found in this "
            "run's detection results (renamed or removed from "
            "sources.yaml?)"
        )

    mismatched = merged[
        merged["strategy_detected"].notna()
        & (merged["strategy_detected"] != merged["strategy_configured"])
    ]
    for _, row in mismatched.iterrows():
        messages.append(
            f"slug '{row['slug']}' is configured in scraper_config.yaml under "
            f"strategy '{row['strategy_configured']}' (platform "
            f"'{row['platform_configured']}') but was detected as strategy "
            f"'{row['strategy_detected']}' (platform '{row['platform_detected']}') "
            "-- scraper_config.yaml may be stale, or platform/strategy "
            "detection may have regressed"
        )

    return messages

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_csv(df: pd.DataFrame) -> None:
    """Print *df* to stdout as machine-readable CSV, header row included.

    Column order is whatever *df* already has (callers pass
    slug, ticker, platform, scrape_url). Use print_csv_table.py to render
    this back into a human-friendly fixed-width table.
    """
    df.to_csv(sys.stdout, index=False, lineterminator="\n")

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
        help="Detect the platform for every entry in --sources. "
             "This is the default when --slug/--ticker/--url are omitted.",
    )

    parser.add_argument(
        "--sources", metavar="PATH", type=Path, default=DEFAULT_SOURCES_YAML,
        help=f"Path to sources.yaml (default: {DEFAULT_SOURCES_YAML}).",
    )
    parser.add_argument(
        "--scraper-config", metavar="PATH", type=Path, default=DEFAULT_SCRAPER_CONFIG_YAML,
        help="Path to scraper_config.yaml, used only by --all to cross-check "
             "every configured slug's platform against what's freshly "
             f"detected here (default: {DEFAULT_SCRAPER_CONFIG_YAML}). See "
             "check_scraper_config_consistency().",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="With --all, exit 1 if scraper_config.yaml disagrees with any "
             "freshly detected platform (mismatches are always printed to "
             "stderr regardless of this flag; the CSV on stdout is written "
             "either way).",
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
        "--debug-dump-html", type=Path, default=None, metavar="PATH",
        help="Save the raw fetched HTML for a single-target lookup "
             "(--slug/--ticker/--url) to PATH, in addition to the normal "
             "CSV output -- e.g. to hand the page to someone else for a "
             "manual look. Same shared flag name as every scraper's own "
             "--debug-dump-html. Ignored (with a warning) under --all, "
             "since that mode fetches many pages, not one.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging (shows which signals fired).",
    )
    parser.add_argument(
        "--list-platforms", action="store_true",
        help="Print the registered platform names, their scraper module "
             "(if any), and a one-line description, then exit. This is "
             "utils.sources_utils.PLATFORMS -- the single source of truth "
             "for platform names -- not something hand-maintained in this "
             "script's --help text, so it can't go stale the way a "
             "hardcoded list here could.",
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    if args.list_platforms:
        print(describe_platforms())
        return 0

    if not any([args.slug, args.ticker, args.url, args.all]):
        # No target specified — default to scanning every entry in sources.
        logger.info("No target specified; defaulting to --all")
        args.all = True

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
        if args.debug_dump_html:
            print(
                "warning: --debug-dump-html is ignored with --all "
                "(only single-target --slug/--ticker/--url lookups support it)",
                file=sys.stderr,
            )
        result = detect_platforms_parallel(df, workers=args.workers, timeout=args.timeout)
        # Compare against config/scraper_config.yaml BEFORE printing the CSV,
        # and report any mismatches on stderr, not stdout -- tasks.py's
        # ir-platform task captures this script's stdout verbatim into
        # reports/latest/ir_platform.csv, so anything printed to stdout here
        # would corrupt that file. See check_scraper_config_consistency()'s
        # docstring for what counts as a mismatch.
        inconsistencies = check_scraper_config_consistency(result, args.scraper_config)
        for message in inconsistencies:
            print(f"warning: {message}", file=sys.stderr)
        print_csv(result)
        if inconsistencies and args.strict:
            noun = "inconsistency" if len(inconsistencies) == 1 else "inconsistencies"
            print(
                f"error: {len(inconsistencies)} scraper_config.yaml {noun} "
                "found (see warnings above)",
                file=sys.stderr,
            )
            return 1
        return 0

    # Single-target lookups
    query = args.slug or args.ticker or args.url
    row = find_row(df, query)

    if row is not None:
        scrape_url            = resolve_scrape_url(row.to_dict())
        slug                  = row.get("slug", "")
        ticker                = row.get("ticker", "")
        news_path             = row.get("news_path", "")
        news_details_segment  = row.get("news_details_segment", "")
    elif args.url:
        # URL not in sources.yaml — detect directly, nothing to fall back to
        scrape_url            = args.url
        slug                  = ""
        ticker                = ""
        news_path             = ""
        news_details_segment  = ""
    else:
        print(f"error: no sources.yaml record found for '{query}'", file=sys.stderr)
        return 1

    with new_session() as session:
        platform, strategy = detect_platform(
            scrape_url, session, timeout=args.timeout, slug=slug,
            news_path=news_path, news_details_segment=news_details_segment,
            debug_dump_html=args.debug_dump_html,
        )
    # Report the full listing URL now that the platform/strategy is known
    # -- e.g. https://news.lockheedmartin.com/news-releases?category=788
    # rather than just https://news.lockheedmartin.com/ -- see
    # resolve_listing_url() and _resolve_listing_platform(). row (when
    # matched) carries news_releases_path for this; a bare --url with no
    # sources.yaml match has no such field, so it falls back to the
    # un-joined scrape_url for any known platform.
    listing_url = resolve_listing_url(
        row.to_dict() if row is not None else {"ir_url": scrape_url},
        _resolve_listing_platform(platform, strategy),
    )
    result = pd.DataFrame([{
        "slug":        slug,
        "ticker":      ticker,
        "platform":    platform,
        "strategy":    strategy,
        "scrape_url":  listing_url,
    }])
    print_csv(result[["slug", "ticker", "platform", "strategy", "scrape_url"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())