"""
tests/test_scrape_notified_year_filter.py

Covers the "Year" filter widget discovery/handling added to
scrape_notified.py and utils/scrape_notified_utils.py.

Background: running

    python src/scrape_notified.py --dry-run --year 2025 --slug virtu \
        --debug-dump-html virtu_2025.html

found only 1 item (a stray January-2026 post about Q4 2025 results)
instead of the 18 real 2025 press releases, even though 2025 has its own
page (https://ir.virtu.com/press-releases -> Year: 2025 -> Filter) with
all 18 releases on it.

Root cause: Virtu's listing page renders a real "Year" <select> filter,
submitted via a GET-method Drupal Views exposed-filter form (the same
form/prefix convention as the pre-existing "Items Per Page" widget --
see resolve_page_size_from_html() in utils/scrape_notified_utils.py). The
module docstring's long-standing assumption ("Year filter ... NOT
reflected in the URL ... filter by year client-side after scraping") does
NOT hold for this site: critically, the <select>'s own default-selected
<option> is NOT "All" (value "_none") -- it's whatever the current year
is (captured 2026-08-03: "2026"). So a plain, param-less fetch of the
listing page (exactly what scrape_notified.py's normal "no year filter" /
binary-search-priming fetches do) silently returns only *this year's*
releases, not full history -- there's nothing to binary-search into for
any other year.

Fix: discover_year_widget()/year_filter_extra_params()/
resolve_year_filter_from_html() (utils/scrape_notified_utils.py) find this
widget (when present) from the same probe fetch already used for
--page-size discovery, and force an explicit, unambiguous year scope into
scrape()'s query params -- the single target year if exactly one is
requested and the widget offers it, else explicit "_none" ("All").

Important wrinkle, caught by testing this fix against a live fetch of
Skyworks (https://investors.skyworksinc.com/press-releases), which has
the exact same widget shape and was previously assumed (by inference from
Virtu's identical markup) to behave the same way: Skyworks' Year <select>
is real and submittable exactly like Virtu's, but the site silently
IGNORES the submitted ``_year[value]`` -- only its Items Per Page param is
actually honored server-side. Because of this, the fix deliberately does
NOT special-case "server-side year filter found -> skip the binary
search"; it only ever forces the query param and otherwise leaves the
existing binary-search/early-exit machinery (which reads each fetched
page's own actual dates rather than trusting the request) to do the real
work, so results stay correct whether or not a given site's widget turns
out to do anything. test_scrape_returns_correct_year_even_when_site_silently_ignores_the_year_param()
below pins that down directly.

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import scrape_notified as sn  # noqa: E402
from utils.scrape_notified_utils import (  # noqa: E402
    discover_year_widget,
    resolve_year_filter_from_html,
    year_filter_extra_params,
)

BASE_URL = "https://ir.example.com"
PREFIX = "aac2c52233ec9ed03e44a98dd9028c83ac2c52a24dacec95b3c1757c0d59015b"


# ---------------------------------------------------------------------------
# Synthetic widget-form fragments, structurally matching real captures of
# both ir.virtu.com/press-releases and investors.skyworksinc.com/press-releases
# (trimmed to just the parts discover_year_widget()/discover_page_size_widget()
# actually look at: the <select>, and the form's form_id/form_build_id
# hidden inputs).
# ---------------------------------------------------------------------------

def _year_widget_form(prefix: str, selected_year: "int | str") -> str:
    """Build a widget-form-base fragment with a Year <select>, matching
    Drupal's own rendering: only the selected <option> gets a `selected`
    attribute; "All" (value "_none") is otherwise implicit."""
    options = ['<option value="_none">All</option>']
    for year in (2026, 2025, 2024, 2023):
        attrs = ' selected="selected"' if str(year) == str(selected_year) else ""
        options.append(f'<option value="{year}"{attrs}>{year}</option>')
    if selected_year == "_none":
        options[0] = '<option value="_none" selected="selected">All</option>'
    return f"""
<form class="webform-submission-form" method="get">
  <select data-drupal-selector="edit-year-value"
          id="edit-year-value"
          name="{prefix}_year[value]"
          class="form-select">{''.join(options)}</select>
  <button type="submit">Filter</button>
  <input type="hidden" name="form_build_id" value="form-TESTBUILDID" />
  <input type="hidden" name="form_id" value="widget_form_base" />
</form>
"""


VIRTU_STYLE_PROBE_HTML = _year_widget_form(PREFIX, selected_year=2026)
SKYWORKS_STYLE_PROBE_HTML = _year_widget_form(PREFIX, selected_year="_none")
NO_WIDGET_PROBE_HTML = "<html><body><p>No filter widgets here.</p></body></html>"


def _card(href: str, iso_date: str, human_date: str, headline: str) -> str:
    """One press-release card, structurally matching the real Virtu/Skyworks
    (and GE Vernova, per test_scrape_notified.py) capture shape closely
    enough for parse_listing_page() to recognize it."""
    return f"""
  <article class="node--nir-news--nir-widget-list">
    <div class="nir-widget--field nir-widget--news--date-time date">
      <time datetime="{iso_date}">{human_date}</time>
    </div>
    <div class="nir-widget--field nir-widget--news--headline title">
      <a href="{href}">{headline}</a>
    </div>
  </article>
"""


def _pager(last_page_index: int) -> str:
    """Drupal Views pager fragment with a "last »" link, structurally
    matching the real markup find_last_page() (utils/scrape_notified_utils.py)
    looks for: an <a href title> whose title says "go to last page"."""
    return f"""
  <ul class="pager__items js-pager__items">
    <li class="pager__item"><a href="?page=0" title="Go to first page">« First</a></li>
    <li class="pager__item pager__item--last">
      <a href="?page={last_page_index}" title="Go to last page">Last »</a>
    </li>
  </ul>
"""


# ---------------------------------------------------------------------------
# Unit tests: discover_year_widget()
# ---------------------------------------------------------------------------

def test_discover_year_widget_finds_virtu_style_widget_defaulting_to_current_year():
    widget = discover_year_widget(VIRTU_STYLE_PROBE_HTML)
    assert widget is not None
    assert widget["prefix"] == PREFIX
    assert widget["years"] == [2023, 2024, 2025, 2026]
    assert widget["has_none"] is True
    # This is the crux of the whole bug: the default is a specific year,
    # not "All".
    assert widget["default"] == 2026
    assert widget["form_id"] == "widget_form_base"
    assert widget["form_build_id"] == "form-TESTBUILDID"


def test_discover_year_widget_finds_skyworks_style_widget_defaulting_to_all():
    widget = discover_year_widget(SKYWORKS_STYLE_PROBE_HTML)
    assert widget is not None
    assert widget["default"] == "_none"


def test_discover_year_widget_returns_none_when_absent():
    assert discover_year_widget(NO_WIDGET_PROBE_HTML) is None


def test_discover_year_widget_returns_none_on_empty_html():
    assert discover_year_widget("") is None


# ---------------------------------------------------------------------------
# Unit tests: year_filter_extra_params()
# ---------------------------------------------------------------------------

def test_year_filter_extra_params_shape():
    widget = discover_year_widget(VIRTU_STYLE_PROBE_HTML)
    params = year_filter_extra_params(widget, 2025)
    assert params[f"{PREFIX}_year[value]"] == "2025"
    assert params["op"] == "Filter"
    assert params[f"{PREFIX}_widget_id"] == PREFIX
    assert params["form_id"] == "widget_form_base"
    assert params["form_build_id"] == "form-TESTBUILDID"


def test_year_filter_extra_params_accepts_none_sentinel():
    widget = discover_year_widget(VIRTU_STYLE_PROBE_HTML)
    params = year_filter_extra_params(widget, "_none")
    assert params[f"{PREFIX}_year[value]"] == "_none"


# ---------------------------------------------------------------------------
# Unit tests: resolve_year_filter_from_html()
# ---------------------------------------------------------------------------

def test_resolve_year_filter_requests_explicit_year_for_single_target_year():
    params = resolve_year_filter_from_html(VIRTU_STYLE_PROBE_HTML, {2025})
    assert params[f"{PREFIX}_year[value]"] == "2025"


def test_resolve_year_filter_forces_none_when_default_is_a_specific_year_and_multiple_years_targeted():
    params = resolve_year_filter_from_html(VIRTU_STYLE_PROBE_HTML, {2023, 2024, 2025})
    assert params[f"{PREFIX}_year[value]"] == "_none"


def test_resolve_year_filter_forces_none_when_default_is_a_specific_year_and_no_years_targeted():
    """This is the case that would otherwise silently corrupt even an
    unfiltered (--dry-run with no --year at all) run on a Virtu-shaped
    site: without this, a plain fetch only sees the current year's items."""
    params = resolve_year_filter_from_html(VIRTU_STYLE_PROBE_HTML, None)
    assert params[f"{PREFIX}_year[value]"] == "_none"


def test_resolve_year_filter_is_noop_when_default_already_all_and_multiple_years_targeted():
    params = resolve_year_filter_from_html(SKYWORKS_STYLE_PROBE_HTML, {2023, 2024, 2025})
    assert params == {}


def test_resolve_year_filter_is_noop_when_default_already_all_and_no_years_targeted():
    params = resolve_year_filter_from_html(SKYWORKS_STYLE_PROBE_HTML, None)
    assert params == {}


def test_resolve_year_filter_still_requests_explicit_single_year_even_when_default_already_all():
    """Single-year requests always ask for that year explicitly, regardless
    of the widget's own default -- harmless when already "All", and an
    optimization (usually cutting the scrape down to 1-2 pages) on sites
    that do honor it."""
    params = resolve_year_filter_from_html(SKYWORKS_STYLE_PROBE_HTML, {2025})
    assert params[f"{PREFIX}_year[value]"] == "2025"


def test_resolve_year_filter_falls_back_to_none_when_requested_year_not_offered():
    """2020 isn't one of the widget's options (2023-2026); since it's a
    single-year request that the widget can't directly satisfy, and the
    widget's own default is already "_none", there's nothing useful to
    force -- fall back to the caller's normal full-archive pagination."""
    params = resolve_year_filter_from_html(SKYWORKS_STYLE_PROBE_HTML, {2020})
    assert params == {}


def test_resolve_year_filter_is_noop_when_no_widget_present():
    assert resolve_year_filter_from_html(NO_WIDGET_PROBE_HTML, {2025}) == {}


def test_resolve_year_filter_is_noop_when_probe_fetch_failed():
    """html=None means the probe fetch itself failed; must not raise."""
    assert resolve_year_filter_from_html(None, {2025}) == {}


# ---------------------------------------------------------------------------
# Integration tests: scrape_notified.scrape() end-to-end, with fetch_html
# mocked out (no real network access).
# ---------------------------------------------------------------------------

def _query_params(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_scrape_forces_explicit_year_param_on_a_virtu_style_site(monkeypatch):
    """End-to-end reproduction of the reported bug and its fix: requesting
    --year 2025 against a site whose unfiltered listing defaults to 2026
    must actually request year=2025 through the widget, and must return
    the 2025 item -- not silently come back with 0/1 items the way the
    original bug did.
    """
    filtered_page = VIRTU_STYLE_PROBE_HTML + _pager(0) + _card(
        f"{BASE_URL}/news-releases/news-release-details/virtu-2025-item",
        "2025-06-15T09:00:00-04:00", "June 15, 2025",
        "Virtu Announces Something In 2025",
    )
    # The unfiltered/default listing (no year param, or year=2026) only has
    # a 2026 item on it -- reproducing Virtu's actual current-year-only
    # default. Used only for the very first widget-discovery probe fetch
    # below (which deliberately carries no query params at all yet).
    unfiltered_probe_page = VIRTU_STYLE_PROBE_HTML + _pager(0) + _card(
        f"{BASE_URL}/news-releases/news-release-details/virtu-2026-item",
        "2026-01-29T09:00:00-05:00", "January 29, 2026",
        "Virtu Announces Something In 2026",
    )

    fetched_urls: list[str] = []

    def fake_fetch_html(url, session, timeout=30):
        fetched_urls.append(url)
        params = _query_params(url)
        if params.get(f"{PREFIX}_year[value]") == "2025":
            return filtered_page
        return unfiltered_probe_page

    monkeypatch.setattr(sn, "fetch_html", fake_fetch_html)

    items = sn.scrape(
        base_url=BASE_URL,
        slug="virtu",
        ticker="VIRT",
        years={2025},
        polite_delay=0,
        timeout=30,
        debug_dump_html=None,
        session=None,
        news_releases_path="press-releases",
    )

    titles = {item.title for item in items}
    assert "Virtu Announces Something In 2025" in titles
    assert "Virtu Announces Something In 2026" not in titles
    assert items[0].publish_date == date(2025, 6, 15)

    # fetched_urls[0] is the widget-discovery probe fetch, made before the
    # year param is even known -- it legitimately has no year param yet.
    # Every fetch after that must explicitly request year=2025: this is
    # the actual fix. (Without it, every one of these would have gone out
    # with no year param at all -- or, on a real Virtu-shaped site, would
    # have silently inherited whatever year the site defaults to.)
    assert len(fetched_urls) >= 2, fetched_urls
    assert _query_params(fetched_urls[0]).get(f"{PREFIX}_year[value]") is None
    for url in fetched_urls[1:]:
        assert _query_params(url).get(f"{PREFIX}_year[value]") == "2025", url


def test_scrape_forces_none_when_no_year_targeted_on_a_virtu_style_site(monkeypatch):
    """Even a plain --dry-run with no --year at all must not silently come
    back scoped to whatever year the site's <select> defaults to."""
    page = VIRTU_STYLE_PROBE_HTML + _pager(0) + _card(
        f"{BASE_URL}/news-releases/news-release-details/virtu-old-item",
        "2023-03-01T09:00:00-05:00", "March 1, 2023",
        "Virtu Announces Something Old",
    )

    fetched_urls: list[str] = []

    def fake_fetch_html(url, session, timeout=30):
        fetched_urls.append(url)
        return page

    monkeypatch.setattr(sn, "fetch_html", fake_fetch_html)

    items = sn.scrape(
        base_url=BASE_URL,
        slug="virtu",
        ticker="VIRT",
        years=None,
        polite_delay=0,
        timeout=30,
        debug_dump_html=None,
        session=None,
        news_releases_path="press-releases",
    )

    assert len(items) == 1
    assert items[0].title == "Virtu Announces Something Old"
    # No year filter was requested at all (years=None), so scrape() never
    # reaches the "if years:" step-1 fetch -- but page_size is None too
    # here, so no probe fetch happens either (see scrape()'s "if page_size
    # or years:" guard) and this ends up going straight to the plain,
    # unfiltered full-archive scan with no widget discovery at all. That's
    # fine: this test's job is just to confirm nothing crashes and the
    # (only) item still comes back correctly when neither feature applies.
    assert fetched_urls


def test_scrape_with_page_size_forces_none_when_no_year_targeted_on_a_virtu_style_site(monkeypatch):
    """Same as above, but with --page-size set (the CLI's actual default is
    100), which is what triggers the widget-discovery probe in practice --
    this is the real "plain --dry-run" shape that matters."""
    page = VIRTU_STYLE_PROBE_HTML + _pager(0) + _card(
        f"{BASE_URL}/news-releases/news-release-details/virtu-old-item",
        "2023-03-01T09:00:00-05:00", "March 1, 2023",
        "Virtu Announces Something Old",
    )

    fetched_urls: list[str] = []

    def fake_fetch_html(url, session, timeout=30):
        fetched_urls.append(url)
        return page

    monkeypatch.setattr(sn, "fetch_html", fake_fetch_html)

    items = sn.scrape(
        base_url=BASE_URL,
        slug="virtu",
        ticker="VIRT",
        years=None,
        page_size=100,
        polite_delay=0,
        timeout=30,
        debug_dump_html=None,
        session=None,
        news_releases_path="press-releases",
    )

    assert len(items) == 1
    assert fetched_urls
    # This time a widget-discovery probe did happen (triggered by
    # page_size), so every fetch -- including the probe -- has the same
    # no-query-params starting point, and the *second* fetch onward must
    # carry the forced "_none" -- the probe fetch itself is exempt for the
    # same reason as in the --year test above.
    assert len(fetched_urls) >= 2, fetched_urls
    for url in fetched_urls[1:]:
        assert _query_params(url).get(f"{PREFIX}_year[value]") == "_none", url


def test_scrape_returns_correct_year_even_when_site_silently_ignores_the_year_param(monkeypatch):
    """Skyworks-shaped regression guard: the widget is present and offers
    the requested year, but the *site itself* ignores the submitted
    ``_year[value]`` and always returns its full, unfiltered,
    reverse-chronological archive. The fix must not blindly trust the
    widget and skip pagination -- it must still find the right items via
    the existing binary-search/early-exit machinery, which reads each
    page's actual dates rather than assuming the request worked.
    """
    # Three pages of the *full, unfiltered* archive (newest first, with a
    # real pager) -- every fetch returns one of these same three pages
    # (keyed only by ?page=, never by the ignored year param), exactly
    # like the real Skyworks behavior confirmed via a live fetch during
    # development of this fix.
    last_page_index = 2
    page0 = SKYWORKS_STYLE_PROBE_HTML + _pager(last_page_index) + "".join([
        _card(f"{BASE_URL}/news-releases/news-release-details/item-2026-a",
              "2026-07-28T07:00:00-04:00", "July 28, 2026", "2026 Item A"),
        _card(f"{BASE_URL}/news-releases/news-release-details/item-2025-a",
              "2025-11-04T07:00:00-04:00", "November 4, 2025", "2025 Item A"),
    ])
    page1 = SKYWORKS_STYLE_PROBE_HTML + _pager(last_page_index) + "".join([
        _card(f"{BASE_URL}/news-releases/news-release-details/item-2025-b",
              "2025-02-10T07:00:00-05:00", "February 10, 2025", "2025 Item B"),
        _card(f"{BASE_URL}/news-releases/news-release-details/item-2024-a",
              "2024-12-01T07:00:00-05:00", "December 1, 2024", "2024 Item A"),
    ])
    page2 = SKYWORKS_STYLE_PROBE_HTML + _pager(last_page_index) + _card(
        f"{BASE_URL}/news-releases/news-release-details/item-2024-b",
        "2024-06-01T07:00:00-04:00", "June 1, 2024", "2024 Item B",
    )
    pages = [page0, page1, page2]

    fetched_urls: list[str] = []

    def fake_fetch_html(url, session, timeout=30):
        fetched_urls.append(url)
        params = _query_params(url)
        page_num = int(params.get("page", 0))
        if fetched_urls.index(url) > 0:
            # Confirm the fix really did request year=2025 on every fetch
            # after the initial widget-discovery probe -- it's just that
            # this fake site (like the real Skyworks) ignores it.
            assert params.get(f"{PREFIX}_year[value]") == "2025", url
        return pages[min(page_num, len(pages) - 1)]

    monkeypatch.setattr(sn, "fetch_html", fake_fetch_html)

    items = sn.scrape(
        base_url=BASE_URL,
        slug="skyworks",
        ticker="SWKS",
        years={2025},
        polite_delay=0,
        timeout=30,
        debug_dump_html=None,
        session=None,
        news_releases_path="press-releases",
    )

    titles = {item.title for item in items}
    assert "2025 Item A" in titles
    assert "2025 Item B" in titles
    # 2026/2024 items may or may not be included by scrape() itself (exact
    # year filtering is finalize_and_output()'s job, not scrape()'s -- see
    # this module's docstring) but the real 2025 items must never be lost,
    # and the binary search must not have walked past page 2 (the whole
    # archive is only 3 pages).
    assert all(int(_query_params(u).get("page", 0)) <= last_page_index for u in fetched_urls)