"""
tests/test_sources_utils.py

Covers sources_utils.join_url_path() -- the helper that replaced the naive
`base_url.rstrip("/") + path` concatenation used across the scrapers (see
sources_utils.py, scrape_notified.py, scrape_investorroom.py, scrape_cdw.py,
scrape_costco.py, scrape_company_template.py).

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# src/ is a flat module directory, not an installed package (matches the
# sys.path.insert() pattern already used by scrape_cdw.py etc. to import
# sibling modules). The three shared utility modules live in src/utils/,
# a regular subpackage of src/, so src/ is what needs to be on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils.sources_utils import join_url_path, resolve_source_identity  # noqa: E402


@pytest.mark.parametrize(
    "base_url, path, expected",
    [
        # The four combinations of trailing slash on base_url / leading
        # slash on path should all normalize to the same result.
        ("https://ir.apollo.com", "/news-events/press-releases",
         "https://ir.apollo.com/news-events/press-releases"),
        ("https://ir.apollo.com/", "/news-events/press-releases",
         "https://ir.apollo.com/news-events/press-releases"),
        # No leading slash on path -- the case that broke before this fix
        # (produced "https://ir.apollo.comnews-events/press-releases").
        ("https://ir.apollo.com", "news-events/press-releases",
         "https://ir.apollo.com/news-events/press-releases"),
        ("https://ir.apollo.com/", "news-events/press-releases",
         "https://ir.apollo.com/news-events/press-releases"),

        # Empty path (resolve_source_identity's default listing_path_suffix)
        # returns base_url unchanged, aside from trailing-slash stripping.
        ("https://ir.apollo.com", "", "https://ir.apollo.com"),
        ("https://ir.apollo.com/", "", "https://ir.apollo.com"),

        # Multiple trailing slashes on base_url, multiple leading slashes
        # on path -- not expected in practice, but shouldn't produce
        # something obviously broken (e.g. doubled slashes in the path).
        ("https://ir.apollo.com///", "//news-releases",
         "https://ir.apollo.com/news-releases"),
    ],
)
def test_join_url_path(base_url: str, path: str, expected: str) -> None:
    assert join_url_path(base_url, path) == expected


# ---------------------------------------------------------------------------
# resolve_source_identity: strip_url_to_root
#
# Bug this covers: strip_url_to_root=True was only honored when a --url was
# passed explicitly. When resolving via --slug/--ticker instead (the common
# path), the record's ir_url was joined with listing_path_suffix as-is, so a
# sources.yaml entry like `ir_url: https://www.genpt.com/overview` produced
# `https://www.genpt.com/overview/press-releases` instead of the intended
# `https://www.genpt.com/press-releases` (see sources.yaml's own field
# comment: news_releases_path is "path appended to ir_url's host").
# ---------------------------------------------------------------------------

def _write_sources(tmp_path: Path, records: list[dict]) -> Path:
    lines = ["sources:"]
    for r in records:
        lines.append(f"  - slug: {r['slug']}")
        lines.append(f"    ticker: {r.get('ticker', '')}")
        lines.append(f"    ir_url: {r['ir_url']}")
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("\n".join(lines) + "\n")
    return sources_path


def test_resolve_source_identity_strips_path_bearing_ir_url_via_slug(tmp_path: Path) -> None:
    """The bug case: slug lookup, ir_url has a path, strip_url_to_root=True."""
    sources_path = _write_sources(tmp_path, [
        {"slug": "genuine-parts", "ticker": "GPC", "ir_url": "https://www.genpt.com/overview"},
    ])
    url, slug, ticker, record = resolve_source_identity(
        None, "genuine-parts", None,
        default_slug="chipotle", default_ticker="CMG", default_url="https://ir.chipotle.com",
        strip_url_to_root=True, sources_path=sources_path,
    )
    assert url == "https://www.genpt.com"
    assert slug == "genuine-parts"
    assert ticker == "GPC"


def test_resolve_source_identity_root_only_ir_url_unaffected_via_slug(tmp_path: Path) -> None:
    """Regression guard: sources whose ir_url is already a bare root (chipotle,
    axon, abbvie, ...) must keep resolving to the same URL as before."""
    sources_path = _write_sources(tmp_path, [
        {"slug": "chipotle", "ticker": "CMG", "ir_url": "https://ir.chipotle.com/"},
    ])
    url, slug, ticker, record = resolve_source_identity(
        None, "chipotle", None,
        default_slug="chipotle", default_ticker="CMG", default_url="https://ir.chipotle.com",
        strip_url_to_root=True, sources_path=sources_path,
    )
    assert url == "https://ir.chipotle.com"


def test_resolve_source_identity_listing_path_suffix_without_strip(tmp_path: Path) -> None:
    """scrape_q4_ir.py's path: no strip_url_to_root, but listing_path_suffix
    is appended onto the record's ir_url as-is."""
    sources_path = _write_sources(tmp_path, [
        {"slug": "amd", "ticker": "AMD", "ir_url": "https://ir.amd.com/"},
    ])
    url, slug, ticker, record = resolve_source_identity(
        None, "amd", None,
        default_slug="chipotle", default_ticker="CMG", default_url="https://ir.chipotle.com",
        listing_path_suffix="news-events/press-releases", sources_path=sources_path,
    )
    assert url == "https://ir.amd.com/news-events/press-releases"


def test_resolve_source_identity_strips_explicit_url(tmp_path: Path) -> None:
    """The branch that already worked before this fix: an explicitly-passed
    --url is still reduced to its root when strip_url_to_root=True."""
    sources_path = _write_sources(tmp_path, [
        {"slug": "genuine-parts", "ticker": "GPC", "ir_url": "https://www.genpt.com/overview"},
    ])
    url, slug, ticker, record = resolve_source_identity(
        "https://www.genpt.com/overview/some-page", None, None,
        default_slug="chipotle", default_ticker="CMG", default_url="https://ir.chipotle.com",
        strip_url_to_root=True, sources_path=sources_path,
    )
    assert url == "https://www.genpt.com"
    assert slug == "genuine-parts"