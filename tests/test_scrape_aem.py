"""
tests/test_scrape_aem.py

Covers scrape_aem.parse_listing_page() -- the listing-page card parser
every AEM-powered source (currently BNY and CME Group) goes through -- plus
date_from_url()'s URL-embedded-date fallback.

BNY_LISTING_PAGE below is trimmed down from a real --debug-dump-html
capture:

    python src/scrape_aem.py --dry-run --year 2026 --slug bny \
        --debug-dump-html bny_2026.html

keeping only three of the ``.list-item-tile`` cards it produced (dropping
the page's header/nav/footer chrome, which parse_listing_page() never
looks at), plus the pagination widget markup that follows them. Those
three cards are the same three items that run's own preview printed:

    2026-01-13  BNY Declares Dividends
    2026-01-13  BNY Reports Fourth Quarter 2025 Results
    2026-01-22  BNY to Speak at the BofA Securities Financial Services Conference

CME_LISTING_PAGE below is likewise trimmed down from a real capture:

    python src/scrape_aem.py --dry-run --year 2026 --slug cme \
        --debug-dump-html cme_2026.html

keeping two of the ``#cmeSearchFilterResults > li`` cards it produced (see
the module docstring's "Page structure (CME Group)" section), plus one
nav-menu link ("Speeches and Comment Letters") from the same page that must
not be mistaken for a press release, and CME's own "bootpag" pagination
widget markup.

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scrape_aem import date_from_url, parse_listing_page  # noqa: E402

BASE_URL = "https://www.bny.com/corporate/global/en/investor-relations/press-releases.html"

# Trimmed from a real --debug-dump-html capture -- see module docstring.
# Each card is BNY's bespoke ".list-item-tile" widget: a <time> dateline
# plus a ".title" div wrapping the headline anchor.
BNY_LISTING_PAGE = """
<div id="page-list">
    <div class="list-item-tile">
        <div class="list-item-content">
            <div class="list-item-header">
                <time class="list-item-header__date"> January 22, 2026</time>
            </div>
            <div class="title">
                <a href="/content/bnymellon/global/en/about-us/newsroom/press-release/bny-to-speak-at-the-bofa-securities-financial-services-conference-130457.html">
                    <h3>BNY to Speak at the BofA Securities Financial Services Conference</h3>
                </a>
            </div>
        </div>
    </div>
    <div class="list-item-tile">
        <div class="list-item-content">
            <div class="list-item-header">
                <time class="list-item-header__date"> January 13, 2026</time>
            </div>
            <div class="title">
                <a href="/content/bnymellon/global/en/about-us/newsroom/press-release/bny-declares-dividends-130456.html">
                    <h3>BNY Declares Dividends</h3>
                </a>
            </div>
        </div>
    </div>
    <div class="list-item-tile">
        <div class="list-item-content">
            <div class="list-item-header">
                <time class="list-item-header__date"> January 13, 2026</time>
            </div>
            <div class="title">
                <a href="/content/bnymellon/global/en/about-us/newsroom/press-release/bny-reports-fourth-quarter-2025-results-130455.html">
                    <h3>BNY Reports Fourth Quarter 2025 Results</h3>
                </a>
            </div>
        </div>
    </div>
</div>
<div class="list-pagination">
    <ul class="pagination">
        <li>
            <label onclick="showDataOnPagination(1,this)" class=" search-pagination--keyboard_arrow_left"></label>
        </li>
    </ul>
</div>
"""


def test_parses_all_three_cards_in_listing_order():
    items = parse_listing_page(BNY_LISTING_PAGE, base_url=BASE_URL, slug="bny", ticker="BNY")
    assert [i.title for i in items] == [
        "BNY to Speak at the BofA Securities Financial Services Conference",
        "BNY Declares Dividends",
        "BNY Reports Fourth Quarter 2025 Results",
    ]


def test_extracts_date_from_time_tag_display_text():
    items = parse_listing_page(BNY_LISTING_PAGE, base_url=BASE_URL, slug="bny", ticker="BNY")
    by_title = {i.title: i for i in items}
    assert by_title["BNY Declares Dividends"].publish_date == date(2026, 1, 13)
    assert by_title["BNY Reports Fourth Quarter 2025 Results"].publish_date == date(2026, 1, 13)
    assert by_title["BNY to Speak at the BofA Securities Financial Services Conference"].publish_date == date(2026, 1, 22)


def test_resolves_relative_hrefs_to_absolute_urls():
    items = parse_listing_page(BNY_LISTING_PAGE, base_url=BASE_URL, slug="bny", ticker="BNY")
    dividends = next(i for i in items if i.title == "BNY Declares Dividends")
    assert dividends.url == (
        "https://www.bny.com/content/bnymellon/global/en/about-us/newsroom/"
        "press-release/bny-declares-dividends-130456.html"
    )


def test_pagination_widget_markup_is_not_mistaken_for_an_item():
    # The pagination <label> at the bottom of the fixture is not an
    # ".list-item-tile" card and has no headline-length title, so it must
    # not show up as a fourth item.
    items = parse_listing_page(BNY_LISTING_PAGE, base_url=BASE_URL, slug="bny", ticker="BNY")
    assert len(items) == 3


# ---------------------------------------------------------------------------
# CME Group -- see module docstring's "Page structure (CME Group)" section.
#
# Unlike BNY, CME's cards are matched directly by ITEM_SELECTOR_CASCADE's
# "#cmeSearchFilterResults > li" entry, not the same-host heuristic
# fallback -- this fixture exercises that direct-match path, plus CME's
# day-first ".cmeBrowseAllDate" dateline and its "bootpag" pagination
# widget's "Next ›" control.
# ---------------------------------------------------------------------------

CME_BASE_URL = "https://www.cmegroup.com/media-room/press-releases.html"

CME_LISTING_PAGE = """
<nav>
<a href="/media-room/speeches-and-comment-letters.html">Speeches and Comment Letters</a>
</nav>
<ul data-type="ul" id="cmeSearchFilterResults" class="cmeResultListing cmeClearContent cmeFloatingHead">
<li><div class="vcard column"><div class="vcard content"><div class="cmeBrowseAllLeft">
<p class="cmeBrowseAllTitle"><a href="/content/cmegroup/en/media-room/press-releases/2026/8/06/cme_group_latin_americanfxfuturesandoptionshitnewrecordsinh12026.html">CME Group Latin American FX Futures and Options Hit New Records in H1 2026</a></p>
<p class="cmeBrowseAllDate">6 August, 2026</p>
</div></div></div></li>
<li><div class="vcard column"><div class="vcard content"><div class="cmeBrowseAllLeft">
<p class="cmeBrowseAllTitle"><a href="/content/cmegroup/en/media-room/press-releases/2026/8/06/cme_group_declaresquarterlydividend.html">CME Group Declares Quarterly Dividend</a></p>
<p class="cmeBrowseAllDate">6 August, 2026</p>
</div></div></div></li>
</ul>
<div class="cmePaginationWrapper">
<ul class="pagination bootpag">
<li data-lp="1" class="first disabled"><a href="javascript:void(0);">«</a></li>
<li data-lp="1" class="prev disabled"><a href="javascript:void(0);">‹ Prev</a></li>
<li data-lp="1" class="active"><a href="javascript:void(0);">1</a></li>
<li data-lp="2" class="next"><a href="javascript:void(0);">Next ›</a></li>
<li data-lp="130" class="last"><a href="javascript:void(0);">»</a></li>
</ul>
</div>
"""


def test_cme_cards_matched_directly_by_item_selector_cascade():
    items = parse_listing_page(CME_LISTING_PAGE, base_url=CME_BASE_URL, slug="cme", ticker="CME")
    assert [i.title for i in items] == [
        "CME Group Latin American FX Futures and Options Hit New Records in H1 2026",
        "CME Group Declares Quarterly Dividend",
    ]


def test_cme_day_first_dateline_is_parsed():
    items = parse_listing_page(CME_LISTING_PAGE, base_url=CME_BASE_URL, slug="cme", ticker="CME")
    assert all(i.publish_date == date(2026, 8, 6) for i in items)


def test_cme_nav_link_and_pagination_widget_are_not_mistaken_for_items():
    # "Speeches and Comment Letters" sits outside #cmeSearchFilterResults
    # entirely, as does the bootpag pagination widget's own <li>/<a>
    # elements -- neither should show up as a third item.
    items = parse_listing_page(CME_LISTING_PAGE, base_url=CME_BASE_URL, slug="cme", ticker="CME")
    assert len(items) == 2
    assert not any("speeches-and-comment-letters" in i.url for i in items)


# ---------------------------------------------------------------------------
# date_from_url() -- the last-resort URL-embedded-date fallback (see
# resolve_publish_date()). CME's own detail-page URLs don't zero-pad the
# month segment (".../2026/8/06/some-title.html", not ".../2026/08/06/..."),
# which the path regex must tolerate even though CME's listing-page cards
# are normally dated directly via ".cmeBrowseAllDate" and never need this
# fallback in practice.
# ---------------------------------------------------------------------------

def test_date_from_url_handles_cme_style_unpadded_month():
    url = (
        "https://www.cmegroup.com/content/cmegroup/en/media-room/press-releases/"
        "2026/8/06/cme_group_declaresquarterlydividend.html"
    )
    assert date_from_url(url) == date(2026, 8, 6)


def test_date_from_url_still_handles_zero_padded_path_segments():
    url = "https://example.com/content/foo/2026/01/15/some-title.html"
    assert date_from_url(url) == date(2026, 1, 15)


def test_date_from_url_still_handles_hyphenated_slug_dates():
    url = "https://example.com/news/2026-01-15-some-title.html"
    assert date_from_url(url) == date(2026, 1, 15)