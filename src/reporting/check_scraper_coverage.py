#!/usr/bin/env python3
"""
check_scraper_coverage.py

Report how many sources in sources/sources.yaml have an automated scraper
configured in config/scraper_config.yaml, which is treated as the single
source of truth for whether a scraper exists and works.

This is a read-only reporting tool -- it does not scrape anything.

Usage:
    python src/reporting/check_scraper_coverage.py
    python src/reporting/check_scraper_coverage.py -v               # per-source table
    python src/reporting/check_scraper_coverage.py --missing-only    # just the gaps, as CSV
    python src/reporting/check_scraper_coverage.py --strict          # exit 1 if <100%
    python src/reporting/check_scraper_coverage.py --write-reports   # write both report files

Exit status:
    0  always, unless --strict is given and coverage is incomplete or a
       config problem (see below) was found, in which case 1.

Also flags two classes of config problems, since they're easy to introduce
by hand-editing YAML and scrape_all.py won't catch them until run time:
  - a slug in scraper_config.yaml that doesn't exist in sources.yaml (typo,
    or a source that was renamed/removed)
  - a slug configured under more than one scraper group (it would be
    scraped twice by scrape_all.py)

To regenerate reports/latest/scraper_coverage_summary.txt (prose) and
reports/latest/scraper_coverage_missing.csv (CSV, header
"slug,ticker,platform,ir_url") in one pass, use --write-reports (this is
what tasks.py's scraper-coverage task runs):

    python src/reporting/check_scraper_coverage.py --write-reports

--write-reports computes coverage once and writes both files from that
single snapshot, so they can't disagree the way running the script twice
(once plain, once with --missing-only, each redirected to a file) could if
sources.yaml or scraper_config.yaml changed in between the two runs.

For a quick look in the terminal instead of writing files, use the
default (prose summary) or --missing-only (pure CSV, no prose mixed in --
just the header and gap rows) exactly as documented under Usage above.
--write-reports can't be combined with -v/--missing-only.

The platform column is read from reports/latest/ir_platform.csv (produced
separately by detect_ir_platform.py). That file is a snapshot from whenever
it was last regenerated -- it is not recomputed here -- so a slug added to
sources.yaml since then will show platform "unknown" with a warning on
stderr. Run `invoke reports` (or `invoke ir-platform`) first if you want it
current.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.sources_utils import load_sources  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"
SCRAPER_CONFIG_PATH = REPO_ROOT / "config" / "scraper_config.yaml"
IR_PLATFORM_CSV_PATH = REPO_ROOT / "reports" / "latest" / "ir_platform.csv"
REPORTS_LATEST_DIR = REPO_ROOT / "reports" / "latest"
SUMMARY_OUT_PATH = REPORTS_LATEST_DIR / "scraper_coverage_summary.txt"
MISSING_OUT_PATH = REPORTS_LATEST_DIR / "scraper_coverage_missing.csv"


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


def load_platform_map(path: Path = IR_PLATFORM_CSV_PATH) -> pd.DataFrame:
    """Return a (slug, platform) DataFrame read from ir_platform.csv.

    ir_platform.csv is produced by a separate, network-fetching script
    (detect_ir_platform.py) and is not regenerated here, so it can be
    absent or stale relative to sources.yaml. Both cases are handled by
    the caller (missing platform values are left as NaN, filled with
    "unknown", and reported on stderr) rather than treated as fatal --
    this script is documented as read-only/offline and shouldn't be
    blocked on another report being fresh.
    """
    if not path.exists():
        print(
            f"warning: {path} not found -- platform column will be 'unknown' "
            f"for every row. Regenerate it with: "
            f"python src/detect_ir_platform.py --all > {path}",
            file=sys.stderr,
        )
        return pd.DataFrame(columns=["slug", "platform"])
    return pd.read_csv(path, usecols=["slug", "platform"], dtype=str, keep_default_na=False)


def missing_coverage_csv(uncovered: list[dict], platform_map: pd.DataFrame) -> str:
    """Return CSV text (with header) for slug,ticker,platform,ir_url of *uncovered*.

    *uncovered* is a list of sources.yaml records (dicts with at least slug/
    ticker/ir_url). Platform is looked up from *platform_map* by slug; a
    slug with no match (ir_platform.csv missing or stale) gets "unknown"
    and is called out on stderr so the gap is visible instead of silently
    blank.
    """
    df = pd.DataFrame(uncovered, columns=["slug", "ticker", "ir_url"])
    if df.empty:
        # Nothing uncovered -- skip the merge (platform_map may not even
        # have a "slug" column in this case) and emit a header-only CSV.
        return pd.DataFrame(columns=["slug", "ticker", "platform", "ir_url"]).to_csv(
            index=False, lineterminator="\n"
        )
    df = df.merge(platform_map, on="slug", how="left")

    unknown_mask = df["platform"].isna()
    if unknown_mask.any():
        stale_slugs = ", ".join(df.loc[unknown_mask, "slug"])
        print(
            f"warning: no platform data for: {stale_slugs} "
            "(missing from ir_platform.csv, or it predates these entries "
            "in sources.yaml)",
            file=sys.stderr,
        )
    df["platform"] = df["platform"].fillna("unknown")

    df = df[["slug", "ticker", "platform", "ir_url"]]
    return df.to_csv(index=False, lineterminator="\n")


def render_summary(total: int, n_covered: int, pct: float, problems: list[str]) -> str:
    """Render the prose summary block shared by stdout mode and --write-reports.

    Kept as one function so the two never drift apart: the same counts and
    problem list back both scraper_coverage_summary.txt and the plain-stdout
    default output.
    """
    lines = [
        f"Sources in sources.yaml:     {total}",
        f"With automated scraper:      {n_covered} ({pct:.1f}%)",
        f"Without automated scraper:   {total - n_covered}",
    ]
    if problems:
        lines.append("")
        lines.append("Config problems found:")
        for problem in problems:
            lines.append(f"  - {problem}")
    return "\n".join(lines) + "\n"


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
    parser.add_argument(
        "--write-reports", action="store_true",
        help=f"Write both report files in one pass instead of printing to stdout: "
             f"{SUMMARY_OUT_PATH.relative_to(REPO_ROOT)} (prose) and "
             f"{MISSING_OUT_PATH.relative_to(REPO_ROOT)} (CSV). Computes coverage "
             f"once so the two files are guaranteed to reflect the same "
             f"sources.yaml/scraper_config.yaml snapshot, rather than the two "
             f"separate runs `invoke scraper-coverage` used to do. Used by "
             f"tasks.py; for a quick look in the terminal use -v/--missing-only "
             f"instead.",
    )
    parser.add_argument(
        "--ir-platform", metavar="PATH", type=Path, default=IR_PLATFORM_CSV_PATH,
        help=f"Path to ir_platform.csv, used for the platform column in "
             f"missing-coverage CSV output (default: {IR_PLATFORM_CSV_PATH}).",
    )
    args = parser.parse_args(argv)

    if args.write_reports and (args.verbose or args.missing_only):
        parser.error("--write-reports can't be combined with -v/--missing-only")

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

    # Only modes that actually emit the CSV need ir_platform.csv, so only
    # load it (and only warn about it being missing/stale) in those modes.
    # Otherwise a plain summary run would print a spurious warning about a
    # file it never uses.
    needs_platform_map = uncovered and (args.missing_only or args.write_reports)
    platform_map = load_platform_map(args.ir_platform) if needs_platform_map else pd.DataFrame()

    exit_code = 1 if args.strict and (uncovered or problems) else 0

    if args.write_reports:
        # Single pass: both files are built from the same in-memory
        # uncovered/problems computed above, so they can't disagree the way
        # two separate `check_scraper_coverage.py` invocations could if
        # sources.yaml or scraper_config.yaml changed in between.
        REPORTS_LATEST_DIR.mkdir(parents=True, exist_ok=True)
        SUMMARY_OUT_PATH.write_text(render_summary(total, n_covered, pct, problems))
        MISSING_OUT_PATH.write_text(missing_coverage_csv(uncovered, platform_map))
        print(f"wrote {SUMMARY_OUT_PATH.relative_to(REPO_ROOT)} "
              f"and {MISSING_OUT_PATH.relative_to(REPO_ROOT)}")
        return exit_code

    if args.missing_only:
        # Pure CSV, nothing else -- no summary lines, no problems section.
        print(missing_coverage_csv(uncovered, platform_map), end="")
        return exit_code

    if args.verbose:
        print("Per-source coverage:\n")
        for record in sources:
            slug = record.get("slug", "")
            name = record.get("name", "")
            status = "MISSING" if slug in {r["slug"] for r in uncovered} else "covered"
            print(f"  [{status:7}] {slug:28} {name:45} ({describe(slug)})")
        print()

    print(render_summary(total, n_covered, pct, problems), end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())