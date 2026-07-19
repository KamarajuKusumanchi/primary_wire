#!/usr/bin/env python3
"""
check_press_release_counts.py

Compare the release counts actually found in data/YYYY/YYYY-MM-DD.csv
against the last known-good snapshot in
reports/latest/press_release_counts.csv, per (year, slug, ticker).

This is a read-only diagnostic (see src/reporting/__init__.py's module
docstring) -- it never writes to data/, sources.yaml, or
config/scraper_config.yaml. It only *reads* data/YYYY/YYYY-MM-DD.csv files
(via press_release_counts.build_report) and the baseline CSV, and reports
where the two disagree.

A mismatch is not automatically a bug: release_count legitimately goes up
whenever a tracked company issues a new press release. But it can also
change (up, down, or land on a suspiciously different number) because
scrape_all.py's own logic, or one of the underlying per-platform scrapers,
is broken -- a selector stopped matching, pagination silently truncated, a
date failed to parse and got dropped, etc. A bare count can't distinguish
those causes from each other, so this script doesn't try to; it just flags
every (year, slug, ticker) whose actual count no longer matches the
recorded baseline, so a human can look at what changed and decide whether
reports/latest/press_release_counts.csv needs regenerating (via
`invoke press-release-counts`) or whether something needs fixing first.

scrape_all.py calls check_release_counts() directly after a non-dry-run
scrape (restricted to the (year, slug) pairs it just touched), and
check_found_release_counts() after a --dry-run one, since --dry-run never
writes to data/ so there's nothing on disk yet to recompute counts from --
instead scrape_all.py tallies what each scraper actually returned in
memory and passes that tally in directly. This module is also runnable
standalone (disk-based only) for an ad-hoc, unrestricted check of
everything currently on disk against the baseline.

Usage
-----
    # Check everything in data/ against the baseline:
    python src/reporting/check_press_release_counts.py

    # Restrict the check, e.g. to mirror a `scrape_all.py --year 2026
    # --slug abbvie` run:
    python src/reporting/check_press_release_counts.py --year 2026 --slug abbvie

    # Point at a different data dir or baseline file (mainly for tests):
    python src/reporting/check_press_release_counts.py \\
        --data-dir /tmp/data --counts-csv /tmp/press_release_counts.csv

Exit status: 0 if every checked (year, slug, ticker) matches the baseline,
1 if any mismatch (or missing baseline file) was found.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# See docs/reporting.txt: scripts under src/reporting/ that need something
# from src/ (here, src/reporting/press_release_counts.py's own build_report())
# add src/ to sys.path first rather than relying on the caller having done so.
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from reporting.press_release_counts import (  # noqa: E402
    DEFAULT_DATA_DIR,
    OUTPUT_COLUMNS,
    build_report,
)

DEFAULT_COUNTS_CSV = REPO_ROOT / "reports" / "latest" / "press_release_counts.csv"

# Same shape as press_release_counts.OUTPUT_COLUMNS -- the baseline CSV is
# just a saved snapshot of that script's own output.
BASELINE_COLUMNS = OUTPUT_COLUMNS


@dataclass(frozen=True)
class CountMismatch:
    """One (year, slug, ticker) whose actual release_count disagrees with
    the baseline, or that only appears on one side.

    ticker is part of the comparison key (not just carried along for
    display): slug and ticker can each be changed independently over time
    (a company rebrands its slug, or changes ticker on a listing change),
    so they aren't reliably consistent with each other across snapshots.
    Keying on all three means a ticker change on an otherwise-unchanged
    slug shows up as its own pair of mismatches (old (year, slug, ticker)
    missing from actual, new one missing from baseline) rather than being
    silently absorbed into a plain count difference.
    """

    year: int
    slug: str
    ticker: str
    baseline_count: Optional[int]  # None if this (year, slug, ticker) isn't in the baseline yet
    actual_count: Optional[int]  # None if nothing was found for (year, slug, ticker)

    @property
    def diff(self) -> int:
        return (self.actual_count or 0) - (self.baseline_count or 0)

    def describe(self) -> str:
        if self.baseline_count is None:
            return (
                f"{self.slug} ({self.ticker}) {self.year}: {self.actual_count} release(s) found, "
                "but (year, slug, ticker) isn't in the baseline yet -- looks new (or the ticker "
                "changed); run `invoke press-release-counts` once the count looks right"
            )
        if self.actual_count is None:
            return (
                f"{self.slug} ({self.ticker}) {self.year}: baseline expects "
                f"{self.baseline_count}, but none were found at all this run -- "
                "check for a scraper regression"
            )
        sign = "+" if self.diff > 0 else ""
        note = "new releases, most likely" if self.diff > 0 else "check for a scraper regression"
        return (
            f"{self.slug} ({self.ticker}) {self.year}: baseline={self.baseline_count}, "
            f"actual={self.actual_count} ({sign}{self.diff}) -- {note}"
        )


def load_baseline(path: Path) -> pd.DataFrame:
    """Load reports/latest/press_release_counts.csv (or an override path)."""
    if not path.exists():
        raise FileNotFoundError(f"baseline counts CSV not found at {path}")
    df = pd.read_csv(path)
    missing = set(BASELINE_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing expected column(s): {sorted(missing)}")
    return df[BASELINE_COLUMNS]


def filter_keys(
    df: pd.DataFrame, years: Optional[set[int]], slugs: Optional[set[str]]
) -> pd.DataFrame:
    """Restrict a (year, slug, ticker, release_count) frame to the given
    years/slugs -- e.g. the subset scrape_all.py actually ran this time.

    None on either argument means "no restriction on that dimension".
    """
    if years is not None:
        df = df[df["year"].isin(years)]
    if slugs is not None:
        df = df[df["slug"].isin(slugs)]
    return df


def compare_counts(baseline: pd.DataFrame, actual: pd.DataFrame) -> list[CountMismatch]:
    """Outer-join baseline and actual on (year, slug, ticker) and return
    every row where they disagree -- including a (year, slug, ticker) that
    only appears on one side (new since the baseline was taken, missing
    entirely from fresh data, or a slug/ticker pairing that changed
    between the two snapshots)."""
    merged = pd.merge(
        baseline,
        actual,
        on=["year", "slug", "ticker"],
        how="outer",
        suffixes=("_baseline", "_actual"),
    )

    mismatches: list[CountMismatch] = []
    for row in merged.itertuples(index=False):
        baseline_count = (
            None if pd.isna(row.release_count_baseline) else int(row.release_count_baseline)
        )
        actual_count = (
            None if pd.isna(row.release_count_actual) else int(row.release_count_actual)
        )
        if baseline_count == actual_count:
            continue

        mismatches.append(
            CountMismatch(
                year=int(row.year),
                slug=row.slug,
                ticker=row.ticker,
                baseline_count=baseline_count,
                actual_count=actual_count,
            )
        )

    mismatches.sort(key=lambda m: (m.year, m.slug, m.ticker))
    return mismatches


def check_release_counts(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    counts_csv: Path = DEFAULT_COUNTS_CSV,
    years: Optional[set[int]] = None,
    slugs: Optional[set[str]] = None,
) -> list[CountMismatch]:
    """End-to-end: load the baseline, recompute actual counts from
    data_dir, restrict both to years/slugs if given, and return every
    (year, slug, ticker) where they disagree.

    years=None / slugs=None means "check everything found on each side" --
    what this module's own CLI does for an ad-hoc, unrestricted check.
    scrape_all.py instead passes the specific years/slugs it just scraped,
    so the check reflects only what that run touched, not the whole
    project's history.

    This is the *disk-based* check: it reads data/YYYY/YYYY-MM-DD.csv
    files, so it only reflects reality after a non-dry-run scrape actually
    wrote to them. For --dry-run, use check_found_release_counts() instead.

    Raises FileNotFoundError if counts_csv doesn't exist -- callers that
    want to treat a missing baseline as "skip the check" (e.g. before the
    file has ever been generated) should catch that themselves.
    """
    baseline = filter_keys(load_baseline(counts_csv), years, slugs)
    actual = filter_keys(build_report(data_dir), years, slugs)
    return compare_counts(baseline, actual)


def actual_counts_from_found_items(
    found_counts: dict[tuple[int, str], int], ticker_lookup: dict[str, str]
) -> pd.DataFrame:
    """Build a (year, slug, ticker, release_count) frame from in-memory
    found-item counts, e.g. scrape_all.py's --dry-run tally of what each
    scraper actually returned -- there's nothing written to data/ in that
    mode for build_report() to read back instead.

    found_counts maps (year, slug) -> count. ticker_lookup maps slug ->
    ticker, purely for display in CountMismatch.describe(); a slug missing
    from it just shows an empty ticker rather than raising.
    """
    rows = [
        {"year": year, "slug": slug, "ticker": ticker_lookup.get(slug, ""), "release_count": count}
        for (year, slug), count in found_counts.items()
    ]
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def check_found_release_counts(
    *,
    counts_csv: Path = DEFAULT_COUNTS_CSV,
    found_counts: dict[tuple[int, str], int],
    ticker_lookup: dict[str, str],
    years: Optional[set[int]] = None,
    slugs: Optional[set[str]] = None,
) -> list[CountMismatch]:
    """Like check_release_counts(), but compares the baseline against
    in-memory found-item counts instead of recomputing counts from
    data/ -- the --dry-run counterpart, since --dry-run never writes to
    data/ so there'd be nothing on disk yet to read back.

    Raises FileNotFoundError if counts_csv doesn't exist, same as
    check_release_counts().
    """
    baseline = filter_keys(load_baseline(counts_csv), years, slugs)
    actual = filter_keys(actual_counts_from_found_items(found_counts, ticker_lookup), years, slugs)
    return compare_counts(baseline, actual)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
        help=f"Root of the data/ tree to compute actual counts from (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--counts-csv", type=Path, default=DEFAULT_COUNTS_CSV,
        help=f"Baseline counts CSV to compare against (default: {DEFAULT_COUNTS_CSV})",
    )
    parser.add_argument(
        "--year", type=int, action="append", dest="years",
        help="Restrict the check to this year (repeatable). Default: every year present.",
    )
    parser.add_argument(
        "--slug", action="append", dest="slugs",
        help="Restrict the check to this slug (repeatable). Default: every slug present.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    years = set(args.years) if args.years else None
    slugs = set(args.slugs) if args.slugs else None

    try:
        mismatches = check_release_counts(
            data_dir=args.data_dir, counts_csv=args.counts_csv, years=years, slugs=slugs
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not mismatches:
        print("All release counts match the baseline.")
        return 0

    print(f"{len(mismatches)} (year, slug, ticker) mismatch(es) found:", file=sys.stderr)
    for m in mismatches:
        print(f"  {m.describe()}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())