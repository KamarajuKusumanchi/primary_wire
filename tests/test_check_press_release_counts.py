"""
tests/test_check_press_release_counts.py

Covers src/reporting/check_press_release_counts.py: comparing actual
release counts (recomputed from data/YYYY/YYYY-MM-DD.csv files) against
the reports/latest/press_release_counts.csv baseline.

Run with:
    uv run pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from reporting.check_press_release_counts import (  # noqa: E402
    check_release_counts,
    load_baseline,
)

CSV_COLUMNS = ["slug", "ticker", "title", "url", "publish_date", "publish_time"]


def _write_daily_csv(data_dir: Path, date_str: str, rows: list[dict]) -> None:
    year = date_str.split("-")[0]
    day_dir = data_dir / year
    day_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=CSV_COLUMNS)
    df.to_csv(day_dir / f"{date_str}.csv", index=False)


def _write_baseline(path: Path, rows: list[dict]) -> None:
    pd.DataFrame(rows, columns=["year", "slug", "ticker", "release_count"]).to_csv(path, index=False)


def _release_row(slug: str, ticker: str, publish_date: str, n: int) -> dict:
    return {
        "slug": slug, "ticker": ticker, "title": f"{slug} release {n}",
        "url": f"https://example.com/{slug}/{n}", "publish_date": publish_date,
        "publish_time": "",
    }


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def counts_csv(tmp_path: Path) -> Path:
    return tmp_path / "press_release_counts.csv"


def test_matching_counts_produce_no_mismatches(data_dir, counts_csv):
    _write_daily_csv(data_dir, "2026-01-05", [
        _release_row("abbvie", "ABBV", "2026-01-05", 1),
        _release_row("abbvie", "ABBV", "2026-01-05", 2),
    ])
    _write_baseline(counts_csv, [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 2}])

    mismatches = check_release_counts(data_dir=data_dir, counts_csv=counts_csv)
    assert mismatches == []


def test_increased_count_is_reported_as_a_mismatch(data_dir, counts_csv):
    _write_daily_csv(data_dir, "2026-01-05", [
        _release_row("abbvie", "ABBV", "2026-01-05", 1),
        _release_row("abbvie", "ABBV", "2026-01-05", 2),
        _release_row("abbvie", "ABBV", "2026-01-06", 3),
    ])
    _write_baseline(counts_csv, [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 2}])

    mismatches = check_release_counts(data_dir=data_dir, counts_csv=counts_csv)
    assert len(mismatches) == 1
    m = mismatches[0]
    assert (m.year, m.slug, m.baseline_count, m.actual_count) == (2026, "abbvie", 2, 3)
    assert m.diff == 1
    assert "new releases" in m.describe()


def test_missing_data_is_reported_as_a_mismatch(data_dir, counts_csv):
    # Baseline expects releases for a slug that has no data/ rows at all --
    # e.g. a scraper that silently returned nothing.
    _write_daily_csv(data_dir, "2026-01-05", [
        _release_row("abbvie", "ABBV", "2026-01-05", 1),
    ])
    _write_baseline(counts_csv, [
        {"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 1},
        {"year": 2026, "slug": "nike", "ticker": "NKE", "release_count": 5},
    ])

    mismatches = check_release_counts(data_dir=data_dir, counts_csv=counts_csv)
    assert len(mismatches) == 1
    m = mismatches[0]
    assert (m.slug, m.baseline_count, m.actual_count) == ("nike", 5, None)
    assert "scraper regression" in m.describe()


def test_new_slug_not_in_baseline_is_reported_as_a_mismatch(data_dir, counts_csv):
    _write_daily_csv(data_dir, "2026-01-05", [
        _release_row("abbvie", "ABBV", "2026-01-05", 1),
        _release_row("newco", "NEW", "2026-01-05", 1),
    ])
    _write_baseline(counts_csv, [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 1}])

    mismatches = check_release_counts(data_dir=data_dir, counts_csv=counts_csv)
    assert len(mismatches) == 1
    m = mismatches[0]
    assert (m.slug, m.baseline_count, m.actual_count) == ("newco", None, 1)
    assert "isn't in the baseline yet" in m.describe()


def test_years_and_slugs_restrict_the_comparison(data_dir, counts_csv):
    # abbvie's 2025 count is broken, but restricting the check to 2026
    # should ignore that entirely.
    _write_daily_csv(data_dir, "2025-06-01", [_release_row("abbvie", "ABBV", "2025-06-01", 1)])
    _write_daily_csv(data_dir, "2026-01-05", [
        _release_row("abbvie", "ABBV", "2026-01-05", 1),
        _release_row("nike", "NKE", "2026-01-05", 1),
    ])
    _write_baseline(counts_csv, [
        {"year": 2025, "slug": "abbvie", "ticker": "ABBV", "release_count": 99},
        {"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 1},
        {"year": 2026, "slug": "nike", "ticker": "NKE", "release_count": 1},
    ])

    mismatches = check_release_counts(
        data_dir=data_dir, counts_csv=counts_csv, years={2026}, slugs={"abbvie"},
    )
    assert mismatches == []


def test_ticker_change_on_same_slug_is_reported_as_two_mismatches(data_dir, counts_csv):
    # abbvie's release count is unchanged, but its ticker in the baseline
    # is stale (e.g. it was relisted). Keying on (year, slug, ticker)
    # means this must NOT be silently treated as "counts match" -- it's
    # exactly the kind of drift the user asked to catch.
    _write_daily_csv(data_dir, "2026-01-05", [
        _release_row("abbvie", "ABBV2", "2026-01-05", 1),
        _release_row("abbvie", "ABBV2", "2026-01-05", 2),
    ])
    _write_baseline(counts_csv, [{"year": 2026, "slug": "abbvie", "ticker": "ABBV", "release_count": 2}])

    mismatches = check_release_counts(data_dir=data_dir, counts_csv=counts_csv)
    assert len(mismatches) == 2

    by_ticker = {m.ticker: m for m in mismatches}
    assert by_ticker["ABBV"].baseline_count == 2
    assert by_ticker["ABBV"].actual_count is None
    assert by_ticker["ABBV2"].baseline_count is None
    assert by_ticker["ABBV2"].actual_count == 2


def test_missing_baseline_file_raises_file_not_found(data_dir, tmp_path):
    with pytest.raises(FileNotFoundError):
        load_baseline(tmp_path / "does_not_exist.csv")


def test_missing_baseline_file_raises_from_check_release_counts(data_dir, tmp_path):
    _write_daily_csv(data_dir, "2026-01-05", [_release_row("abbvie", "ABBV", "2026-01-05", 1)])
    with pytest.raises(FileNotFoundError):
        check_release_counts(data_dir=data_dir, counts_csv=tmp_path / "does_not_exist.csv")