"""
tests/test_scrape_aem.py

Covers scrape_aem.parse_listing_page() -- the listing-page card parser
every AEM-powered source (currently just BNY) goes through.

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

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scrape_aem import parse_listing_page  # noqa: E402

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