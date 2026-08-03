"""
tests/test_news_details_segment_derivation.py

Covers the enhancement to scrape_q4_ir.py's news_details_segment handling:

  1. If sources.yaml (or --news-details-segment) specifies a segment, it's
     used as-is -- unchanged from before.
  2. If it's NOT specified, the segment is now derived from the source's own
     rendered listing-page markup (q4_link_pattern.derive_news_details_segment())
     instead of blindly assuming the Costco/CDW default "news-details". Only
     when that derivation finds nothing does it fall back to
     DEFAULT_NEWS_DETAILS_SEGMENT ("news-details"), same as the old
     unconditional behavior.

Background: sources.yaml currently hardcodes "news_details_segment:
press-release-details" for netflix and quest-diagnostics because their Q4
theme's detail links don't use the "news-details" segment Costco/CDW's does.
Every other Q4 source in sources.yaml (Costco included) relies on the
implicit "news-details" default. Before this change, any *future* source
whose theme also uses a non-default segment would silently scrape zero
items until someone noticed and hand-added the field -- this derivation
step is meant to catch that automatically instead.

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils.q4_link_pattern import (  # noqa: E402
    DEFAULT_NEWS_DETAILS_SEGMENT,
    derive_news_details_segment,
)
from scrape_q4_ir import resolve_source  # noqa: E402


# ---------------------------------------------------------------------------
# derive_news_details_segment(): unit-level, synthetic listing-page fixtures
# ---------------------------------------------------------------------------

def test_derives_default_segment_from_costco_style_markup():
    """Costco's theme uses the "news-details" segment -- confirm it's read
    straight off the markup, not just coincidentally equal to the default."""
    html = """
    <ul>
      <li><a href="/news/news-details/2026/Costco-Wholesale-Corporation-Reports-May-Sales-Results/default.aspx">May Sales</a></li>
      <li><a href="/news/news-details/2026/Costco-Wholesale-Corporation-Announces-Quarterly-Cash-Dividend/default.aspx">Dividend</a></li>
    </ul>
    """
    assert derive_news_details_segment(html) == "news-details"


def test_derives_non_default_segment_from_netflix_style_markup():
    """Netflix's theme uses "press-release-details", not the "news-details"
    default -- this is the exact case that used to require a hand-added
    sources.yaml override before any scraping worked at all."""
    html = """
    <div class="module_item">
      <a href="https://ir.netflix.net/investor-news-and-events/financial-releases/press-release-details/2026/Netflix-to-Announce-First-Quarter-2026-Financial-Results/default.aspx">Q1 Results</a>
    </div>
    <div class="module_item">
      <a href="https://ir.netflix.net/investor-news-and-events/financial-releases/press-release-details/2026/Netflix-Declines-to-Raise-Offer-for-Warner-Bros-/default.aspx">Warner Bros</a>
    </div>
    """
    assert derive_news_details_segment(html) == "press-release-details"


def test_derivation_ignores_unrelated_details_links_via_majority_vote():
    """A page might have one unrelated "-details" link (e.g. an event or
    webcast page) alongside the real press-release links. The real segment
    -- the one actually used by multiple press-release cards -- should win
    a plain majority vote rather than being confused by a single outlier."""
    html = """
    <a href="/events/event-details/2026/Q1-Earnings-Call/default.aspx">Earnings Call</a>
    <a href="/news/news-details/2026/First-Release/default.aspx">First</a>
    <a href="/news/news-details/2026/Second-Release/default.aspx">Second</a>
    <a href="/news/news-details/2026/Third-Release/default.aspx">Third</a>
    """
    assert derive_news_details_segment(html) == "news-details"


def test_derivation_returns_none_when_no_details_link_present():
    """A page with no Q4-shaped detail link at all (e.g. it failed to
    render, or this isn't actually a Q4 theme) can't be derived from --
    the caller is expected to fall back to DEFAULT_NEWS_DETAILS_SEGMENT."""
    html = "<html><body><p>Nothing relevant here.</p></body></html>"
    assert derive_news_details_segment(html) is None


def test_default_segment_constant_used_as_final_fallback():
    """Sanity check that the documented final-fallback value is what
    scrape_q4_ir.py's render_news_page() actually falls back to when
    derivation fails -- guards against the constant drifting independently
    of the derivation behavior it backstops."""
    assert DEFAULT_NEWS_DETAILS_SEGMENT == "news-details"


# ---------------------------------------------------------------------------
# resolve_source(): sources.yaml precedence, against the real sources.yaml
# ---------------------------------------------------------------------------
# These exercise requirement (1) from the enhancement request directly:
# "if news_details_segment is specified in sources.yaml, just use that."
# netflix has an explicit override; costco does not (and must not be forced
# to DEFAULT_NEWS_DETAILS_SEGMENT here anymore -- see the module docstring).

def test_resolve_source_uses_sources_yaml_override_when_present():
    _url, slug, _ticker, _fetch, news_details_segment = resolve_source(
        url=None, slug="netflix", ticker=None,
    )
    assert slug == "netflix"
    assert news_details_segment == "press-release-details"


def test_resolve_source_leaves_segment_unresolved_when_not_in_sources_yaml():
    """Costco has no news_details_segment field in sources.yaml. Previously
    resolve_source() would eagerly default this to "news-details" itself;
    now it should come back as None, deferring the decision (configured
    value vs. derived vs. default fallback) to render_news_page()."""
    _url, slug, _ticker, _fetch, news_details_segment = resolve_source(
        url=None, slug="costco", ticker=None,
    )
    assert slug == "costco"
    assert news_details_segment is None


def test_resolve_source_cli_arg_overrides_sources_yaml():
    """--news-details-segment (the news_details_segment argument here) beats
    sources.yaml even when sources.yaml has its own field set."""
    _url, slug, _ticker, _fetch, news_details_segment = resolve_source(
        url=None, slug="netflix", ticker=None,
        news_details_segment="custom-details-segment",
    )
    assert slug == "netflix"
    assert news_details_segment == "custom-details-segment"