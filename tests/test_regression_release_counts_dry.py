"""
tests/test_regression_release_counts_dry.py

On-demand regression test: scrapes every source configured in
config/scraper_config.yaml (current year, --dry-run) and asserts that the
release_count found for every (year, slug, ticker) combination matches the
baseline snapshot in reports/latest/press_release_counts.csv.

This is the in-memory/--dry-run counterpart to
test_regression_release_counts_wet.py, which exercises the on-disk write
path instead.

This hits real, live IR pages -- the same network calls
`python src/scrape_all.py --dry-run` makes -- so it is slow and
network-dependent. It's marked 'regression' (see the marker registered in
pyproject.toml) and is excluded from a plain `pytest` / `python -m pytest`
run. Run it explicitly with:

    pytest -m regression
    pytest tests/test_regression_release_counts_dry.py -m regression   # this file only

Unlike `scrape_all.py`'s own end-of-run check (check_scraped_release_counts,
which only logs a warning and never fails the run -- a mismatch there can
legitimately just mean new press releases landed), this test's job is the
opposite: fail loudly, with every mismatching (year, slug, ticker) spelled
out, so a scraper regression doesn't go unnoticed. If a mismatch here really
is just new releases, regenerate the baseline (`invoke press-release-counts`)
rather than loosening this test.

Runs with --dry-run so nothing is written to data/ -- this test only reads
data (the baseline CSV) and compares in-memory counts, it doesn't mutate
the repo.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reporting.check_press_release_counts import check_found_release_counts  # noqa: E402
from scrape_all import build_argv, iter_selected_sources, load_scraper_config, run_scraper  # noqa: E402
from utils.scrape_utils import count_items_by_year_slug_ticker, get_last_run_items  # noqa: E402

pytestmark = pytest.mark.regression


def test_release_counts_match_baseline():
    """Scrape every configured source for the current year (--dry-run) and
    compare the resulting per-(year, slug, ticker) release counts against
    reports/latest/press_release_counts.csv. Fails with the full list of
    mismatches if any (year, slug, ticker) disagrees with the baseline.
    """
    year = datetime.date.today().year
    config = load_scraper_config()
    sources = list(iter_selected_sources(config, platform=None, slug=None))
    assert sources, "No sources found in scraper_config.yaml -- nothing to regression-test"

    found_counts: dict[tuple[int, str, str], int] = {}
    scraper_failures: list[str] = []

    for _group_name, module_name, entry in sources:
        slug = entry["slug"]
        extra_args = list(entry.get("args", []))
        argv = build_argv(slug, year, extra_args, dry_run=True)

        rc = run_scraper(module_name, argv)
        if rc != 0:
            scraper_failures.append(f"{slug} ({module_name}) exited with code {rc}")
            continue

        for key, count in count_items_by_year_slug_ticker(get_last_run_items()).items():
            found_counts[key] = found_counts.get(key, 0) + count

    assert not scraper_failures, (
        f"{len(scraper_failures)} scraper(s) failed to run:\n"
        + "\n".join(f"  - {f}" for f in scraper_failures)
    )

    slugs = {entry["slug"] for _, _, entry in sources}
    try:
        mismatches = check_found_release_counts(found_counts=found_counts, years={year}, slugs=slugs)
    except FileNotFoundError as e:
        pytest.fail(f"Baseline counts CSV missing, can't run regression check: {e}")

    if mismatches:
        details = "\n".join(f"  - {m.describe()}" for m in mismatches)
        pytest.fail(
            f"{len(mismatches)} of {len(slugs)} slug(s) have (year, slug, ticker) "
            f"release counts that differ from the baseline for {year}:\n{details}"
        )