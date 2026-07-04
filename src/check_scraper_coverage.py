#!/usr/bin/env python3
"""
check_scraper_coverage.py

Report how many sources in sources/sources.yaml have an automated scraper
configured in config/scraper_config.yaml, which is treated as the single
source of truth for whether a scraper exists and works.

This is a read-only reporting tool -- it does not scrape anything.

Usage:
    python src/check_scraper_coverage.py
    python src/check_scraper_coverage.py -v              # per-source table
    python src/check_scraper_coverage.py --missing-only   # just the gaps
    python src/check_scraper_coverage.py --strict         # exit 1 if <100%

Exit status:
    0  always, unless --strict is given and coverage is incomplete or a
       config problem (see below) was found, in which case 1.

Also flags two classes of config problems, since they're easy to introduce
by hand-editing YAML and scrape_all.py won't catch them until run time:
  - a slug in scraper_config.yaml that doesn't exist in sources.yaml (typo,
    or a source that was renamed/removed)
  - a slug configured under more than one scraper group (it would be
    scraped twice by scrape_all.py)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("Missing dependency. Install with: pip install ruamel.yaml")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources_utils import load_sources  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"
SCRAPER_CONFIG_PATH = REPO_ROOT / "config" / "scraper_config.yaml"


def load_scraper_config(path: Path = SCRAPER_CONFIG_PATH) -> dict:
    if not path.exists():
        sys.exit(f"scraper_config.yaml not found at {path}")
    yaml = YAML()
    with open(path) as f:
        return yaml.load(f) or {}


def configured_slugs(config: dict) -> tuple[dict[str, list[str]], list[str]]:
    """Return (slug -> [group names it's configured under], problem messages).

    A slug appearing under more than one group is recorded in `problems`
    since scrape_all.py would run it twice.
    """
    problems: list[str] = []
    entries: list[dict] = []
    for group_name, group in config.items():
        for entry in group.get("sources", []):
            slug = entry.get("slug")
            if not slug:
                problems.append(f"config group '{group_name}' has an entry with no slug")
                continue
            entries.append({"slug": slug, "group": group_name})

    if not entries:
        return {}, problems

    # sort=False keeps slugs in first-appearance order, so output/problem
    # ordering matches what a hand-written dict accumulation would give.
    grouped = pd.DataFrame(entries).groupby("slug", sort=False)["group"].apply(list)
    groups_by_slug = grouped.to_dict()

    for slug, group_names in grouped[grouped.apply(len) > 1].items():
        problems.append(
            f"slug '{slug}' is configured under multiple groups: {', '.join(group_names)} "
            "(scrape_all.py would run it more than once)"
        )

    return groups_by_slug, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print a per-source status table")
    parser.add_argument("--missing-only", action="store_true",
                        help="Print only the sources with no scraper coverage")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 if coverage is incomplete or config problems were found")
    args = parser.parse_args(argv)

    sources = load_sources(SOURCES_PATH)
    if not sources:
        sys.exit(f"No sources found in {SOURCES_PATH}")

    config = load_scraper_config()
    groups_by_slug, problems = configured_slugs(config)

    source_slugs = {s["slug"] for s in sources if s.get("slug")}

    # Flag scraper_config.yaml entries that don't match any known source.
    for slug in sorted(groups_by_slug):
        if slug not in source_slugs:
            problems.append(
                f"scraper_config.yaml has slug '{slug}' which is not in sources.yaml"
            )

    covered: list[dict] = []
    uncovered: list[dict] = []
    for record in sources:
        slug = record.get("slug", "")
        if slug in groups_by_slug:
            covered.append(record)
        else:
            uncovered.append(record)

    total = len(sources)
    n_covered = len(covered)
    pct = 100 * n_covered / total if total else 0.0

    def describe(slug: str) -> str:
        if slug in groups_by_slug:
            return f"config: {'/'.join(groups_by_slug[slug])}"
        return "none"

    if args.verbose or args.missing_only:
        rows = uncovered if args.missing_only else sources
        label = "Missing scraper coverage" if args.missing_only else "Per-source coverage"
        print(f"{label}:\n")
        for record in rows:
            slug = record.get("slug", "")
            name = record.get("name", "")
            status = "MISSING" if slug in {r["slug"] for r in uncovered} else "covered"
            print(f"  [{status:7}] {slug:28} {name:45} ({describe(slug)})")
        print()

    print(f"Sources in sources.yaml:     {total}")
    print(f"With automated scraper:      {n_covered} ({pct:.1f}%)")
    print(f"Without automated scraper:   {total - n_covered}")

    if uncovered and not (args.verbose or args.missing_only):
        print("\nUncovered slugs:")
        for record in uncovered:
            print(f"  {record.get('slug', '')}")

    if problems:
        print("\nConfig problems found:")
        for problem in problems:
            print(f"  - {problem}")

    if args.strict and (uncovered or problems):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())