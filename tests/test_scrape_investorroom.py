"""
tests/test_scrape_investorroom.py

Covers scrape_investorroom.parse_listing_page() / extract_date_near_link() --
specifically a regression found on Danaher's InvestorRoom site.

Background: running

    python src/scrape_investorroom.py --dry-run --year 2026 --slug danaher

for 2026-07-21 returned the one expected press release *plus* six unrelated
sidebar "quick links" (Events & Presentations, Annual Report & Proxy,
Additional Financial Information, Historical Price Lookup, Investment
Calculator, Request Printed Materials), all mis-dated 2026-07-21.

Root cause: those sidebar links are same-host, single-path-segment URLs, so
classify_link() treats them as "bare-slug" detail-page candidates (Style C,
see module docstring) pending confirmation via a headline-length title and a
nearby date. Their titles are long enough to pass, and extract_date_near_link()
climbs up to 5 ancestor elements looking for a date -- which, for a sidebar
nav link, eventually reaches a layout container broad enough to also contain
the real press-release list further down the page, and picks up *that* list's
first item's date as if it belonged to the sidebar link.

Fix: extract_date_near_link() now stops climbing as soon as the current
ancestor contains another *distinct* candidate link (see
_other_candidate_hrefs()) -- i.e. as soon as it's climbed out of this item's
own card and into a wrapper shared with other, unrelated items. A same-article
duplicate link (e.g. a headline link plus a separate ?asPDF download link)
does not count as "another" item and must not stop the climb early, since
that's how a real Danaher press-release card is shaped -- covered by
test_real_card_with_duplicate_pdf_link_still_gets_its_date() below.

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scrape_investorroom import parse_listing_page  # noqa: E402

BASE_URL = "https://investors.danaher.com"

# ---------------------------------------------------------------------------
# Trimmed down from the actual --debug-dump-html capture of
# https://investors.danaher.com/news-releases?year=2026, keeping only the
# structural details that matter: a sidebar nav with several bare-slug
# "quick links" sharing a broad ancestor with the real press-release list,
# whose first item is a genuine card with a headline link, a duplicate
# ?asPDF link, and a standalone date text node.
# ---------------------------------------------------------------------------

DANAHER_PAGE = """
<div class="layout-page">
  <div class="wd_nav">
    <div class="wd_nav-inner">
      <div class="wd_nav-item has_children">
        <div class="wd_subnav">
          <a href="https://investors.danaher.com/press-releases">Press Releases</a>
          <a href="https://investors.danaher.com/events-presentations">Events &amp; Presentations</a>
        </div>
      </div>
      <div class="wd_nav-item has_children">
        <div class="wd_subnav">
          <a href="https://investors.danaher.com/sec-filings">SEC Filings</a>
          <a href="https://investors.danaher.com/annual-report-and-proxy">Annual Report &amp; Proxy</a>
          <a href="https://investors.danaher.com/Additional-Financial-Information">Additional Financial Information</a>
        </div>
      </div>
    </div>
  </div>

  <ul class="wd_layout-simple wd_item_list">
    <li class="wd_item">
      <div class="wd_item_wrapper">
        <div class="wd_pdf_link">
          <a href="https://investors.danaher.com/2026-07-21-Danaher-Reports-Second-Quarter-2026-Results?asPDF"><img src="acrobat.png" alt="Download PDF"/></a>
        </div>
        <div class="wd_title">
          <a href="https://investors.danaher.com/2026-07-21-Danaher-Reports-Second-Quarter-2026-Results">Danaher Reports Second Quarter 2026 Results</a>
        </div>
        <div class="wd_date">Jul 21, 2026</div>
        <div class="wd_summary"><p>Danaher Corporation (NYSE: DHR) today announced results for the second quarter 2026.</p></div>
      </div>
    </li>
  </ul>
</div>
"""


def _items_by_title(html: str) -> dict:
    items, _next_url = parse_listing_page(html, BASE_URL, slug="danaher", ticker="DHR")
    return {item.title: item for item in items}


def test_sidebar_quick_links_are_not_mistaken_for_press_releases():
    """The sidebar nav links must not appear as items at all: they have no
    date of their own nearby, so they should fail bare-slug confirmation
    and be skipped -- not silently inherit the first press release's date."""
    by_title = _items_by_title(DANAHER_PAGE)

    leaked_titles = {
        "Events & Presentations",
        "Annual Report & Proxy",
        "Additional Financial Information",
    }
    assert not (leaked_titles & set(by_title)), (
        f"Sidebar nav links leaked into results: {leaked_titles & set(by_title)}"
    )


def test_real_card_with_duplicate_pdf_link_still_gets_its_date():
    """The genuine press-release card has two links to the same article (the
    headline link and a ?asPDF download link) -- that duplicate must NOT be
    treated as 'another item' and must not stop the ancestor climb before it
    reaches the card's actual date text."""
    by_title = _items_by_title(DANAHER_PAGE)

    title = "Danaher Reports Second Quarter 2026 Results"
    assert title in by_title
    assert by_title[title].publish_date == date(2026, 7, 21)


def test_only_one_item_for_the_press_release_url():
    """End-to-end sanity check matching the originally reported symptom:
    parsing the page should yield exactly one item, not the real release
    plus six mis-dated sidebar links."""
    items, _next_url = parse_listing_page(DANAHER_PAGE, BASE_URL, slug="danaher", ticker="DHR")
    assert len(items) == 1
    assert items[0].title == "Danaher Reports Second Quarter 2026 Results"
    assert items[0].publish_date == date(2026, 7, 21)