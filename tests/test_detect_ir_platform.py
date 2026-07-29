"""
tests/test_detect_ir_platform.py

Regression test for a bug found on investors.sysco.com: it was misclassified
as "notified" instead of "investis".

Root cause: _check_investis() only recognized the "Investis Digital" footer
branding (investisdigital.com), which Home Depot's IR site uses. Sysco's IR
site uses the plain "Investis" branding instead ("Delivered by Investis",
linking to investis.com, no "Digital" anywhere) -- confirmed via
--debug-dump-html captures of both sites. That variant matched neither
regex, so _check_investis() returned False and detection fell through to
Notified's broad link-pattern heuristic, which matches Investis's own
/news-releases/<year>/<mm-dd-yyyy>-<serial> URL shape just as well as an
actual Notified site's.

Fix: _check_investis() now checks for the "Investis Sitecore common GTM"
HTML comment instead -- an identical, vendor-generated build-tool signature
present in <head> on both the Sysco (plain) and Home Depot ("Digital")
captures, regardless of footer branding. Confirmed present on both, so no
footer-text fallback is needed.

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
# Trimmed down from real `--debug-dump-html` captures:
#   python src/scrape_investis.py --dry-run --year 2026 --slug sysco \
#       --debug-dump-html sysco.html
#   python src/scrape_investis.py --dry-run --year 2026 --slug home-depot \
#       --debug-dump-html home-depot.html
# Keeping only the two structural details that matter: the <head> Sitecore
# GTM comment (identical on both) and one news-releases link with
# Investis's <year>/<mm-dd-yyyy>-<serial> shape (which also happens to
# satisfy Notified's broad link-pattern heuristic -- see NOTIFIED_DETAIL_RE).
# Footer branding is deliberately left out of both fixtures below, since
# detection no longer depends on it.
# ---------------------------------------------------------------------------

SITECORE_GTM_COMMENT = "<!-- Investis Sitecore common GTM -->"

SYSCO_PAGE = f"""
<html><head>
{SITECORE_GTM_COMMENT}
</head><body>
<a href="https://investors.sysco.com/annual-reports-and-sec-filings/news-releases/2026/07-14-2026-130519584">
Sysco to Announce Fourth Quarter and Fiscal Year 2026 Financial Results on August 4</a>
</body></html>
"""

HOME_DEPOT_PAGE = f"""
<html><head>
{SITECORE_GTM_COMMENT}
</head><body>
<a href="https://ir.homedepot.com/news-releases/2026/07-15-2026-130113180">
Some Home Depot release</a>
</body></html>
"""


def test_sysco_page_is_recognized_as_investis():
    """The bug: Sysco's page (plain "Investis" footer branding, not
    "Digital") used to go undetected entirely by the old footer-text-only
    check. Detection no longer looks at footer text at all."""
    assert _check_investis(SYSCO_PAGE)


def test_home_depot_page_is_recognized_as_investis():
    """Regression guard: fixing Sysco's case must not break Home Depot's
    ("Investis Digital" branding) detection."""
    assert _check_investis(HOME_DEPOT_PAGE)


def test_sysco_page_is_not_misclassified_as_notified():
    """End-to-end: Sysco's news-releases link shape
    (/news-releases/<year>/<mm-dd-yyyy>-<serial>) also satisfies Notified's
    broad link-pattern heuristic, so Investis must be detected before that
    heuristic gets a chance to fire."""
    assert detect_platform_from_html(SYSCO_PAGE) == PLATFORM_INVESTIS
    assert detect_platform_from_html(SYSCO_PAGE) != PLATFORM_NOTIFIED


def test_home_depot_page_is_not_misclassified_as_notified():
    assert detect_platform_from_html(HOME_DEPOT_PAGE) == PLATFORM_INVESTIS