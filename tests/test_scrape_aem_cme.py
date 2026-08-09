"""
tests/test_scrape_aem_cme.py

Covers scrape_aem_cme.parse_listing_page() -- the listing-page card parser
for CME Group's press-release site -- plus date_from_url()'s
CME-specific unpadded-month URL fallback.

Split out from the former tests/test_scrape_aem.py, which covered both BNY
and CME Group against the single shared scrape_aem.py. See
scrape_aem_cme.py's module docstring for why they're now separate scraper
modules (and therefore separate test files) despite both being "aem"
platform sites.

CME_LISTING_PAGE below is trimmed down from a real --debug-dump-html
capture:

    python src/scrape_aem_cme.py --dry-run --year 2026 --slug cme \
        --debug-dump-html cme_2026.html

keeping two of the ``#cmeSearchFilterResults > li`` cards it produced (see
scrape_aem_cme.py's module docstring's "Page structure" section), plus one
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

from scrape_aem_cme import date_from_url, parse_listing_page  # noqa: E402

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


def test_cme_cards_matched_directly_by_item_selector():
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