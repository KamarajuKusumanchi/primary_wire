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

from utils.scrape_utils import NewsItem, dedupe_by_url, finalize_and_output, parse_date  # noqa: E402


def _item(title: str, url: str, d: date | None) -> NewsItem:
    return NewsItem(slug="x", ticker="X", title=title, url=url, publish_date=d)


# ---------------------------------------------------------------------------
# parse_date()
# ---------------------------------------------------------------------------
# Regression coverage added after debugging scrape_notified_gated.py against
# Robinhood: GLOBE NEWSWIRE/PR Newswire datelines abbreviate Jan/Feb (and
# Aug/Sept/Oct/Nov/Dec) with a trailing period ("Feb. 19, 2026"), which
# Python's %b strptime directive rejects outright. Combined with parse_date()
# previously giving up after the *first* regex match instead of trying every
# match in the text, this silently dropped the publish date -- and therefore
# the whole item, once the year filter ran -- for any January/February
# release using this dateline style. Confirmed live against Robinhood's
# investor-relations site: 3 of 16 2026 press releases were dropped this way.

def test_parse_date_handles_abbreviated_month_with_trailing_period():
    d, raw = parse_date("MENLO PARK, Calif. , Jan. 02, 2026 (GLOBE NEWSWIRE) -- ...")
    assert d == date(2026, 1, 2)
    assert raw == "Jan. 02, 2026"

    d, raw = parse_date("MENLO PARK, Calif., Feb. 19, 2026 (GLOBE NEWSWIRE) today reported ...")
    assert d == date(2026, 2, 19)


def test_parse_date_skips_unparseable_match_and_finds_a_later_good_one():
    # The headline mentions a *future* event date in full-month-name format
    # ("February 10, 2026"); the actual dateline is the abbreviated,
    # period-suffixed publish date ("Feb. 10, 2026") appearing earlier in
    # the text. Either being found is "correct" in the sense of returning
    # *a* real 2026 date, but the key regression this guards against is that
    # parse_date() must not return None just because the first match it
    # encounters fails to strptime.
    text = (
        "Robinhood Markets, Inc. to Announce Fourth Quarter and Full Year "
        "2025 Results on February 10, 2026 -- MENLO PARK, Calif., Feb. 10, "
        "2026 (GLOBE NEWSWIRE) -- Today, Robinhood Markets, Inc. announced "
        "that it will release its fourth quarter and full year 2025 "
        "financial results on Tuesday, February 10, 2026, after market close."
    )
    d, raw = parse_date(text)
    assert d == date(2026, 2, 10)


def test_parse_date_still_handles_unabbreviated_and_other_formats():
    # Non-regression: the common cases parse_date already handled correctly
    # must keep working after the fix.
    assert parse_date("MENLO PARK, Calif., July 02, 2026 (GLOBE NEWSWIRE)")[0] == date(2026, 7, 2)
    assert parse_date("NORTH CHICAGO, Ill., June 26, 2026")[0] == date(2026, 6, 26)
    assert parse_date("06/18/2026 some text")[0] == date(2026, 6, 18)
    assert parse_date("2026-06-18 iso date")[0] == date(2026, 6, 18)
    assert parse_date("nothing date-like here")[0] is None


# ---------------------------------------------------------------------------
# parse_date() -- day-first "6 August, 2026" dateline order, e.g. CME
# Group's own press-release listing (cmegroup.com, ".cmeBrowseAllDate"
# cards -- see scrape_aem.py's module docstring). Confirmed against a real
# --debug-dump-html capture of CME's listing page: every card there reads
# "6 August, 2026", not BNY-style "August 6, 2026".
# ---------------------------------------------------------------------------

def test_parse_date_handles_cme_style_day_first_date_with_comma():
    d, raw = parse_date("6 August, 2026")
    assert d == date(2026, 8, 6)
    assert raw == "6 August, 2026"


def test_parse_date_handles_day_first_date_without_comma():
    assert parse_date("6 August 2026")[0] == date(2026, 8, 6)
    assert parse_date("16 December 2026")[0] == date(2026, 12, 16)


def test_parse_date_handles_day_first_date_with_abbreviated_month():
    assert parse_date("6 Aug, 2026")[0] == date(2026, 8, 6)
    assert parse_date("06 Aug 2026")[0] == date(2026, 8, 6)


def test_parse_date_month_first_still_wins_when_both_could_match():
    # The day-first pattern is tried after the month-first one precisely so
    # it can't hijack an ordinary month-first dateline -- e.g. it must not
    # match just the "18, 2026" tail of "Jun 18, 2026" as if "18" were
    # itself a day-first date missing its month.
    assert parse_date("Jun 18, 2026")[0] == date(2026, 6, 18)


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


def test_finalize_and_output_write_false_skips_daily_csv_merge(tmp_path):
    # write=False is what scrape_all.py uses so it can collect every
    # source's items first and merge them in a single batched pass
    # afterward, instead of each source writing to data/ as it finishes
    # (see finalize_and_output()'s docstring for why). Nothing should be
    # written to data_dir in this mode, even though dry_run=False.
    items = [_item("A", "https://e.com/a", date(2026, 1, 1))]
    data_dir = tmp_path / "data"

    filtered = finalize_and_output(
        items,
        years=None, since=None, until=None, limit=None,
        format="csv", output=None, dry_run=False, data_dir=data_dir,
        write=False,
    )

    assert not data_dir.exists()
    assert [i.title for i in filtered] == ["A"]


def test_finalize_and_output_write_false_still_writes_json(tmp_path):
    # write= only governs the daily-CSV merge branch; JSON output (a
    # distinct, single-file output the batched-merge race doesn't apply to)
    # still happens regardless.
    items = [_item("A", "https://e.com/a", date(2026, 1, 1))]
    json_out = tmp_path / "out.json"

    finalize_and_output(
        items,
        years=None, since=None, until=None, limit=None,
        format="json", output=json_out, dry_run=False, data_dir=tmp_path / "data",
        write=False,
    )

    assert json_out.exists()
    assert len(json.loads(json_out.read_text())) == 1


def test_finalize_and_output_write_true_is_the_default(tmp_path):
    # Standalone CLI usage (each scraper's own main()) doesn't pass write=
    # explicitly, so the default must still merge into data/'s daily CSVs,
    # matching behavior before write= was introduced.
    items = [_item("A", "https://e.com/a", date(2026, 1, 1))]
    data_dir = tmp_path / "data"

    finalize_and_output(
        items,
        years=None, since=None, until=None, limit=None,
        format="csv", output=None, dry_run=False, data_dir=data_dir,
    )

    assert (data_dir / "2026" / "2026-01-01.csv").exists()