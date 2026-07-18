#!/usr/bin/env python3
"""
Break down the number of press releases per (year, slug, ticker).

Reads every data/YYYY/YYYY-MM-DD.csv file and counts rows per (year, slug,
ticker), where "year" is derived from the publish_date column -- never from
the YYYY in the file's path/name, since a release scraped and filed under
one date could in principle carry a publish_date in a different year (e.g.
scraper backfill, timezone edge cases around New Year's). publish_date is
the golden source of truth for the date.

Output is a CSV with columns: year, slug, ticker, release_count, sorted by
(year, slug, ticker, release_count).

Usage
-----
    python src/reporting/press_release_counts.py
    python src/reporting/press_release_counts.py --data-dir path/to/data
    python src/reporting/press_release_counts.py --out reports/latest/press_release_counts.csv
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_OUT = REPO_ROOT / "reports" / "latest" / "press_release_counts.csv"

# Columns expected in every data/YYYY/YYYY-MM-DD.csv file (see
# src/utils/csv_utils.py:CSV_FIELDS). We only need a subset here.
REQUIRED_COLUMNS = ["slug", "ticker", "publish_date"]

OUTPUT_COLUMNS = ["year", "slug", "ticker", "release_count"]


def find_daily_csv_files(data_dir: Path) -> list[Path]:
    """Return every data/YYYY/YYYY-MM-DD.csv file under data_dir, sorted.

    Sorting isn't required for correctness (we aggregate over all rows
    regardless of read order), but it makes --verbose/debug output and
    error messages easier to follow.
    """
    return sorted(data_dir.glob("*/*.csv"))


def load_press_releases(csv_paths: list[Path]) -> pd.DataFrame:
    """Load and concatenate the given daily CSV files into one DataFrame.

    Only REQUIRED_COLUMNS are kept. Rows with an unparseable or missing
    publish_date are dropped (with a warning to stderr) since a year can't
    be derived from them -- everything else in this module treats
    publish_date as the golden source of the date, so a bad publish_date
    is a bad row, not a fallback-to-filename situation.
    """
    frames = []
    for path in csv_paths:
        df = pd.read_csv(path, usecols=REQUIRED_COLUMNS)
        df["__source_file"] = str(path.relative_to(path.parent.parent.parent))
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=REQUIRED_COLUMNS + ["__source_file"])

    return pd.concat(frames, ignore_index=True)


def add_year_column(df: pd.DataFrame) -> pd.DataFrame:
    """Derive a 'year' column from publish_date, dropping rows that fail to parse.

    This is the one place the "derive year from publish_date, not from the
    file path" rule is enforced.
    """
    parsed = pd.to_datetime(df["publish_date"], errors="coerce")
    bad = df[parsed.isna()]
    if not bad.empty:
        for _, row in bad.iterrows():
            print(
                f"WARNING: skipping row with unparseable publish_date "
                f"({row['publish_date']!r}) in {row['__source_file']} "
                f"(slug={row['slug']!r})",
                file=sys.stderr,
            )

    df = df.loc[parsed.notna()].copy()
    df["year"] = parsed.loc[parsed.notna()].dt.year
    return df


def count_releases_per_year_slug_ticker(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate to one row per (year, slug, ticker) with a release_count column.

    Sorted by (year, slug, ticker, release_count) as required by the report
    spec. In practice (year, slug, ticker) is already a unique key after
    groupby, so release_count only breaks ties for stability if that
    invariant is ever violated.
    """
    counts = (
        df.groupby(["year", "slug", "ticker"], as_index=False)
        .size()
        .rename(columns={"size": "release_count"})
    )
    counts = counts.sort_values(
        by=["year", "slug", "ticker", "release_count"],
        ascending=True,
        kind="stable",
    ).reset_index(drop=True)
    return counts[OUTPUT_COLUMNS]


def build_report(data_dir: Path) -> pd.DataFrame:
    """End-to-end: find CSVs under data_dir -> load -> derive year -> count."""
    csv_paths = find_daily_csv_files(data_dir)
    if not csv_paths:
        print(f"WARNING: no CSV files found under {data_dir}", file=sys.stderr)

    raw = load_press_releases(csv_paths)
    with_year = add_year_column(raw)
    return count_releases_per_year_slug_ticker(with_year)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Directory containing data/YYYY/YYYY-MM-DD.csv files "
        "(default: data/ relative to the repo root)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write CSV to this path instead of stdout "
        f"(e.g. {DEFAULT_OUT.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args()

    if not args.data_dir.exists():
        raise FileNotFoundError(f"data directory not found at {args.data_dir}")

    report = build_report(args.data_dir)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(args.out, index=False)
        print(f"Wrote {len(report)} rows to {args.out}", file=sys.stderr)
    else:
        report.to_csv(sys.stdout, index=False)


if __name__ == "__main__":
    main()