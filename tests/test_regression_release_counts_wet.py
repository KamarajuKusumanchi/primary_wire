"""
tests/test_regression_release_counts_wet.py

On-demand regression test for scrape_all.py's normal (non-dry-run, "wet")
mode: scrapes every source configured in config/scraper_config.yaml for the
current year, writing to a throwaway data/ directory (never the repo's real
data/), then recomputes per-(year, slug, ticker) release counts from those
on-disk CSVs -- exactly the way scrape_all.py's own check_release_counts()
does after a real `python src/scrape_all.py` run -- and asserts they match
the baseline snapshot in reports/latest/press_release_counts.csv.

This is the on-disk-path counterpart to test_regression_release_counts_dry.py
(which exercises the --dry-run/in-memory path). The two deliberately
overlap in what they scrape and check; the point of *this* file is to also
exercise the merge-into-daily-CSVs write path (utils/csv_utils.py) and the
disk-based re-read (check_release_counts() / press_release_counts.build_report()),
neither of which a --dry-run run ever touches.

Like the dry-run version, this hits real, live IR pages, so it's slow and
network-dependent, and is excluded from a plain `pytest` run via the
'regression' marker registered in pyproject.toml:

    pytest -m regression
    pytest tests/test_regression_release_counts_wet.py -m regression   # this file only

Redirecting writes to a throwaway data/ directory: every scraper module
now takes its own --data-dir CLI flag (each still defaults to writing
through its own module-level DATA_DIR constant, but --data-dir overrides
it), so this test just passes data_dir=... through to build_argv() and
lets it append "--data-dir <tmp_path>/data" to each scraper's argv. That
keeps this test from ever writing real scraped files into the repo's own
data/ directory (data/ is curated output, not a test fixture).
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reporting.check_press_release_counts import check_release_counts  # noqa: E402
from scrape_all import build_argv, iter_selected_sources, load_scraper_config, run_scraper  # noqa: E402

pytestmark = pytest.mark.regression


def test_release_counts_match_baseline_on_disk(tmp_path):
    """Scrape every configured source for the current year in normal (wet)
    mode -- writing to a throwaway data/ directory -- then recompute
    per-(year, slug, ticker) release counts from those on-disk CSVs and
    compare against reports/latest/press_release_counts.csv. Fails with the
    full list of mismatches if any (year, slug, ticker) disagrees with the
    baseline.
    """
    year = datetime.date.today().year
    config = load_scraper_config()
    sources = list(iter_selected_sources(config, platform=None, slug=None))
    assert sources, "No sources found in scraper_config.yaml -- nothing to regression-test"

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    scraper_failures: list[str] = []

    for _group_name, module_name, entry in sources:
        slug = entry["slug"]
        extra_args = list(entry.get("args", []))
        # Every scraper module's --data-dir flag redirects its writes to
        # the throwaway dir -- see the module docstring above.
        argv = build_argv(slug, year, extra_args, dry_run=False, data_dir=data_dir)

        rc = run_scraper(module_name, argv)
        if rc != 0:
            scraper_failures.append(f"{slug} ({module_name}) exited with code {rc}")

    assert not scraper_failures, (
        f"{len(scraper_failures)} scraper(s) failed to run:\n"
        + "\n".join(f"  - {f}" for f in scraper_failures)
    )

    slugs = {entry["slug"] for _, _, entry in sources}
    try:
        mismatches = check_release_counts(data_dir=data_dir, years={year}, slugs=slugs)
    except FileNotFoundError as e:
        pytest.fail(f"Baseline counts CSV missing, can't run regression check: {e}")

    if mismatches:
        details = "\n".join(f"  - {m.describe()}" for m in mismatches)
        pytest.fail(
            f"{len(mismatches)} of {len(slugs)} slug(s) have (year, slug, ticker) "
            f"release counts that differ from the baseline for {year} "
            f"(on-disk check, scraped data written under {data_dir}):\n{details}"
        )