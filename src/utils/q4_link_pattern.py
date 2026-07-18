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


def strip_year_placeholder(path: str) -> str:
    """Drop a "{year}/" (or bare "{year}") placeholder segment from *path*.

    Used wherever a "{year}"-templated news_path needs to become a real,
    year-agnostic path: scrape_q4_ir.py's _resolve_year_url() when no
    concrete year is requested, and detect_ir_platform.py's
    _join_news_path()/_check_q4(), which only need *some* listing page to
    check for platform fingerprints, not a particular year.
    """
    return path.replace("{year}/", "").replace("{year}", "")