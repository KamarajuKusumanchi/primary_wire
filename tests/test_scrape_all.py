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

import argparse
import random
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import scrape_all  # noqa: E402
from scrape_all import (  # noqa: E402
    check_scraped_release_counts,
    group_sources_by_signature,
    pick_smoke_test_selection,
)

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

# ---------------------------------------------------------------------------
# check_scraped_release_counts()
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["slug", "ticker", "title", "url", "publish_date", "publish_time"]


def _write_daily_csv(data_dir: Path, date_str: str, rows: list[dict]) -> None:
    year = date_str.split("-")[0]
    day_dir = data_dir / year
    day_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=CSV_COLUMNS).to_csv(day_dir / f"{date_str}.csv", index=False)


def _release_row(slug: str, ticker: str, publish_date: str) -> dict:
    return {
        "slug": slug, "ticker": ticker, "title": f"{slug} release",
        "url": f"https://example.com/{slug}", "publish_date": publish_date, "publish_time": "",
    }


def _fake_sources(*slugs: str) -> list[scrape_all.Source]:
    return [("group", "module", {"slug": slug}) for slug in slugs]


# --- wet-run (disk-based) path -----------------------------------------------

def test_check_scraped_release_counts_wet_run_logs_no_mismatch_when_counts_match(
    tmp_path, monkeypatch, caplog
):
    data_dir = tmp_path / "data"
    counts_csv = tmp_path / "press_release_counts.csv"
    _write_daily_csv(data_dir, "2026-01-05", [_release_row("abbvie", "ABBV", "2026-01-05")])
    pd.DataFrame(
        [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 1}]
    ).to_csv(counts_csv, index=False)
    monkeypatch.setattr(scrape_all, "DATA_DIR", data_dir)

    args = argparse.Namespace(
        dry_run=False, skip_count_check=False, counts_csv=counts_csv, data_dir=data_dir,
    )
    with caplog.at_level("INFO", logger="scrape_all"):
        check_scraped_release_counts(_fake_sources("abbvie"), 2026, args, {})

    assert "1 slug(s) match the baseline" in caplog.text
    assert "WARNING" not in caplog.text


def test_check_scraped_release_counts_wet_run_warns_on_mismatch(tmp_path, monkeypatch, caplog):
    data_dir = tmp_path / "data"
    counts_csv = tmp_path / "press_release_counts.csv"
    _write_daily_csv(data_dir, "2026-01-05", [_release_row("abbvie", "ABBV", "2026-01-05")])
    pd.DataFrame(
        [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 5}]
    ).to_csv(counts_csv, index=False)
    monkeypatch.setattr(scrape_all, "DATA_DIR", data_dir)

    args = argparse.Namespace(
        dry_run=False, skip_count_check=False, counts_csv=counts_csv, data_dir=data_dir,
    )
    with caplog.at_level("WARNING", logger="scrape_all"):
        check_scraped_release_counts(_fake_sources("abbvie"), 2026, args, {})

    assert "baseline=5, actual=1" in caplog.text


def test_check_scraped_release_counts_wet_run_handles_missing_baseline_gracefully(
    tmp_path, monkeypatch, caplog
):
    data_dir = tmp_path / "data"
    _write_daily_csv(data_dir, "2026-01-05", [_release_row("abbvie", "ABBV", "2026-01-05")])
    monkeypatch.setattr(scrape_all, "DATA_DIR", data_dir)

    args = argparse.Namespace(
        dry_run=False, skip_count_check=False,
        counts_csv=tmp_path / "does_not_exist.csv", data_dir=data_dir,
    )
    with caplog.at_level("WARNING", logger="scrape_all"):
        check_scraped_release_counts(_fake_sources("abbvie"), 2026, args, {})

    assert "Skipping release-count check" in caplog.text


# --- dry-run (in-memory found_counts) path -----------------------------------
#
# found_counts is keyed by (year, slug, ticker) -- the same triple
# check_release_counts() compares on for wet runs -- since scrape_all.py's
# scrape loop tallies it from each item's own slug/ticker (see
# utils.scrape_utils.count_items_by_year_slug_ticker()) rather than a
# separate slug->ticker lookup.

def test_check_scraped_release_counts_dry_run_logs_no_mismatch_when_counts_match(
    tmp_path, caplog
):
    counts_csv = tmp_path / "press_release_counts.csv"
    pd.DataFrame(
        [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 3}]
    ).to_csv(counts_csv, index=False)

    args = argparse.Namespace(dry_run=True, skip_count_check=False, counts_csv=counts_csv)
    found_counts = {(2026, "abbvie", "ABBV"): 3}
    with caplog.at_level("INFO", logger="scrape_all"):
        check_scraped_release_counts(_fake_sources("abbvie"), 2026, args, found_counts)

    assert "dry-run" in caplog.text
    assert "1 slug(s) match the baseline" in caplog.text
    assert "WARNING" not in caplog.text


def test_check_scraped_release_counts_dry_run_warns_on_mismatch(tmp_path, caplog):
    counts_csv = tmp_path / "press_release_counts.csv"
    pd.DataFrame(
        [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 5}]
    ).to_csv(counts_csv, index=False)

    args = argparse.Namespace(dry_run=True, skip_count_check=False, counts_csv=counts_csv)
    # Only 2 found in memory this dry run, vs. a baseline of 5.
    found_counts = {(2026, "abbvie", "ABBV"): 2}
    with caplog.at_level("WARNING", logger="scrape_all"):
        check_scraped_release_counts(_fake_sources("abbvie"), 2026, args, found_counts)

    assert "baseline=5, actual=2" in caplog.text


def test_check_scraped_release_counts_dry_run_flags_a_slug_with_nothing_found(
    tmp_path, caplog
):
    # e.g. the scraper's selector broke and it silently returned zero items.
    counts_csv = tmp_path / "press_release_counts.csv"
    pd.DataFrame(
        [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 5}]
    ).to_csv(counts_csv, index=False)

    args = argparse.Namespace(dry_run=True, skip_count_check=False, counts_csv=counts_csv)
    with caplog.at_level("WARNING", logger="scrape_all"):
        check_scraped_release_counts(_fake_sources("abbvie"), 2026, args, {})

    assert "none were found at all this run" in caplog.text


def test_check_scraped_release_counts_dry_run_handles_missing_baseline_gracefully(
    tmp_path, caplog
):
    args = argparse.Namespace(
        dry_run=True, skip_count_check=False, counts_csv=tmp_path / "does_not_exist.csv",
    )
    with caplog.at_level("WARNING", logger="scrape_all"):
        check_scraped_release_counts(
            _fake_sources("abbvie"), 2026, args, {(2026, "abbvie", "ABBV"): 3},
        )

    assert "Skipping release-count check" in caplog.text


def test_check_scraped_release_counts_dry_run_uses_items_own_ticker_not_a_lookup(
    tmp_path, caplog
):
    # Regression test: the dry-run tally must key on each item's own
    # ticker (as scraped), not some other slug->ticker source that could
    # be stale relative to the baseline. abbvie's baseline ticker (ABBV)
    # and what was actually found (ABBV2, e.g. a relisting mid-run) don't
    # match, so this must surface as a real (year, slug, ticker) mismatch
    # -- not be silently coerced into "counts match" by a lookup that
    # overrides the found ticker with the old one.
    counts_csv = tmp_path / "press_release_counts.csv"
    pd.DataFrame(
        [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 3}]
    ).to_csv(counts_csv, index=False)

    args = argparse.Namespace(dry_run=True, skip_count_check=False, counts_csv=counts_csv)
    found_counts = {(2026, "abbvie", "ABBV2"): 3}
    with caplog.at_level("WARNING", logger="scrape_all"):
        check_scraped_release_counts(_fake_sources("abbvie"), 2026, args, found_counts)

    assert "isn't in the baseline yet" in caplog.text
    assert "none were found at all this run" in caplog.text


# --- shared behavior -----------------------------------------------------

def test_check_scraped_release_counts_skipped_when_flag_set(tmp_path, caplog):
    args = argparse.Namespace(
        dry_run=False, skip_count_check=True, counts_csv=tmp_path / "press_release_counts.csv",
    )
    with caplog.at_level("DEBUG", logger="scrape_all"):
        check_scraped_release_counts(_fake_sources("abbvie"), 2026, args, {})

    assert caplog.text == ""


# --- last-run item tracking wired into main()'s scrape loop ------------------

def test_run_scraper_resets_last_run_items_before_each_call(monkeypatch):
    # A module whose main() never calls finalize_and_output() (e.g. it
    # errors out early) must not leave the previous source's items sitting
    # around for count_items_by_year_slug_ticker(get_last_run_items()) to pick up.
    from utils import scrape_utils

    calls = []

    class _FakeModule:
        @staticmethod
        def main(argv):
            calls.append(argv)
            if len(calls) == 1:
                scrape_utils._last_run_items = [
                    _fake_item("abbvie", "2026-01-05"),
                    _fake_item("abbvie", "2026-01-06"),
                ]
                return 0
            # second call: this scraper errors out before reaching
            # finalize_and_output() at all, so nothing should be recorded.
            return 1

    monkeypatch.setattr(scrape_all.importlib, "import_module", lambda name: _FakeModule)

    scrape_all.run_scraper("scrape_investorroom", ["--slug", "abbvie"])
    assert len(scrape_utils.get_last_run_items()) == 2

    scrape_all.run_scraper("scrape_investorroom", ["--slug", "nike"])
    assert scrape_utils.get_last_run_items() == []


def _fake_item(slug: str, publish_date: str):
    from datetime import date as _date
    from utils.scrape_utils import NewsItem

    y, m, d = (int(p) for p in publish_date.split("-"))
    return NewsItem(slug=slug, ticker="", title="t", url="u", publish_date=_date(y, m, d))