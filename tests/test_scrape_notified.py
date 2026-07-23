"""
tests/test_scrape_notified.py

Covers scrape_notified.parse_listing_page() -- a regression found on GE
Vernova's IR site (https://www.gevernova.com/news/media-hub?tag=Investor%20Relations).

Background: running

    python src/scrape_notified.py --dry-run --year 2026 --slug ge-vernova \
        --news-releases-path "news/media-hub?tag=Investor%20Relations"

found 0 items even though the listing page clearly had press releases on it.

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

from scrape_notified import parse_listing_page  # noqa: E402

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