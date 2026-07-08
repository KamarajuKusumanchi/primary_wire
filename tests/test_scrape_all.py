"""
tests/test_scrape_all.py

Covers scrape_all.group_sources_by_signature() and
scrape_all.pick_smoke_test_selection() -- the --smoke-test grouping logic.

The key behavior under test: grouping is by (scraper module, extra args,
durable per-source facts registered in DURABLE_SIGNATURE_FIELDS), NOT by
the YAML group name. Two sources in the same YAML group but with different
extra args (e.g. costco's --fallback-to-visible vs. cdw's no-args) are
different signatures and each get their own representative; two sources
with identical extra args *and* identical durable facts (costco, coinbase)
are one signature and share a single, randomly-picked representative; and
cdw -- despite having no 'args' override at all -- still gets its own
signature because sources.yaml marks it with needs_detail_page_dates: true,
which DURABLE_SIGNATURE_FIELDS registers for scrape_q4_ir.

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
# matter for signature grouping: a shared-args pair, a lone entry in the
# same YAML group that's distinguished only by a durable sources.yaml fact
# (not by 'args'), and an unrelated group with its own no-args entries
# (which must NOT merge with other no-args groups).
SAMPLE_CONFIG = {
    "q4_ir": {
        "scraper": "scrape_q4_ir",
        "sources": [
            {"slug": "costco", "args": ["--fallback-to-visible"]},
            {"slug": "cdw"},
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

# Mirrors the shape of sources/sources.yaml, keyed by slug. Only cdw carries
# the durable fact (needs_detail_page_dates) that scrape_all.DURABLE_SIGNATURE_FIELDS
# registers for scrape_q4_ir; everything else is left at the implicit
# default (absent -> None) to confirm that's treated as "same as everyone
# else who doesn't set it", not as its own distinct signature.
SAMPLE_SOURCES_LOOKUP = {
    "costco": {"slug": "costco"},
    "coinbase": {"slug": "coinbase"},
    "cdw": {"slug": "cdw", "needs_detail_page_dates": True},
    "chipotle": {"slug": "chipotle"},
    "axon": {"slug": "axon"},
    "abbvie": {"slug": "abbvie"},
    "amd": {"slug": "amd"},
    "apollo": {"slug": "apollo"},
    "teradyne": {"slug": "teradyne"},
}


def _slugs(sources) -> set[str]:
    return {entry["slug"] for _, _, entry in sources}


def _groups():
    return group_sources_by_signature(SAMPLE_CONFIG, SAMPLE_SOURCES_LOOKUP)


def _pick(seed):
    return pick_smoke_test_selection(SAMPLE_CONFIG, SAMPLE_SOURCES_LOOKUP, random.Random(seed))


def test_same_args_and_same_durable_facts_share_a_signature():
    groups = _groups()
    q4_shared = groups[("scrape_q4_ir", ("--fallback-to-visible",), (("needs_detail_page_dates", None),))]
    assert _slugs(q4_shared) == {"costco", "coinbase"}


def test_durable_fact_alone_earns_its_own_signature_even_with_no_args():
    # cdw shares a YAML group ('q4_ir') with costco/coinbase and has no
    # 'args' override at all, but sources.yaml marks it with
    # needs_detail_page_dates: true, which DURABLE_SIGNATURE_FIELDS
    # registers for scrape_q4_ir -- so it must NOT be grouped with them.
    groups = _groups()
    cdw_group = groups[("scrape_q4_ir", (), (("needs_detail_page_dates", True),))]
    assert _slugs(cdw_group) == {"cdw"}


def test_no_args_groups_dont_merge_across_different_modules():
    # investorroom and notified both have no-args, no-durable-fact entries,
    # but they use different scraper modules, so they must remain separate
    # signatures. Neither module has an entry in DURABLE_SIGNATURE_FIELDS,
    # so their durable-values tuple is simply empty.
    groups = _groups()
    assert _slugs(groups[("scrape_investorroom", (), ())]) == {"chipotle", "axon"}
    assert _slugs(groups[("scrape_notified", (), ())]) == {"abbvie", "amd", "apollo", "teradyne"}


def test_smoke_test_selection_always_includes_singleton_signatures():
    # cdw has no interchangeable sibling, so every seed must include it.
    for seed in range(20):
        assert "cdw" in _slugs(_pick(seed))


def test_smoke_test_selection_picks_exactly_one_per_signature():
    selection = _pick(0)
    assert len(selection) == len(_groups()) == 4

    slugs = _slugs(selection)
    assert "cdw" in slugs
    assert len(slugs & {"costco", "coinbase"}) == 1
    assert len(slugs & {"chipotle", "axon"}) == 1
    assert len(slugs & {"abbvie", "amd", "apollo", "teradyne"}) == 1


def test_smoke_test_selection_is_reproducible_with_same_seed():
    first = _pick(42)
    second = _pick(42)
    assert _slugs(first) == _slugs(second)


def test_smoke_test_selection_rotates_across_seeds():
    # Not a strict guarantee (a sibling could repeat by chance), but with
    # 2 candidates per group and 30 seeds, seeing both costco and coinbase
    # picked confirms the choice is actually randomized and not hardcoded
    # to always return the first candidate.
    picks_seen = set()
    for seed in range(30):
        picks_seen |= (_slugs(_pick(seed)) & {"costco", "coinbase"})
    assert picks_seen == {"costco", "coinbase"}