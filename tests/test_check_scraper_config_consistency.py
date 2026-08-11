"""
tests/test_check_scraper_config_consistency.py

Covers detect_ir_platform.load_configured_platforms() and
check_scraper_config_consistency() -- the cross-check between
config/scraper_config.yaml (which slugs have a hand-verified scraper --
i.e. a *strategy* -- for a given platform) and this run's freshly detected
(platform, strategy) for that same slug (detect_platforms_parallel()'s
output).

See detect_ir_platform.py's "Platform vs. strategy" note (just above
GATED_SLUGS) for the distinction: "platform" is what the page-fingerprint
checks find (q4, notified, aem, ...); "strategy" is which scraper
(src/scrape_*.py) actually handles a slug (q4_ir, notified_gated,
aem_bny, ...) -- a scraper_config.yaml *group name* IS a strategy name.

Three cases matter:
  1. Everything configured agrees with what was detected -> no messages.
  2. A configured slug is detected under a DIFFERENT strategy -> flagged
     (this is the "scraper_config.yaml is stale, or detection regressed"
     case).
  3. A configured slug isn't in the detection results at all -> flagged
     separately (this is the "renamed/removed from sources.yaml" case).

Also covers the "q4_ir" (scraper_config.yaml group/strategy name) -> "q4"
(detected platform name) translation, via STRATEGY_TO_PLATFORM, since
that's one of the group names in the real config that doesn't match its
platform name 1:1 -- a naive platform-only comparison would miss a
mismatch between two *strategies* that happen to share one platform (e.g.
aem_bny vs. aem_cme, both platform "aem").

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reporting.detect_ir_platform import (  # noqa: E402
    check_scraper_config_consistency,
    load_configured_platforms,
)

# Mirrors config/scraper_config.yaml's shape closely enough to exercise the
# group/strategy-name translation (q4_ir -> q4) plus a plain 1:1 group.
SAMPLE_CONFIG = {
    "notified": {
        "scraper": "scrape_notified",
        "sources": [{"slug": "abbvie"}, {"slug": "amd"}],
    },
    "q4_ir": {
        "scraper": "scrape_q4_ir",
        "sources": [{"slug": "costco"}],
    },
}


def _write_config(tmp_path: Path, config: dict) -> Path:
    from ruamel.yaml import YAML

    path = tmp_path / "scraper_config.yaml"
    YAML().dump(config, path.open("w"))
    return path


def test_load_configured_platforms_translates_group_name(tmp_path):
    path = _write_config(tmp_path, SAMPLE_CONFIG)
    df = load_configured_platforms(path)

    # strategy is the raw group name (no translation needed) ...
    got_strategy = dict(zip(df["slug"], df["strategy"]))
    assert got_strategy == {"abbvie": "notified", "amd": "notified", "costco": "q4_ir"}

    # ... platform is that group name translated down via STRATEGY_TO_PLATFORM.
    got_platform = dict(zip(df["slug"], df["platform"]))
    assert got_platform == {"abbvie": "notified", "amd": "notified", "costco": "q4"}


def test_check_scraper_config_consistency_no_mismatches(tmp_path):
    path = _write_config(tmp_path, SAMPLE_CONFIG)
    result_df = pd.DataFrame({
        "slug": ["abbvie", "amd", "costco", "some-other-uncovered-slug"],
        "platform": ["notified", "notified", "q4", "unknown"],
        "strategy": ["notified", "notified", "q4_ir", "unknown"],
    })

    assert check_scraper_config_consistency(result_df, path) == []


def test_check_scraper_config_consistency_flags_strategy_mismatch(tmp_path):
    path = _write_config(tmp_path, SAMPLE_CONFIG)
    # abbvie is configured under strategy "notified" but this run detected
    # "investis" -- e.g. the site migrated to a different IR platform.
    result_df = pd.DataFrame({
        "slug": ["abbvie", "amd", "costco"],
        "platform": ["investis", "notified", "q4"],
        "strategy": ["investis", "notified", "q4_ir"],
    })

    messages = check_scraper_config_consistency(result_df, path)

    assert len(messages) == 1
    assert "abbvie" in messages[0]
    assert "notified" in messages[0]
    assert "investis" in messages[0]


def test_check_scraper_config_consistency_flags_strategy_mismatch_same_platform(tmp_path):
    """Two strategies can share a platform (e.g. aem_bny/aem_cme both
    "aem") -- a platform-only comparison would miss this, so the check
    must compare on strategy."""
    config = {
        "aem_bny": {"scraper": "scrape_aem_bny", "sources": [{"slug": "bny"}]},
    }
    path = _write_config(tmp_path, config)
    # bny is configured under strategy "aem_bny" but this run detected
    # "aem_cme" -- same platform ("aem") either way, so a platform-only
    # comparison would wrongly report no mismatch.
    result_df = pd.DataFrame({
        "slug": ["bny"],
        "platform": ["aem"],
        "strategy": ["aem_cme"],
    })

    messages = check_scraper_config_consistency(result_df, path)

    assert len(messages) == 1
    assert "bny" in messages[0]
    assert "aem_bny" in messages[0]
    assert "aem_cme" in messages[0]


def test_check_scraper_config_consistency_flags_missing_slug(tmp_path):
    path = _write_config(tmp_path, SAMPLE_CONFIG)
    # costco isn't in this run's results at all -- e.g. removed from
    # sources.yaml since scraper_config.yaml was last updated.
    result_df = pd.DataFrame({
        "slug": ["abbvie", "amd"],
        "platform": ["notified", "notified"],
        "strategy": ["notified", "notified"],
    })

    messages = check_scraper_config_consistency(result_df, path)

    assert len(messages) == 1
    assert "costco" in messages[0]
    assert "not found" in messages[0]


def test_check_scraper_config_consistency_empty_config_returns_no_messages(tmp_path):
    path = _write_config(tmp_path, {})
    result_df = pd.DataFrame({
        "slug": ["abbvie"], "platform": ["notified"], "strategy": ["notified"],
    })

    assert check_scraper_config_consistency(result_df, path) == []