"""
tests/test_detect_ir_platform.py

Regression tests for TWO separate bugs, both found on investors.sysco.com,
both ending in the same misclassification ("notified" instead of
"investis"), via the same underlying mistake: _check_investis() relying on
a single fingerprint that turned out to not be permanent.

Bug #1 (original): _check_investis() only recognized the "Investis
Digital" footer branding (investisdigital.com), which Home Depot's IR site
uses. Sysco's IR site used the plain "Investis" branding instead
("Delivered by Investis", linking to investis.com, no "Digital" anywhere).
That variant matched neither regex, so _check_investis() returned False and
detection fell through to Notified's broad link-pattern heuristic, which
matches Investis's own /news-releases/<year>/<mm-dd-yyyy>-<serial> URL
shape just as well as an actual Notified site's.

Fix #1: switched _check_investis() to check for the "Investis Sitecore
common GTM" HTML comment instead -- a vendor-generated build-tool
signature confirmed present in <head> on both the Sysco (plain) and Home
Depot ("Digital") captures, regardless of footer branding.

Bug #2 (this one): a later Sysco IR-site redesign dropped the Sitecore GTM
comment from <head> entirely (confirmed via a fresh
--debug-dump-html capture), while leaving the "Delivered by Investis"
footer credit and its investis.com link untouched. Since Fix #1 had made
the Sitecore comment the ONLY signal _check_investis() looked at, losing
it reproduced the exact same "notified" misclassification via the exact
same failure mode as Bug #1 -- just with the fragile single fingerprint
swapped for a different fragile single fingerprint.

Fix #2: _check_investis() now checks BOTH signals (Sitecore comment OR
footer credit/link), so either one going away in isolation is no longer
enough to cause a misclassification. The tests below cover all four
combinations that matter: comment-only (old Sysco-style capture, still
must work), footer-only (new Sysco-style capture, the actual regression),
both together (Home Depot), and neither (must NOT be classified investis).

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

# Bug #1's original shape: Sitecore comment present, no footer markup at all.
SYSCO_PAGE_COMMENT_ONLY = f"""
<html><head>
{SITECORE_GTM_COMMENT}
</head><body>
{SYSCO_NEWS_LINK}
</body></html>
"""

# Bug #2's shape (this fix): redesigned page, Sitecore comment gone from
# <head>, but the footer branding credit/link survived the redesign.
# Mirrors the real sysco.html --debug-dump-html capture that reproduced
# this bug.
SYSCO_PAGE_FOOTER_ONLY = f"""
<html><head>
</head><body>
{SYSCO_NEWS_LINK}
<footer>
{SYSCO_FOOTER}
</footer>
</body></html>
"""

# Home Depot capture: has both signals together, as originally observed.
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

# Neither signal present -- a genuine Notified site's page, for contrast.
# Same link shape as Sysco/Home Depot's news-releases URLs would NOT even
# need to appear here for this to look like Notified; it's the *absence*
# of both Investis signals that must correctly leave this unclassified as
# investis (detect_platform_from_html falls through to its own signals
# elsewhere in the pipeline -- not this module's concern for this test).
PAGE_WITH_NEITHER_SIGNAL = """
<html><head></head><body>
<a href="https://example.com/news-releases/news-release-details/some-slug">
Some unrelated release</a>
</body></html>
"""


def test_sysco_comment_only_page_is_recognized_as_investis():
    """Bug #1's shape must still work: Sitecore comment alone is enough."""
    assert _check_investis(SYSCO_PAGE_COMMENT_ONLY)


def test_sysco_footer_only_page_is_recognized_as_investis():
    """Bug #2 (this fix): footer credit/link alone, with no Sitecore
    comment in <head>, must still be recognized as Investis."""
    assert _check_investis(SYSCO_PAGE_FOOTER_ONLY)


def test_home_depot_page_is_recognized_as_investis():
    """Regression guard: fixing Sysco's cases must not break Home Depot's
    (both signals present) detection."""
    assert _check_investis(HOME_DEPOT_PAGE)


def test_page_with_neither_signal_is_not_recognized_as_investis():
    """Sanity check: a page with neither fingerprint isn't just always
    treated as Investis regardless of content."""
    assert not _check_investis(PAGE_WITH_NEITHER_SIGNAL)


def test_sysco_comment_only_page_is_not_misclassified_as_notified():
    """End-to-end: Sysco's news-releases link shape
    (/news-releases/<year>/<mm-dd-yyyy>-<serial>) also satisfies Notified's
    broad link-pattern heuristic, so Investis must be detected before that
    heuristic gets a chance to fire."""
    assert detect_platform_from_html(SYSCO_PAGE_COMMENT_ONLY) == PLATFORM_INVESTIS
    assert detect_platform_from_html(SYSCO_PAGE_COMMENT_ONLY) != PLATFORM_NOTIFIED


def test_sysco_footer_only_page_is_not_misclassified_as_notified():
    """The actual bug being fixed here, end to end: with the Sitecore
    comment gone (as on the redesigned page), the footer signal must still
    win over Notified's broad link-pattern heuristic."""
    assert detect_platform_from_html(SYSCO_PAGE_FOOTER_ONLY) == PLATFORM_INVESTIS
    assert detect_platform_from_html(SYSCO_PAGE_FOOTER_ONLY) != PLATFORM_NOTIFIED


def test_home_depot_page_is_not_misclassified_as_notified():
    assert detect_platform_from_html(HOME_DEPOT_PAGE) == PLATFORM_INVESTIS