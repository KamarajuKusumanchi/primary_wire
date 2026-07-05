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

from utils.sources_utils import join_url_path  # noqa: E402


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