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

from scrape_aem_cme import (  # noqa: E402
    _cme_date_filter_url,
    _cme_date_range_ms,
    _navigate_full,
    date_from_url,
    parse_listing_page,
)

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


# ---------------------------------------------------------------------------
# --use-date-filter -- _cme_date_range_ms() / _cme_date_filter_url().
#
# These are the two pieces the previous, untested version of
# --use-date-filter got wrong: it drove the "Refine Your Search" UI (which
# silently no-oped -- see scrape_aem_cme.py's module docstring) instead of
# navigating straight to CME's own hash-routed filtered-listing URL. The
# expected millisecond values below were hand-verified against a real
# CME session for three separate years (2024, 2025, 2026) via
# --debug-dump-html + --show-browser -- see the "Date-range filter"
# section of scrape_aem_cme.py's module docstring.
# ---------------------------------------------------------------------------

def test_cme_date_range_ms_matches_verified_2024_values():
    date_from_ms, date_to_ms = _cme_date_range_ms(2024)
    assert date_from_ms == 1704085200000  # 2024-01-01 00:00:00 America/New_York
    assert date_to_ms == 1735621200000    # 2024-12-31 00:00:00 America/New_York


def test_cme_date_range_ms_matches_verified_2025_values():
    date_from_ms, date_to_ms = _cme_date_range_ms(2025)
    assert date_from_ms == 1735707600000
    assert date_to_ms == 1767157200000


def test_cme_date_range_ms_matches_verified_2026_values():
    date_from_ms, date_to_ms = _cme_date_range_ms(2026)
    assert date_from_ms == 1767243600000
    assert date_to_ms == 1798693200000


def test_cme_date_filter_url_builds_expected_fragment():
    url = _cme_date_filter_url(CME_BASE_URL, 3, 1704085200000, 1735621200000)
    assert url == (
        "https://www.cmegroup.com/media-room/press-releases.html"
        "#pageNum=3&dateFrom=1704085200000&dateTo=1735621200000"
    )


def test_cme_date_filter_url_strips_any_existing_fragment():
    url = _cme_date_filter_url(CME_BASE_URL + "#pageNum=7", 1, 111, 222)
    assert url == CME_BASE_URL + "#pageNum=1&dateFrom=111&dateTo=222"


# ---------------------------------------------------------------------------
# --use-date-filter -- _navigate_full().
#
# This is the fix for the *second* bug in --use-date-filter: even once the
# fragment itself (dateFrom/dateTo/pageNum) was correct, a plain
# page.goto() from an already-loaded page to a URL differing only by that
# fragment turned out to be a same-document navigation in Chromium/CDP --
# it doesn't re-run the page's bootstrap JS, so CME's listing never
# actually re-read the new hash. A live run showed exactly this: the
# correct filtered URL logged, immediately followed by "Item list did not
# change." These tests use a minimal fake Page (no real browser) to pin
# down exactly which Playwright calls _navigate_full() makes in each case,
# since that distinction is the entire bug.
# ---------------------------------------------------------------------------

class _FakePage:
    """Minimal stand-in for playwright.sync_api.Page, just enough to
    observe which navigation strategy _navigate_full() picks."""

    def __init__(self, current_url: str):
        self.url = current_url
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[tuple[str, str]] = []
        self.reload_calls: int = 0

    def goto(self, url, wait_until=None, timeout=None):  # noqa: ANN001
        self.goto_calls.append(url)
        self.url = url

    def evaluate(self, script, arg=None):  # noqa: ANN001
        self.evaluate_calls.append((script, arg))
        base = self.url.split("#", 1)[0]
        self.url = f"{base}#{arg}" if arg else base

    def reload(self, wait_until=None, timeout=None):  # noqa: ANN001
        self.reload_calls += 1


def test_navigate_full_uses_plain_goto_for_a_different_base_url():
    page = _FakePage("about:blank")
    target = CME_BASE_URL + "#pageNum=1&dateFrom=1&dateTo=2"
    _navigate_full(page, target, timeout_ms=5000)
    assert page.goto_calls == [target]
    assert page.evaluate_calls == []
    assert page.reload_calls == 0


def test_navigate_full_forces_reload_when_only_the_fragment_differs():
    # The exact scenario that broke live: page already loaded the bare
    # (hash-less) listing URL, then we try to apply the date filter,
    # which only changes the fragment.
    page = _FakePage(CME_BASE_URL)
    target = _cme_date_filter_url(CME_BASE_URL, 1, 1704085200000, 1735621200000)
    _navigate_full(page, target, timeout_ms=5000)
    # A same-document goto() would silently no-op -- must NOT be used.
    assert page.goto_calls == []
    assert page.evaluate_calls == [
        (
            "h => { window.location.hash = h; }",
            "pageNum=1&dateFrom=1704085200000&dateTo=1735621200000",
        )
    ]
    assert page.reload_calls == 1


def test_navigate_full_forces_reload_when_only_the_pagenum_changes():
    # Same base + same dateFrom/dateTo, just the next page's pageNum --
    # this is the ordinary within-filter pagination case and must also
    # go through a real reload, not a same-document goto().
    page = _FakePage(_cme_date_filter_url(CME_BASE_URL, 1, 100, 200))
    target = _cme_date_filter_url(CME_BASE_URL, 2, 100, 200)
    _navigate_full(page, target, timeout_ms=5000)
    assert page.goto_calls == []
    assert page.evaluate_calls == [
        ("h => { window.location.hash = h; }", "pageNum=2&dateFrom=100&dateTo=200")
    ]
    assert page.reload_calls == 1