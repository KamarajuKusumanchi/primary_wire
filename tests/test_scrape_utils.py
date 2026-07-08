"""
tests/test_scrape_utils.py

Covers utils/scrape_utils.py's dedupe_by_url() and finalize_and_output() --
the shared main()-tail logic extracted from scrape_investorroom.py,
scrape_notified.py, and scrape_q4_ir.py so a new platform scraper doesn't
have to reimplement it.

finalize_and_output() specifically standardizes three behaviors that used
to differ across the three scrapers (see its docstring):
  1. Always preview, regardless of --dry-run.
  2. --format both writes BOTH csv and json (it used to silently behave
     like plain csv in two of the three scrapers).
  3. --format json with no --output falls back to a caller-supplied
     default path instead of being a hard CLI error.

Run with:
    uv run pytest
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils.scrape_utils import NewsItem, dedupe_by_url, finalize_and_output  # noqa: E402


def _item(title: str, url: str, d: date | None) -> NewsItem:
    return NewsItem(slug="x", ticker="X", title=title, url=url, publish_date=d)


def test_dedupe_by_url_keeps_first_occurrence_and_ignores_trailing_slash():
    items = [
        _item("B", "https://e.com/b", date(2026, 1, 2)),
        _item("A first", "https://e.com/a", date(2026, 1, 1)),
        _item("A dup, trailing slash", "https://e.com/a/", date(2026, 1, 1)),
    ]
    deduped = dedupe_by_url(items)
    assert [i.title for i in deduped] == ["B", "A first"]


def test_finalize_and_output_format_both_writes_csv_and_json(tmp_path, capsys):
    items = [
        _item("A", "https://e.com/a", date(2026, 1, 1)),
        _item("B", "https://e.com/b", date(2026, 1, 2)),
    ]
    data_dir = tmp_path / "data"
    json_out = tmp_path / "out.json"

    finalize_and_output(
        items,
        years=None, since=None, until=None, limit=None,
        format="both", output=json_out, dry_run=False, data_dir=data_dir,
    )

    payload = json.loads(json_out.read_text())
    assert len(payload) == 2

    csv_files = sorted(p.name for p in data_dir.rglob("*.csv"))
    assert csv_files == ["2026-01-01.csv", "2026-01-02.csv"]


def test_finalize_and_output_json_without_output_uses_default_path(tmp_path):
    items = [_item("A", "https://e.com/a", date(2026, 1, 1))]
    default_path = tmp_path / "default.json"

    finalize_and_output(
        items,
        years=None, since=None, until=None, limit=None,
        format="json", output=None, dry_run=False, data_dir=tmp_path / "data",
        default_json_path=default_path,
    )

    assert default_path.exists()
    assert len(json.loads(default_path.read_text())) == 1


def test_finalize_and_output_json_without_output_or_default_raises(tmp_path):
    items = [_item("A", "https://e.com/a", date(2026, 1, 1))]

    with pytest.raises(SystemExit):
        finalize_and_output(
            items,
            years=None, since=None, until=None, limit=None,
            format="json", output=None, dry_run=False, data_dir=tmp_path / "data",
        )


def test_finalize_and_output_always_previews_even_without_dry_run(tmp_path, capsys):
    # Regression guard: scrape_investorroom.py used to only call
    # print_preview() when --dry-run was passed; scrape_notified.py and
    # scrape_q4_ir.py always did. finalize_and_output() standardizes on
    # "always preview" for every scraper.
    items = [_item("A", "https://e.com/a", date(2026, 1, 1))]

    finalize_and_output(
        items,
        years=None, since=None, until=None, limit=None,
        format="csv", output=None, dry_run=False, data_dir=tmp_path / "data",
    )

    out = capsys.readouterr().out
    assert "A" in out
    assert "https://e.com/a" in out
