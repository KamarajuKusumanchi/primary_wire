"""
tests/test_detect_ir_platform.py

Tests for Investis detection in detect_ir_platform.py.

_check_investis() recognizes an Investis-powered IR site via either of two
independent signals, each sufficient on its own:

  1. The "Investis Sitecore common GTM" HTML comment in <head>.
  2. The visible footer branding credit/link: "Delivered by Investis"
     (plain branding) or "Delivered by Investis Digital" (Digital branding),
     linking to investis.com or investisdigital.com respectively.

This must be detected before Notified's broad link-pattern heuristic
(_check_notified_links / NOTIFIED_DETAIL_RE) runs, because Investis's own
press-release detail URLs -- /news-releases/<year>/<mm-dd-yyyy>-<serial>,
e.g. https://ir.homedepot.com/news-releases/2026/07-15-2026-130113180 --
also match that heuristic's shape.

The tests below cover each signal in isolation, both together, and neither,
plus the end-to-end priority against Notified.

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reporting.detect_ir_platform import (  # noqa: E402
    PLATFORM_INVESTIS,
    PLATFORM_NOTIFIED,
    _check_investis,
    detect_platform_from_html,
)

# ---------------------------------------------------------------------------
# Fixtures below are trimmed-down versions of real `--debug-dump-html`
# captures, e.g.:
#   python src/scrape_investis.py --dry-run --year 2026 --slug sysco \
#       --debug-dump-html sysco.html
#   python src/scrape_investis.py --dry-run --year 2026 --slug home-depot \
#       --debug-dump-html home-depot.html
# Keeping only the structural details that matter: the <head> Sitecore GTM
# comment, the footer branding credit/link, and one news-releases link with
# Investis's <year>/<mm-dd-yyyy>-<serial> shape (which also happens to
# satisfy Notified's broad link-pattern heuristic -- see NOTIFIED_DETAIL_RE).
# ---------------------------------------------------------------------------

SITECORE_GTM_COMMENT = "<!-- Investis Sitecore common GTM -->"
SYSCO_FOOTER = (
    '<div class="inv-branding"><p><a href="http://www.investis.com" '
    'title="Delivered by Investis \u2013 link to website (opens in a new '
    'window)" target="_blank" rel="nofollow"><span>Delivered by Investis'
    '</span></a></p></div>'
)
HOME_DEPOT_FOOTER = (
    '<p><a href="https://www.investisdigital.com" '
    'title="Delivered by Investis Digital">Delivered by Investis Digital</a></p>'
)
SYSCO_NEWS_LINK = (
    '<a href="https://investors.sysco.com/annual-reports-and-sec-filings/'
    'news-releases/2026/07-14-2026-130519584">'
    "Sysco to Announce Fourth Quarter and Fiscal Year 2026 Financial Results "
    "on August 4</a>"
)
HOME_DEPOT_NEWS_LINK = (
    '<a href="https://ir.homedepot.com/news-releases/2026/07-15-2026-130113180">'
    "Some Home Depot release</a>"
)

# Sitecore comment present, no footer markup.
SYSCO_PAGE_COMMENT_ONLY = f"""
<html><head>
{SITECORE_GTM_COMMENT}
</head><body>
{SYSCO_NEWS_LINK}
</body></html>
"""

# Footer branding credit/link present, no Sitecore comment in <head>.
SYSCO_PAGE_FOOTER_ONLY = f"""
<html><head>
</head><body>
{SYSCO_NEWS_LINK}
<footer>
{SYSCO_FOOTER}
</footer>
</body></html>
"""

# Both signals present together.
HOME_DEPOT_PAGE = f"""
<html><head>
{SITECORE_GTM_COMMENT}
</head><body>
{HOME_DEPOT_NEWS_LINK}
<footer>
{HOME_DEPOT_FOOTER}
</footer>
</body></html>
"""

# Neither Investis signal present -- a genuine Notified site's page, for
# contrast.
PAGE_WITH_NEITHER_SIGNAL = """
<html><head></head><body>
<a href="https://example.com/news-releases/news-release-details/some-slug">
Some unrelated release</a>
</body></html>
"""


def test_sysco_comment_only_page_is_recognized_as_investis():
    """Sitecore comment alone is sufficient."""
    assert _check_investis(SYSCO_PAGE_COMMENT_ONLY)


def test_sysco_footer_only_page_is_recognized_as_investis():
    """Footer credit/link alone, with no Sitecore comment in <head>, is
    sufficient."""
    assert _check_investis(SYSCO_PAGE_FOOTER_ONLY)


def test_home_depot_page_is_recognized_as_investis():
    """Both signals present together is also recognized."""
    assert _check_investis(HOME_DEPOT_PAGE)


def test_page_with_neither_signal_is_not_recognized_as_investis():
    """A page with neither fingerprint is not classified as Investis."""
    assert not _check_investis(PAGE_WITH_NEITHER_SIGNAL)


def test_sysco_comment_only_page_is_not_misclassified_as_notified():
    """End-to-end: Sysco's news-releases link shape
    (/news-releases/<year>/<mm-dd-yyyy>-<serial>) also satisfies Notified's
    broad link-pattern heuristic, so Investis must be detected before that
    heuristic gets a chance to fire."""
    assert detect_platform_from_html(SYSCO_PAGE_COMMENT_ONLY) == PLATFORM_INVESTIS
    assert detect_platform_from_html(SYSCO_PAGE_COMMENT_ONLY) != PLATFORM_NOTIFIED


def test_sysco_footer_only_page_is_not_misclassified_as_notified():
    """Same as above, but with only the footer signal present (no Sitecore
    comment)."""
    assert detect_platform_from_html(SYSCO_PAGE_FOOTER_ONLY) == PLATFORM_INVESTIS
    assert detect_platform_from_html(SYSCO_PAGE_FOOTER_ONLY) != PLATFORM_NOTIFIED


def test_home_depot_page_is_not_misclassified_as_notified():
    assert detect_platform_from_html(HOME_DEPOT_PAGE) == PLATFORM_INVESTIS