"""
tests/test_scrape_notified_gated.py

Covers scrape_notified_gated.build_year_url() -- the same query-string-
dropping bug reported and fixed for scrape_investorroom.py and
scrape_notified.py (see their own test files), also present here.

Background: a --url passed with a site-specific query string (e.g.
https://investor.tjx.com/investors/press-releases?category=788) had that
query string silently discarded -- resolve_source() strips --url down to
its site root (see resolve_source_identity()'s extra_query_params in
sources_utils.py) so news_releases_path can be joined onto the site root,
and build_year_url() then built its own exposed-filter query from scratch,
with no way for anything else that had been on --url to survive.

Fix: build_year_url() (and the chain feeding it -- get_year_url(),
scrape_year(), scrape(), resolve_source(), main()) now accepts an
extra_params dict and merges it into the query it builds, with the
exposed-filter's own params winning on any key collision.

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scrape_notified_gated import FormTokens, build_year_url  # noqa: E402

TOKENS = FormTokens(widget_hash="abc123", form_build_id="form-XYZ")


def test_build_year_url_preserves_extra_query_params():
    url = build_year_url(
        "https://investor.tjx.com/investors/press-releases",
        year=2026,
        tokens=TOKENS,
        extra_params={"category": "788"},
    )
    assert url.startswith(
        "https://investor.tjx.com/investors/press-releases?category=788&"
    )
    assert "abc123_year%5Bvalue%5D=2026" in url
    assert "form_build_id=form-XYZ" in url
    assert url.endswith("#widget-form-base")


def test_no_extra_params_matches_old_behavior():
    """Sanity check: omitting extra_params (the common case) is unaffected."""
    url = build_year_url(
        "https://investor.tjx.com/investors/press-releases",
        year=2026,
        tokens=TOKENS,
    )
    assert url == (
        "https://investor.tjx.com/investors/press-releases"
        "?abc123_year%5Bvalue%5D=2026&abc123_widget_id=abc123"
        "&form_build_id=form-XYZ&form_id=widget_form_base"
        "#widget-form-base"
    )
