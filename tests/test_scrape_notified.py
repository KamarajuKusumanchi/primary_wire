"""
tests/test_scrape_notified.py

Covers scrape_notified.parse_listing_page() -- a regression found on GE
Vernova's IR site (https://www.gevernova.com/news/media-hub?tag=Investor%20Relations).

Background: running

    python src/scrape_notified.py --dry-run --year 2026 --slug ge-vernova \
        --news-releases-path "news/media-hub?tag=Investor%20Relations"

found 0 items even though the listing page clearly had press releases on it.

sources.yaml's news_releases_path for ge-vernova has since been narrowed to
"news/media-hub?tag=Investor+Relations&type[press_release]=press_release"
(adds a type filter so the listing is press releases only, instead of
everything tagged "Investor Relations"). Re-verified against a fresh
--debug-dump-html capture of that URL: parse_listing_page() and
listing_page_url()'s query-param merge (see its docstring) both handle the
extra param with no code changes -- the merge logic was already written to
preserve however many pre-existing params there are, not just one. See
test_doubled_news_segment_in_href_is_still_recognized() below for one
real-world quirk that capture surfaced and is now pinned down as a
regression guard.

Root cause #1 (0 items found): DETAIL_URL_RE required *two* path segments
after the news-releases/press-releases/financial-releases keyword (matching
AbbVie's /news-releases/news-release-details/<slug>), but GE Vernova's detail
pages are only one segment deep: /news/press-releases/<slug>. Every detail
link on the page failed is_detail_url(), so nothing was ever recognized as a
press release.

Root cause #2 (found while fixing #1, would otherwise have silently
corrupted every row once #1 was fixed): GE Vernova wraps an entire card --
read-time label, date, headline, and summary -- inside a *single* <a>, unlike
sites where the anchor is just the headline. This broke two things that
assume the anchor's own text is just the headline:
  - Title: anchor.get_text() picked up the read-time label, date, headline,
    and summary all concatenated together instead of just the headline.
  - Date: extract_date_and_time_from_row()'s ancestor-climbing strategies
    strip the anchor's own text out of surrounding text before searching it
    for a date (to avoid matching a date mentioned inside the headline
    itself) -- but here the date lives *inside* that same anchor text, so it
    got stripped away too, and the search fell through to a shared ancestor
    and picked up an unrelated sibling card's date instead. In practice every
    item silently inherited the newest release's date.

Fix: DETAIL_URL_RE's second path segment is now optional (see its own
docstring), a heading tag nested inside the anchor is now preferred over the
full anchor text for the title, and a <time datetime="..."> tag nested
inside the anchor is now read directly (Strategy 0) before any of the
text-scanning/stripping strategies run.

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scrape_notified import is_detail_url, parse_listing_page  # noqa: E402

BASE_URL = "https://www.gevernova.com"

# ---------------------------------------------------------------------------
# Trimmed down from the actual --debug-dump-html capture of
# https://www.gevernova.com/news/media-hub?tag=Investor%20Relations, keeping
# only the structural details that matter: two "card-wrapper" anchors, each
# wrapping its own read-time/date/headline/summary together, with single-
# segment /news/press-releases/<slug> detail URLs (no news-release-details/
# middle segment the way AbbVie's site has).
# ---------------------------------------------------------------------------

GE_VERNOVA_PAGE = """
<div class="pr-listing">
  <div class="pr-content-card pr-container">
    <a class="card-wrapper" href="https://www.gevernova.com/news/press-releases/ge-vernova-reports-second-quarter-2026-financial-results-raises-2026-financial">
      <div class="flex-column">
        <p class="read-time">Press Release</p>
        <div class="v-line">|</div>
        <p class="read-time">10 min read</p>
        <p class="eyebrow-text"><time datetime="2026-07-22T06:00:00-04:00" title="July 22 2026">July 22, 2026</time></p>
        <h5 class="card-title">GE Vernova reports second quarter 2026 financial results and raises 2026 financial guidance</h5>
        <div class="info-text"><p>CAMBRIDGE, Mass., (July 22, 2026) - GE Vernova Inc. (NYSE: GEV) today reported financial results.</p></div>
      </div>
    </a>
  </div>
  <div class="pr-content-card pr-container">
    <a class="card-wrapper" href="https://www.gevernova.com/news/press-releases/hawaiian-electric-order-ge-vernova-aeroderivative-turbine-hawaii-power">
      <div class="flex-column">
        <p class="read-time">Press Release</p>
        <div class="v-line">|</div>
        <p class="read-time">4 min read</p>
        <p class="eyebrow-text"><time datetime="2026-07-20T03:50:17-04:00" title="July 20 2026">July 20, 2026</time></p>
        <h5 class="card-title">Hawaiian Electric orders GE Vernova aeroderivative packages to help Hawaii's power remain stable and uninterrupted</h5>
        <div class="info-text"><p>HONOLULU, (July 20, 2026) - GE Vernova Inc. (NYSE: GEV) today announced a contract with Hawaiian Electric.</p></div>
      </div>
    </a>
  </div>
</div>
"""


def _items_by_title(html: str) -> dict:
    items = parse_listing_page(html, BASE_URL, slug="ge-vernova", ticker="GEV")
    return {item.title: item for item in items}


def test_single_segment_detail_urls_are_recognized():
    """End-to-end sanity check matching the originally reported symptom:
    both cards must be found, not silently skipped as non-detail links just
    because their URLs are one path segment shorter than AbbVie's."""
    by_title = _items_by_title(GE_VERNOVA_PAGE)
    assert len(by_title) == 2


def test_title_is_just_the_headline_not_the_whole_card():
    """The anchor wraps the read-time label, date, headline, and summary
    together; the title must be just the <h5 class="card-title"> text, not
    all of that concatenated."""
    by_title = _items_by_title(GE_VERNOVA_PAGE)
    assert "Press Release" not in by_title
    assert any(
        title.startswith("GE Vernova reports second quarter 2026")
        for title in by_title
    )
    title = next(t for t in by_title if t.startswith("GE Vernova reports"))
    assert "min read" not in title
    assert "CAMBRIDGE" not in title


def test_each_card_gets_its_own_date_not_a_sibling_s():
    """Regression guard for the date-bleed bug: each card must be dated from
    its own <time datetime> attribute, not the first (or any other) sibling
    card's date."""
    by_title = _items_by_title(GE_VERNOVA_PAGE)

    q2_title = next(t for t in by_title if t.startswith("GE Vernova reports"))
    hawaiian_title = next(t for t in by_title if t.startswith("Hawaiian Electric"))

    assert by_title[q2_title].publish_date == date(2026, 7, 22)
    assert by_title[hawaiian_title].publish_date == date(2026, 7, 20)


# ---------------------------------------------------------------------------
# Regression: the site's "News RSS" feed-subscribe link mistaken for a
# press release (found on AMD, Global Payments, and FedEx Freight's IR
# sites, all of which use this same Q4-style listing markup).
#
# Background: running
#
#   python src/scrape_notified.py --dry-run --slug fedex-freight --year 2026
#
# produced a bogus extra item:
#
#   2026-06-25 (4:10 pm EDT)  rss_feed News RSS
#              https://ir.fedexfreight.com/news-events/press-releases/rss
#
# Root cause: the "News RSS" widget below the listing links to
# .../press-releases/rss, which satisfies DETAIL_URL_RE just as well as a
# real detail link like .../press-releases/detail/185/<slug> (same
# "keyword + one segment" shape), so is_detail_url() wrongly said yes to
# it. That link sits outside any <article>/row, so it has no date of its
# own; extract_date_and_time_from_row()'s ancestor-climbing fallback then
# picked up the first (i.e. newest) date it found while walking up toward
# the shared listing container, silently borrowing the most recent real
# release's date. The bogus title was simply the anchor's own text: the
# material-icon glyph name ("rss_feed") plus its label ("News RSS").
#
# Fix: DETAIL_URL_RE now excludes a literal "rss" segment right after the
# keyword (see its own docstring), without touching real slugs that merely
# start with those letters.
# ---------------------------------------------------------------------------

# Trimmed down from the actual --debug-dump-html capture of
# https://ir.fedexfreight.com/news-events/press-releases, keeping the
# structural details that matter: two <article class="media"> releases
# followed by the page-level "News RSS" widget, which is a sibling of the
# listing -- not inside either article -- exactly as on the real page.
FEDEX_FREIGHT_PAGE = """
<article class="media">
    <div class="date"><time datetime="2026-06-25T16:10:00">Jun 25, 2026 4:10 pm EDT</time></div>
    <div class="media-heading">
        <a href="https://ir.fedexfreight.com/news-events/press-releases/detail/185/fedex-freight-reports-fourth-quarter-and-full-fiscal-year-2026-financial-results">
            FedEx Freight Reports Fourth Quarter and Full Fiscal Year 2026 Financial Results
        </a>
    </div>
</article>
<article class="media">
    <div class="date"><time datetime="2026-06-02T16:28:00">Jun 2, 2026 4:28 pm EDT</time></div>
    <div class="media-heading">
        <a href="https://ir.fedexfreight.com/news-events/press-releases/detail/184/fedex-freight-to-report-fourth-quarter-2026-earnings-on-june-25-2026">
            FedEx Freight to Report Fourth Quarter 2026 Earnings on June 25, 2026
        </a>
    </div>
</article>
<div class="clear"></div>
<div class="rss-link">
    <a href="https://ir.fedexfreight.com/news-events/press-releases/rss" class="link--icon" target="_blank" rel="noopener">
        <span class="material-icons" aria-hidden="true">rss_feed</span> News RSS
    </a>
</div>
"""

FEDEX_BASE_URL = "https://ir.fedexfreight.com"


def test_rss_feed_link_is_not_recognized_as_a_detail_url():
    assert not is_detail_url("https://ir.fedexfreight.com/news-events/press-releases/rss")
    # a real slug that happens to start with "rss" must still be recognized
    assert is_detail_url("https://example.com/press-releases/rss-feed-integration-announced")


def test_rss_feed_link_is_excluded_from_parsed_releases():
    items = parse_listing_page(
        FEDEX_FREIGHT_PAGE, FEDEX_BASE_URL, slug="fedex-freight", ticker="FDXF"
    )

    assert len(items) == 2
    assert all(not item.url.rstrip("/").endswith("/rss") for item in items)
    assert all(item.title != "rss_feed News RSS" for item in items)


def test_newest_release_keeps_its_own_date_not_the_rss_link_s():
    items = parse_listing_page(
        FEDEX_FREIGHT_PAGE, FEDEX_BASE_URL, slug="fedex-freight", ticker="FDXF"
    )
    by_title = {item.title.strip(): item for item in items}

    newest = by_title["FedEx Freight Reports Fourth Quarter and Full Fiscal Year 2026 Financial Results"]
    assert newest.publish_date == date(2026, 6, 25)


# ---------------------------------------------------------------------------
# Regression: a doubled "news/news/press-releases/<slug>" href, observed in a
# real --debug-dump-html capture of GE Vernova's press-release-filtered
# listing (one card out of ten had this shape; the rest were the normal
# single "news/press-releases/<slug>"). Presumed a site-side quirk, not
# something primary_wire caused.
#
# DETAIL_URL_RE uses re.search() rather than an anchored match, so it finds
# "/press-releases/<slug>" wherever it sits in the href and doesn't care
# about the doubled "news/news" prefix ahead of it. This test just pins
# that down as a regression guard, in case a future refactor tightens the
# regex (e.g. anchoring it, or asserting the keyword is the first path
# segment) without realizing real sites can have paths like this.
# ---------------------------------------------------------------------------

GE_VERNOVA_DOUBLED_SEGMENT_CARD = """
<div class="pr-content-card pr-container">
  <a class="card-wrapper" href="https://www.gevernova.com/news/news/press-releases/ge-vernova-releases-2025-sustainability-report">
    <div class="flex-column">
      <p class="read-time">Press Release</p>
      <p class="eyebrow-text"><time datetime="2026-06-17T08:35:33-04:00" title="June 17 2026">June 17, 2026</time></p>
      <h5 class="card-title">GE Vernova's New Sustainability Report Highlights Progress</h5>
      <div class="info-text"><p>Summary text.</p></div>
    </div>
  </a>
</div>
"""


def test_doubled_news_segment_in_href_is_still_recognized():
    href = "https://www.gevernova.com/news/news/press-releases/ge-vernova-releases-2025-sustainability-report"
    assert is_detail_url(href)

    items = parse_listing_page(
        GE_VERNOVA_DOUBLED_SEGMENT_CARD, BASE_URL, slug="ge-vernova", ticker="GEV"
    )
    assert len(items) == 1
    assert items[0].url == href
    assert items[0].publish_date == date(2026, 6, 17)
    assert items[0].title == "GE Vernova's New Sustainability Report Highlights Progress"