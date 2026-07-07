"""
tests/test_scrape_all.py

Covers scrape_all.group_sources_by_signature() and
scrape_all.pick_smoke_test_selection() -- the --smoke-test grouping logic.

The key behavior under test: grouping is by (scraper module, extra args),
NOT by the YAML group name. Two sources in the same YAML group but with
different extra args (e.g. cdw's --fetch-detail-pages vs. costco's
--fallback-to-visible) are different signatures and each get their own
representative; two sources with identical extra args (costco, coinbase)
are one signature and share a single, randomly-picked representative.

Run with:
    uv run pytest
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scrape_all import group_sources_by_signature, pick_smoke_test_selection  # noqa: E402

# Mirrors the shape of config/scraper_config.yaml, trimmed to the cases that
# matter for signature grouping: a shared-args pair, a lone distinct-args
# entry in the same YAML group, and an unrelated group with its own
# no-args entries (which must NOT merge with other no-args groups).
SAMPLE_CONFIG = {
    "q4_ir": {
        "scraper": "scrape_q4_ir",
        "sources": [
            {"slug": "costco", "args": ["--fallback-to-visible"]},
            {"slug": "cdw", "args": ["--fetch-detail-pages"]},
            {"slug": "coinbase", "args": ["--fallback-to-visible"]},
        ],
    },
    "investorroom": {
        "scraper": "scrape_investorroom",
        "sources": [
            {"slug": "chipotle"},
            {"slug": "axon"},
        ],
    },
    "notified": {
        "scraper": "scrape_notified",
        "sources": [
            {"slug": "abbvie"},
            {"slug": "amd"},
            {"slug": "apollo"},
            {"slug": "teradyne"},
        ],
    },
}


def _slugs(sources) -> set[str]:
    return {entry["slug"] for _, _, entry in sources}


def test_same_args_within_same_module_share_a_signature():
    groups = group_sources_by_signature(SAMPLE_CONFIG)
    q4_shared = groups[("scrape_q4_ir", ("--fallback-to-visible",))]
    assert _slugs(q4_shared) == {"costco", "coinbase"}


def test_distinct_args_within_same_yaml_group_get_their_own_signature():
    # cdw shares a YAML group ('q4_ir') with costco/coinbase, but has
    # different args, so it must NOT be grouped with them.
    groups = group_sources_by_signature(SAMPLE_CONFIG)
    cdw_group = groups[("scrape_q4_ir", ("--fetch-detail-pages",))]
    assert _slugs(cdw_group) == {"cdw"}


def test_no_args_groups_dont_merge_across_different_modules():
    # investorroom and notified both have no-args entries, but they use
    # different scraper modules, so they must remain separate signatures.
    groups = group_sources_by_signature(SAMPLE_CONFIG)
    assert _slugs(groups[("scrape_investorroom", ())]) == {"chipotle", "axon"}
    assert _slugs(groups[("scrape_notified", ())]) == {"abbvie", "amd", "apollo", "teradyne"}


def test_smoke_test_selection_always_includes_singleton_signatures():
    # cdw has no interchangeable sibling, so every seed must include it.
    for seed in range(20):
        selection = pick_smoke_test_selection(SAMPLE_CONFIG, random.Random(seed))
        assert "cdw" in _slugs(selection)


def test_smoke_test_selection_picks_exactly_one_per_signature():
    selection = pick_smoke_test_selection(SAMPLE_CONFIG, random.Random(0))
    assert len(selection) == len(group_sources_by_signature(SAMPLE_CONFIG)) == 4

    slugs = _slugs(selection)
    assert "cdw" in slugs
    assert len(slugs & {"costco", "coinbase"}) == 1
    assert len(slugs & {"chipotle", "axon"}) == 1
    assert len(slugs & {"abbvie", "amd", "apollo", "teradyne"}) == 1


def test_smoke_test_selection_is_reproducible_with_same_seed():
    first = pick_smoke_test_selection(SAMPLE_CONFIG, random.Random(42))
    second = pick_smoke_test_selection(SAMPLE_CONFIG, random.Random(42))
    assert _slugs(first) == _slugs(second)


def test_smoke_test_selection_rotates_across_seeds():
    # Not a strict guarantee (a sibling could repeat by chance), but with
    # 2-4 candidates per group and 30 seeds, seeing more than one distinct
    # costco/coinbase pick confirms the choice is actually randomized and
    # not hardcoded to always return the first candidate.
    picks_seen = set()
    for seed in range(30):
        selection = pick_smoke_test_selection(SAMPLE_CONFIG, random.Random(seed))
        picks_seen |= (_slugs(selection) & {"costco", "coinbase"})
    assert picks_seen == {"costco", "coinbase"}