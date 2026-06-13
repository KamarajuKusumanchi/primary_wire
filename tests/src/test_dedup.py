"""
tests/src/test_dedup.py
-----------------------
Tests for src/dedup.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pandas as pd
from dedup import load_existing, append_new_entries

COLUMNS = ["slug", "ticker", "title", "url", "published_at"]


def make_df(rows):
    return pd.DataFrame(rows, columns=COLUMNS)


# ── load_existing ─────────────────────────────────────────────────────────────

def test_load_existing_returns_empty_if_no_file(tmp_path):
    df = load_existing(tmp_path / "2026-06-01.csv")
    assert df.empty
    assert list(df.columns) == COLUMNS


def test_load_existing_reads_csv(tmp_path):
    path = tmp_path / "2026-06-01.csv"
    make_df([("fedex", "FDX", "Title A", "https://example.com/a", "2026-06-01T08:00:00Z")]).to_csv(path, index=False)

    df = load_existing(path)
    assert len(df) == 1
    assert df.iloc[0]["url"]    == "https://example.com/a"
    assert df.iloc[0]["ticker"] == "FDX"


def test_load_existing_empty_ticker_for_govt_source(tmp_path):
    path = tmp_path / "2026-06-01.csv"
    make_df([("bls", "", "CPI Report", "https://bls.gov/a", "2026-06-01T09:00:00Z")]).to_csv(path, index=False)

    df = load_existing(path)
    assert df.iloc[0]["ticker"] == ""


# ── append_new_entries ────────────────────────────────────────────────────────

def test_append_new_entries_creates_file(tmp_path):
    path   = tmp_path / "2026-06-01.csv"
    new_df = make_df([("fedex", "FDX", "Title A", "https://example.com/a", "2026-06-01T08:00:00Z")])

    added = append_new_entries(new_df, path)

    assert added == 1
    assert path.exists()


def test_append_new_entries_skips_duplicates(tmp_path):
    path = tmp_path / "2026-06-01.csv"
    row  = ("fedex", "FDX", "Title A", "https://example.com/a", "2026-06-01T08:00:00Z")
    make_df([row]).to_csv(path, index=False)

    added = append_new_entries(make_df([row]), path)

    assert added == 0
    assert len(pd.read_csv(path)) == 1


def test_append_new_entries_adds_only_new(tmp_path):
    path     = tmp_path / "2026-06-01.csv"
    existing = make_df([("fedex", "FDX", "Title A", "https://example.com/a", "2026-06-01T08:00:00Z")])
    existing.to_csv(path, index=False)

    new_df = make_df([
        ("fedex", "FDX", "Title A", "https://example.com/a", "2026-06-01T08:00:00Z"),  # duplicate
        ("fedex", "FDX", "Title B", "https://example.com/b", "2026-06-01T09:00:00Z"),  # new
    ])

    added = append_new_entries(new_df, path)

    assert added == 1
    assert len(pd.read_csv(path)) == 2


def test_append_new_entries_returns_zero_when_nothing_new(tmp_path):
    path   = tmp_path / "2026-06-01.csv"
    new_df = make_df([("fedex", "FDX", "Title A", "https://example.com/a", "2026-06-01T08:00:00Z")])
    new_df.to_csv(path, index=False)

    added = append_new_entries(new_df, path)
    assert added == 0
