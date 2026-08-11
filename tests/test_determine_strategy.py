"""
tests/test_determine_strategy.py

Covers detect_ir_platform.determine_strategy() and
_resolve_listing_platform() -- the platform -> strategy expansion
described in detect_ir_platform.py's "Platform vs. strategy" note (just
above GATED_SLUGS).

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reporting.detect_ir_platform import (  # noqa: E402
    PLATFORM_AEM,
    PLATFORM_INVESTORROOM,
    PLATFORM_NOTIFIED,
    PLATFORM_NOTIFIED_GATED,
    PLATFORM_Q4,
    PLATFORM_UNKNOWN,
    STRATEGY_AEM_BNY,
    STRATEGY_AEM_CME,
    STRATEGY_Q4_IR,
    _resolve_listing_platform,
    determine_strategy,
)


def test_default_strategy_equals_platform():
    """The common case: one platform, one strategy, same name."""
    assert determine_strategy(PLATFORM_INVESTORROOM, "axon") == PLATFORM_INVESTORROOM
    assert determine_strategy(PLATFORM_UNKNOWN, "some-slug") == PLATFORM_UNKNOWN


def test_gated_slug_promotes_notified_to_notified_gated():
    assert determine_strategy(PLATFORM_NOTIFIED, "tjx") == PLATFORM_NOTIFIED_GATED
    # Case-insensitive, matching GATED_SLUGS' own lowercase membership check.
    assert determine_strategy(PLATFORM_NOTIFIED, "TJX") == PLATFORM_NOTIFIED_GATED


def test_non_gated_slug_stays_plain_notified():
    assert determine_strategy(PLATFORM_NOTIFIED, "abbvie") == PLATFORM_NOTIFIED


def test_q4_always_becomes_q4_ir():
    assert determine_strategy(PLATFORM_Q4, "costco") == STRATEGY_Q4_IR
    assert determine_strategy(PLATFORM_Q4, "some-other-q4-slug") == STRATEGY_Q4_IR


def test_aem_bny_slug():
    assert determine_strategy(PLATFORM_AEM, "bny") == STRATEGY_AEM_BNY


def test_aem_cme_slug():
    assert determine_strategy(PLATFORM_AEM, "cme") == STRATEGY_AEM_CME


def test_aem_unassigned_slug_stays_plain_aem():
    """An AEM slug not yet in AEM_BNY_SLUGS/AEM_CME_SLUGS is a known
    platform with no scraper assigned yet -- strategy stays "aem", not an
    error."""
    assert determine_strategy(PLATFORM_AEM, "abbott") == PLATFORM_AEM


def test_resolve_listing_platform_notified_gated_uses_strategy():
    assert _resolve_listing_platform(PLATFORM_NOTIFIED, PLATFORM_NOTIFIED_GATED) \
        == PLATFORM_NOTIFIED_GATED


def test_resolve_listing_platform_otherwise_uses_platform():
    """q4_ir/aem_bny/aem_cme have no separate sources_utils.PLATFORMS
    entry -- resolve_listing_url() must still be handed the base platform
    for these, or it silently loses the listing-path field lookup."""
    assert _resolve_listing_platform(PLATFORM_Q4, STRATEGY_Q4_IR) == PLATFORM_Q4
    assert _resolve_listing_platform(PLATFORM_AEM, STRATEGY_AEM_BNY) == PLATFORM_AEM
    assert _resolve_listing_platform(PLATFORM_AEM, STRATEGY_AEM_CME) == PLATFORM_AEM
    assert _resolve_listing_platform(PLATFORM_NOTIFIED, PLATFORM_NOTIFIED) == PLATFORM_NOTIFIED