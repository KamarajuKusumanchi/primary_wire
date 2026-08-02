"""
tests/test_check_scraper_config_consistency.py

Covers detect_ir_platform.load_configured_platforms() and
check_scraper_config_consistency() -- the cross-check between
config/scraper_config.yaml (which slugs have a hand-verified scraper for a
given platform) and this run's freshly detected platform for that same
slug (detect_platforms_parallel()'s output).

Three cases matter:
  1. Everything configured agrees with what was detected -> no messages.
  2. A configured slug is detected under a DIFFERENT platform -> flagged
     (this is the "scraper_config.yaml is stale, or detection regressed"
     case).
  3. A configured slug isn't in the detection results at all -> flagged
     separately (this is the "renamed/removed from sources.yaml" case).

Also covers the "q4_ir" (scraper_config.yaml group name) -> "q4" (detected
platform name) translation, via CONFIG_GROUP_TO_PLATFORM, since that's the
one group name in the real config that doesn't match its platform name
1:1 -- a naive comparison against the raw group name would misfire on
every configured Q4 source.

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
# group-name translation (q4_ir -> q4) plus a plain 1:1 group.
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

    got = dict(zip(df["slug"], df["platform"]))
    assert got == {"abbvie": "notified", "amd": "notified", "costco": "q4"}


def test_check_scraper_config_consistency_no_mismatches(tmp_path):
    path = _write_config(tmp_path, SAMPLE_CONFIG)
    result_df = pd.DataFrame({
        "slug": ["abbvie", "amd", "costco", "some-other-uncovered-slug"],
        "platform": ["notified", "notified", "q4", "unknown"],
    })

    assert check_scraper_config_consistency(result_df, path) == []


def test_check_scraper_config_consistency_flags_platform_mismatch(tmp_path):
    path = _write_config(tmp_path, SAMPLE_CONFIG)
    # abbvie is configured as "notified" but this run detected "investis" --
    # e.g. the site migrated to a different IR platform.
    result_df = pd.DataFrame({
        "slug": ["abbvie", "amd", "costco"],
        "platform": ["investis", "notified", "q4"],
    })

    messages = check_scraper_config_consistency(result_df, path)

    assert len(messages) == 1
    assert "abbvie" in messages[0]
    assert "notified" in messages[0]
    assert "investis" in messages[0]


def test_check_scraper_config_consistency_flags_missing_slug(tmp_path):
    path = _write_config(tmp_path, SAMPLE_CONFIG)
    # costco isn't in this run's results at all -- e.g. removed from
    # sources.yaml since scraper_config.yaml was last updated.
    result_df = pd.DataFrame({
        "slug": ["abbvie", "amd"],
        "platform": ["notified", "notified"],
    })

    messages = check_scraper_config_consistency(result_df, path)

    assert len(messages) == 1
    assert "costco" in messages[0]
    assert "not found" in messages[0]


def test_check_scraper_config_consistency_empty_config_returns_no_messages(tmp_path):
    path = _write_config(tmp_path, {})
    result_df = pd.DataFrame({"slug": ["abbvie"], "platform": ["notified"]})

    assert check_scraper_config_consistency(result_df, path) == []