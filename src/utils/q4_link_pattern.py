#!/usr/bin/env python3
"""
src/utils/q4_link_pattern.py

Shared Q4 Inc. IR-theme URL rules, factored out of scrape_q4_ir.py and
src/reporting/detect_ir_platform.py, which both independently derived the
same news-details link regex and the same "{year}" placeholder handling.

Q4-powered IR sites (Costco, CDW, and many more in sources.yaml) share the
same news-details URL shape:

    /<news_details_segment>/<year>/<slug>[/default.aspx]

where news_details_segment defaults to "news-details" (Costco/CDW's theme)
but is overridable per-source via sources.yaml's "news_details_segment"
field (e.g. Netflix uses "press-release-details").

Some Q4 themes also bake the year directly into the listing URL's path via
a "{year}" placeholder segment in sources.yaml's "news_path" field (e.g.
Netflix); DEFAULT_NEWS_PATH is the fallback when a source has no such field.

Imported by:
  - scrape_q4_ir.py, which scrapes a Q4 listing page.
  - src/reporting/detect_ir_platform.py, which fingerprints a source's IR
    platform by checking whether this link shape appears on its page.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Optional

# The "-details" path segment used by press-release detail links, e.g. the
# "news-details" in /news/news-details/<year>/<slug>/default.aspx. Most Q4
# themes (Costco, CDW) share this literal segment; some (e.g. Netflix, whose
# detail links use /investor-news-and-events/financial-releases/
# press-release-details/<year>/<slug>/default.aspx) use a different word.
# Overridable via sources.yaml's "news_details_segment" field.
DEFAULT_NEWS_DETAILS_SEGMENT = "news-details"

# Listing-page path appended to a slug/ticker-derived ir_url when a source
# doesn't specify its own sources.yaml "news_path". Most Q4 themes (Costco,
# CDW) use a fixed listing URL and select the year via an in-page dropdown
# instead. Some themes (e.g. Netflix) embed the year directly in the listing
# URL's path via a "{year}" placeholder segment in news_path.
DEFAULT_NEWS_PATH = "news/default.aspx"


def q4_news_link_re(news_details_segment: str = "") -> re.Pattern:
    """Build the regex that matches a Q4 press-release detail link for one
    source's theme.

    For the default "news-details" segment this matches
    /news/news-details/<year>/<slug>[/default.aspx] on any Q4 IR hostname;
    no literal "/news/" prefix is assumed, since some Q4 themes nest their
    news-details links elsewhere (e.g. Travelers uses
    "/newsroom/press-releases/news-details/...").
    """
    escaped = re.escape(news_details_segment or DEFAULT_NEWS_DETAILS_SEGMENT)
    return re.compile(rf"/{escaped}/\d{{4}}/[^/]+/?(?:default\.aspx)?", re.IGNORECASE)


def q4_news_link_selector(news_details_segment: str = "") -> str:
    """Build the CSS selector counterpart to q4_news_link_re(), for use with
    Playwright/BeautifulSoup element selection.
    """
    segment = news_details_segment or DEFAULT_NEWS_DETAILS_SEGMENT
    return f"a[href*='/{segment}/']"


# Generic, segment-agnostic sniff pattern -- used only to *derive* an
# unspecified source's news_details_segment from its own rendered listing
# page, never to identify actual press-release items for scraping (that's
# what q4_news_link_re(), built from the *specific* derived/configured
# segment, is for). Matches the same overall shape --
# /<segment>-details/<year>/<slug>... -- but leaves the segment itself
# wide open (any run of lowercase word-chars ending in "-details") since
# discovering that name is the whole point here.
_DETAILS_SEGMENT_SNIFF_RE = re.compile(
    r"/([a-z0-9]+(?:-[a-z0-9]+)*-details)/\d{4}/[^/\"'?#]+", re.IGNORECASE
)

# Broad CSS selector used only while a source's news_details_segment is
# still unknown, so the listing page can be rendered/waited-on at all before
# the precise per-source selector (q4_news_link_selector()) can be built.
# Matches any "...-details/" path segment -- deliberately looser than the
# final selector, which is why it's never used for actual item parsing.
GENERIC_NEWS_DETAILS_SELECTOR = "a[href*='-details/']"


def derive_news_details_segment(html: str) -> Optional[str]:
    """Best-effort: figure out a Q4 source's news_details_segment by
    inspecting its own rendered listing-page markup, instead of assuming
    the Costco/CDW default "news-details" for every source.

    Scans every href in *html* for the shape
    .../<segment>-details/<year>/<slug>... (the same shape q4_news_link_re()
    matches for a *known* segment) via _DETAILS_SEGMENT_SNIFF_RE above, and
    returns the most common captured segment -- most listing pages only ever
    show one, but taking a mode guards against an occasional unrelated
    "-details" link elsewhere on the page (e.g. an event or webcast link)
    outvoting the real one. Returns None if no such link is found anywhere,
    in which case the caller should fall back to DEFAULT_NEWS_DETAILS_SEGMENT.

    Used by scrape_q4_ir.py's render_news_page() when a source has no
    "news_details_segment" field in sources.yaml and none was passed via
    --news-details-segment: rather than blindly assuming "news-details"
    (which silently returns zero items for a theme like Netflix's, whose
    real segment is "press-release-details"), the listing page is first
    rendered with the broad GENERIC_NEWS_DETAILS_SELECTOR above, and this
    function then inspects what actually showed up.
    """
    matches = _DETAILS_SEGMENT_SNIFF_RE.findall(html)
    if not matches:
        return None
    segment, _count = Counter(matches).most_common(1)[0]
    return segment


def strip_year_placeholder(path: str) -> str:
    """Drop a "{year}/" (or bare "{year}") placeholder segment from *path*.

    Used wherever a "{year}"-templated news_path needs to become a real,
    year-agnostic path: scrape_q4_ir.py's _resolve_year_url() when no
    concrete year is requested, and detect_ir_platform.py's
    _join_news_path()/_check_q4(), which only need *some* listing page to
    check for platform fingerprints, not a particular year.
    """
    return path.replace("{year}/", "").replace("{year}", "")