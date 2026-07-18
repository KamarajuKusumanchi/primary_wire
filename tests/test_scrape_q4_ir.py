"""
tests/test_scrape_q4_ir.py

Covers scrape_q4_ir.parse_news_items() -- the listing-page date/category
extraction that every Q4-powered source (Costco, CDW, and everything else in
sources.yaml) goes through before any detail-page fallback is considered.

Background: CDW's theme was previously marked `needs_detail_page_dates: true`
in sources.yaml because parse_news_items() came back with publish_date=None
for every item, forcing a slow, individual detail-page fetch per release.
Investigation (via --debug-dump-html against the live listing page) found
two things:

  1. CDW's news anchor carries an accessible `aria-label` that already
     includes the date, e.g. "CDW Reports First Quarter 2026 Earnings,
     May 6, 2026" -- no DOM climbing required at all.
  2. Independently, the ancestor-walk had a real bug: CDW's card renders
     *two* links to the same article (the headline link, plus a separate
     "Continue Reading" link), and the walk's "stop if this ancestor
     contains more than one news link" heuristic -- meant to detect
     climbing into a shared list wrapper holding multiple *different*
     articles -- treated that same-article pair as if it were two sibling
     items, and bailed out one level too early: one level before the
     ancestor that actually held the date text.

Fixing (2) alone was independently sufficient to recover all dates in the
real listing-page dump; (1) is an additional, cheaper/more-robust path added
alongside it since CDW happens to expose it. Both are covered below, along
with the pre-existing multi-item wrapper case that (2)'s fix must not break.

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scrape_q4_ir import parse_news_items, _news_link_matcher  # noqa: E402

BASE_URL = "https://investor.example.com/news/default.aspx"


# ---------------------------------------------------------------------------
# CDW-shaped fixtures: aria-label date + duplicate headline/"Continue
# Reading" links to the same article.
# ---------------------------------------------------------------------------
# Trimmed down from the actual --debug-dump-html capture of
# https://investor.cdw.com/news/default.aspx, keeping only the structural
# details that matter: the aria-label on the headline anchor, a second
# "Continue Reading" anchor pointing at the same href, and the date sitting
# in the text of an ancestor one level further up than the anchor's
# immediate wrapper.

CDW_CARD = """
<div class="evergreen-news-content-list">
  <div class="evergreen-item-container">
    <div class="evergreen-news-item-wrap">
      <div class="evergreen-g evergreen-g--gutter">
        <div class="evergreen-1-1 evergreen-gr-lc-24-24">
          May 6, 2026
          <div class="evergreen-news-headline">
            <a class="evergreen-item-title evergreen-news-headline-link"
               aria-label="CDW Reports First Quarter 2026 Earnings, May 6, 2026"
               href="/news/news-details/2026/CDW-Reports-First-Quarter-2026-Earnings/default.aspx">
              CDW Reports First Quarter 2026 Earnings
            </a>
          </div>
          <a class="evergreen-link evergreen-news-link"
             aria-label="Continue Reading - CDW Reports First Quarter 2026 Earnings, May 6, 2026"
             href="/news/news-details/2026/CDW-Reports-First-Quarter-2026-Earnings/default.aspx">
            Continue Reading
          </a>
        </div>
      </div>
    </div>
  </div>
</div>
"""

# Same structural bug (duplicate same-article links) but with a title that
# itself contains a comma before the trailing date, and no full date pattern
# elsewhere in the headline -- guards against a naive "split on first/last
# comma" fix that would mis-extract "Media & Communications Conference, May 5"
# instead of "May 5, 2026".
CDW_CARD_COMMA_IN_TITLE = """
<div class="evergreen-news-content-list">
  <div class="evergreen-item-container">
    <div class="evergreen-news-item-wrap">
      <div class="evergreen-g evergreen-g--gutter">
        <div class="evergreen-1-1 evergreen-gr-lc-24-24">
          May 5, 2026
          <div class="evergreen-news-headline">
            <a class="evergreen-item-title evergreen-news-headline-link"
               aria-label="CDW to Participate in the J.P. Morgan 2026 Global Technology, Media &amp; Communications Conference, May 5, 2026"
               href="/news/news-details/2026/CDW-to-Participate-JPM-Conference/default.aspx">
              CDW to Participate in the J.P. Morgan 2026 Global Technology, Media &amp; Communications Conference
            </a>
          </div>
          <a class="evergreen-link evergreen-news-link"
             aria-label="Continue Reading - CDW to Participate in the J.P. Morgan 2026 Global Technology, Media &amp; Communications Conference, May 5, 2026"
             href="/news/news-details/2026/CDW-to-Participate-JPM-Conference/default.aspx">
            Continue Reading
          </a>
        </div>
      </div>
    </div>
  </div>
</div>
"""


def test_cdw_style_card_gets_date_from_aria_label():
    """aria-label alone should resolve the date, with no ancestor climb needed."""
    items = parse_news_items(CDW_CARD, BASE_URL, slug="cdw", ticker="CDW")
    assert len(items) == 1
    item = items[0]
    assert item.publish_date == date(2026, 5, 6)
    assert item.raw_date_text == "May 6, 2026"
    assert item.title == "CDW Reports First Quarter 2026 Earnings"
    assert item.url.endswith("/CDW-Reports-First-Quarter-2026-Earnings/default.aspx")


def test_cdw_style_card_does_not_get_confused_by_comma_in_title():
    """The date must be the trailing 'Month Day, Year', not a mangled split
    on an internal comma in the headline text."""
    items = parse_news_items(CDW_CARD_COMMA_IN_TITLE, BASE_URL, slug="cdw", ticker="CDW")
    assert len(items) == 1
    assert items[0].publish_date == date(2026, 5, 5)
    assert items[0].raw_date_text == "May 5, 2026"


def test_duplicate_same_article_links_do_not_block_ancestor_date_climb():
    """Even without any aria-label at all, a headline link + a second
    "Continue Reading" link to the *same* article must not be mistaken for
    two different sibling items -- the ancestor climb should still reach the
    date text one level up."""
    html = """
    <div class="evergreen-news-item-wrap">
      <div class="evergreen-g">
        May 6, 2026
        <a href="/news/news-details/2026/Some-Release/default.aspx">Some Release</a>
        <a href="/news/news-details/2026/Some-Release/default.aspx">Continue Reading</a>
      </div>
    </div>
    """
    items = parse_news_items(html, BASE_URL, slug="cdw", ticker="CDW")
    assert len(items) == 1
    assert items[0].publish_date == date(2026, 5, 6)


def test_multiple_different_sibling_items_still_stop_the_climb():
    """The original purpose of the '>1 news link' heuristic must still hold:
    once an ancestor holds links to two genuinely *different* articles, the
    climb should stop there rather than picking up a neighboring item's date."""
    html = """
    <div class="wrapper-with-two-different-articles">
      <div class="card">
        May 1, 2026
        <a href="/news/news-details/2026/First-Release/default.aspx">First Release</a>
      </div>
      <div class="card">
        <a href="/news/news-details/2026/Second-Release/default.aspx">Second Release</a>
      </div>
    </div>
    """
    items = parse_news_items(html, BASE_URL, slug="cdw", ticker="CDW")
    assert len(items) == 2
    by_title = {i.title: i for i in items}
    assert by_title["First Release"].publish_date == date(2026, 5, 1)
    # Second Release's own card has no date, and the climb must stop at the
    # shared wrapper (which holds both articles) rather than picking up
    # First Release's date.
    assert by_title["Second Release"].publish_date is None


# ---------------------------------------------------------------------------
# Seagate-shaped fixture: regression test for a real bug seen running
#     python src/scrape_q4_ir.py --slug seagate --year 2026 --show-browser --dry-run
#
# Confirmed against a real --debug-dump-html capture of
# https://investors.seagate.com/news/default.aspx. Seagate's aria-label
# follows the same "<title>, Month Day, Year" trailing-date convention as
# CDW, but its headline text itself embeds a full, unrelated date ("Report
# Fiscal Second Quarter 2026 Financial Results on January 27, 2026" -- the
# date of the *future* earnings call, not this release's publish date). The
# real publish date ("January 13, 2026") is the trailing one appended after
# the final comma. The old aria-label parsing took parse_date()'s first
# match in the whole label and got "January 27, 2026" -- two weeks off.
# ---------------------------------------------------------------------------

SEAGATE_CARD = """
<div class="evergreen-news-item-wrap">
    <div class="evergreen-g evergreen-g--gutter">
        <div class="evergreen-1-1 evergreen-gr-lc-24-24 evergreen-gr-sm-1-1">
            <div class="evergreen-item-date-time evergreen-news-date">
                January 13, 2026
            </div>
            <div class="evergreen-news-headline">
                <a class="evergreen-item-title evergreen-news-link evergreen-news-headline-link"
                   href="/news/news-details/2026/Seagate-Technology-to-Report-Fiscal-Second-Quarter-2026-Financial-Results-on-January-27-2026/default.aspx"
                   aria-label="Seagate Technology to Report Fiscal Second Quarter 2026 Financial Results on January 27, 2026, January 13, 2026">
                    Seagate Technology to Report Fiscal Second Quarter 2026 Financial Results on January 27, 2026
                </a>
            </div>
            <div class="evergreen-news-read-more-container evergreen-hidden">
                <a class="evergreen-link evergreen-news-link"
                   href="/news/news-details/2026/Seagate-Technology-to-Report-Fiscal-Second-Quarter-2026-Financial-Results-on-January-27-2026/default.aspx"
                   aria-label="Read More - Seagate Technology to Report Fiscal Second Quarter 2026 Financial Results on January 27, 2026, January 13, 2026">
                    Read More
                </a>
            </div>
        </div>
    </div>
</div>
"""


def test_seagate_style_aria_label_uses_trailing_date_not_headlines_own_date():
    """The real publish date is the trailing date after the last comma in
    the aria-label, not the unrelated future-earnings-call date the
    headline itself mentions."""
    items = parse_news_items(SEAGATE_CARD, BASE_URL, slug="seagate", ticker="STX")
    assert len(items) == 1
    item = items[0]
    assert item.publish_date == date(2026, 1, 13)
    assert item.raw_date_text == "January 13, 2026"
    assert "January 27, 2026" in item.title  # sanity check: still in headline


# ---------------------------------------------------------------------------
# Costco-shaped fixture: date embedded directly in the ancestor card text,
# no aria-label at all. This is the pre-existing working path and must keep
# working unchanged.
# ---------------------------------------------------------------------------

COSTCO_CARD = """
<ul class="news-list">
  <li class="news-item">
    <span class="date">Jun 18, 2026</span>
    <a href="/news/news-details/2026/Costco-Wholesale-Corporation-Reports-Third-Quarter/default.aspx">
      Costco Wholesale Corporation Reports Third Quarter
    </a>
  </li>
</ul>
"""


def test_costco_style_card_still_gets_date_from_ancestor_text():
    items = parse_news_items(COSTCO_CARD, BASE_URL, slug="costco", ticker="COST")
    assert len(items) == 1
    assert items[0].publish_date == date(2026, 6, 18)
    assert items[0].raw_date_text == "Jun 18, 2026"


def test_category_is_still_found_when_present_near_the_date():
    html = """
    <li class="news-item">
      <span class="date">Feb 4, 2026</span>
      <span class="category">Earnings Releases</span>
      <a href="/news/news-details/2026/Some-Earnings-Release/default.aspx">Some Earnings Release</a>
    </li>
    """
    items = parse_news_items(html, BASE_URL, slug="costco", ticker="COST")
    assert len(items) == 1
    assert items[0].category == "Earnings Releases"
    assert items[0].publish_date == date(2026, 2, 4)


def test_no_matching_links_returns_empty_list():
    assert parse_news_items("<html><body>no news here</body></html>", BASE_URL, "x", "X") == []


# ---------------------------------------------------------------------------
# Netflix-shaped fixture: regression test for a real bug seen running
#     python src/scrape_q4_ir.py --slug netflix --year 2026 --show-browser --dry-run
#
# Confirmed against a real --debug-dump-html capture of
# https://ir.netflix.net/investor-news-and-events/financial-releases/default.aspx.
# Trimmed down to one card (module_item), keeping the exact structure that
# matters:
#
#   - The FIRST <a href> for this article is actually an aria-hidden
#     thumbnail link with no text at all -- parse_news_items() must fall
#     through that one (empty title, not added to seen_urls) and pick up the
#     real headline anchor next.
#   - The headline anchor's *own* immediate parent (div.module_headline)
#     wraps nothing but that anchor. Climbing one ancestor only reaches the
#     headline's own text, which itself mentions an unrelated date
#     ("Schedules Special Meeting for March 20, 2026, to Approve..."). The
#     old code fed that straight to parse_date() and got "March 20, 2026" --
#     wrong.
#   - The real dateline ("Feb 17, 2026") lives in a sibling div
#     (module_date-time) one level further up, alongside a *second*
#     unrelated mention of "March 20, 2026" inside a hidden sr-only span
#     next to the PDF download link. Feb 17 appears earlier in reading
#     order than that duplicate, so once the anchor's own text is excluded
#     from the search, the real date is the first (and correct) match.
# ---------------------------------------------------------------------------

NETFLIX_LINK_RE, _ = _news_link_matcher("press-release-details")

NETFLIX_CARD = """
<div id="_ctrl0_ctl57_repeaterContent_ctl05_divItem" class="module_item">
    <div class="module_thumbnail">
        <a href="https://ir.netflix.net/investor-news-and-events/financial-releases/press-release-details/2026/WBD-Files-Definitive-Proxy-Statement-and-Schedules-Special-Meeting-for-March-20-2026-to-Approve-the-WBD-Netflix-Transaction/default.aspx" id="_ctrl0_ctl57_repeaterContent_ctl05_hrefThumbnail" class="module_thumbnail-link" aria-hidden="true" aria-label="">
        </a>
    </div>
    <div class="module_date-time">
        <span class="module_date-text">Feb 17, 2026</span>
    </div>
    <div class="module_headline">
        <a href="https://ir.netflix.net/investor-news-and-events/financial-releases/press-release-details/2026/WBD-Files-Definitive-Proxy-Statement-and-Schedules-Special-Meeting-for-March-20-2026-to-Approve-the-WBD-Netflix-Transaction/default.aspx" id="_ctrl0_ctl57_repeaterContent_ctl05_hrefHeadline" class="module_headline-link">
            WBD Files Definitive Proxy Statement and Schedules Special Meeting for March 20, 2026, to Approve the WBD-Netflix Transaction
        </a>
    </div>
    <div class="module_links">
        <a href="//s22.q4cdn.com/959853165/files/doc_news/2026/02/Netflix-Press-Release-FINAL-1.pdf" id="_ctrl0_ctl57_repeaterContent_ctl05_hrefDownload" class="module_link" target="1908922337">
            <i class="q4-icon_pdf"></i>
            <span class="module_link-text">Download</span>
            <span class="sr-only">PDF format download (opens in new window)</span>
        <span class="sr-only">WBD Files Definitive Proxy Statement and Schedules Special Meeting for March 20, 2026, to Approve the WBD-Netflix Transaction</span></a>
    </div>
    <div class="module_body">
        <span id="_ctrl0_ctl57_repeaterContent_ctl05_spanEllipse" class="module_ellipse"></span>
    </div>
    <div class="module_more">
        <a href="https://ir.netflix.net/investor-news-and-events/financial-releases/press-release-details/2026/WBD-Files-Definitive-Proxy-Statement-and-Schedules-Special-Meeting-for-March-20-2026-to-Approve-the-WBD-Netflix-Transaction/default.aspx" id="_ctrl0_ctl57_repeaterContent_ctl05_hrefMoreLink" class="module_more-link" aria-hidden="true" aria-label="">
        </a>
    </div>
</div>
"""


def test_netflix_style_card_ignores_date_mentioned_inside_headline():
    """The real publish date (from the card's own module_date-text node)
    must win, not the unrelated "March 20, 2026" the headline text -- and a
    duplicate hidden sr-only span -- happen to mention."""
    items = parse_news_items(
        NETFLIX_CARD, BASE_URL, slug="netflix", ticker="NFLX", link_re=NETFLIX_LINK_RE
    )
    assert len(items) == 1
    assert items[0].publish_date == date(2026, 2, 17)
    assert items[0].raw_date_text == "Feb 17, 2026"
    assert "March 20, 2026" in items[0].title  # sanity check: still in headline