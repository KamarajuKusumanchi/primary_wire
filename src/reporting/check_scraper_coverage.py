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
"slug,ticker,platform,scrape_url") in one pass, use --write-reports (this
is what tasks.py's scraper-coverage task runs):

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
from utils.sources_utils import load_sources, resolve_listing_url  # noqa: E402

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


def build_missing_df(uncovered: list[dict], platform_map: pd.DataFrame) -> pd.DataFrame:
    """Return a slug,ticker,platform,scrape_url DataFrame for *uncovered*.

    *uncovered* is a list of sources.yaml records (dicts with at least
    slug/ticker/ir_url, and optionally news_url, news_path,
    news_releases_path). scrape_url is the full press-release *listing*
    URL a scraper would actually fetch for that record -- site root
    (news_url if set, else ir_url) plus the platform-specific listing path
    -- see utils.sources_utils.resolve_listing_url() -- rather than just
    the site root, so a reader can paste this column directly into a
    browser and land on the same listing page the scraper parses. This
    also means it lines up with the platform value next to it, since the
    listing-path field/default resolve_listing_url() uses is chosen by
    that same platform value.

    Platform is looked up from *platform_map* by slug; a slug with no
    match (ir_platform.csv missing or stale) gets "unknown" and is called
    out on stderr so the gap is visible instead of silently blank. Because
    resolve_listing_url() can't guess a listing path for "unknown", those
    rows fall back to showing just the site root.

    This is the single source of truth for "uncovered + platform" used by
    both missing_coverage_csv() and platform_breakdown(), so the two can't
    disagree about which platform a given missing slug got assigned.
    """
    df = pd.DataFrame(
        uncovered,
        columns=["slug", "ticker", "ir_url", "news_url", "news_path", "news_releases_path"],
    )
    if df.empty:
        # Nothing uncovered -- skip the merge (platform_map may not even
        # have a "slug" column in this case) and return an empty frame
        # with the right columns.
        return pd.DataFrame(columns=["slug", "ticker", "platform", "scrape_url"])
    # Records that never set these optional sources.yaml fields come back
    # from pd.DataFrame() as NaN (not "" or missing), and NaN is truthy in
    # Python -- resolve_scrape_url()'s "news_url or ir_url" precedence (and
    # resolve_listing_url()'s "record.get(field_name) or default_path")
    # would then wrongly pick NaN over the real fallback. Normalize to ""
    # first.
    optional_cols = ["ir_url", "news_url", "news_path", "news_releases_path"]
    df[optional_cols] = df[optional_cols].fillna("")
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

    # scrape_url is computed AFTER the platform merge/fillna above, since
    # resolve_listing_url() needs the (possibly stale-fallback) platform
    # value to know which listing-path field/default applies.
    df["scrape_url"] = df.apply(
        lambda r: resolve_listing_url(
            {
                "ir_url": r["ir_url"],
                "news_url": r["news_url"],
                "news_path": r["news_path"],
                "news_releases_path": r["news_releases_path"],
            },
            r["platform"],
        ),
        axis=1,
    )

    return df[["slug", "ticker", "platform", "scrape_url"]]


def missing_coverage_csv(missing_df: pd.DataFrame) -> str:
    """Return CSV text (with header) for a slug,ticker,platform,scrape_url DataFrame.

    *missing_df* is the output of build_missing_df().
    """
    return missing_df.to_csv(index=False, lineterminator="\n")


# scraper_config.yaml group names that don't match the platform name
# detect_ir_platform.py's detect_platform() assigns for the same platform
# (see that module's detect_platform() return values: "investorroom",
# "notified", "notified_gated", "q4", "unknown"). Every group not listed
# here is assumed to already match its platform name 1:1 (true today for
# investorroom, notified, notified_gated).
CONFIG_GROUP_TO_PLATFORM = {
    "q4_ir": "q4",
}

# Platforms that detect_platform() can only ever assign via a curated,
# manually-maintained override list rather than from an actual page
# signal -- see detect_ir_platform.py's GATED_SLUGS docstring: "there's no
# general-purpose signal yet to detect 'gated' automatically". A slug not
# already on that list will be classified under whatever platform it
# would be *without* the override (currently "notified"), never flagged
# as needing the gated variant. So counting "missing" rows tagged with
# one of these platforms doesn't tell you how many uncovered sources
# actually need that variant -- it only tells you how many of the
# already-curated slugs happen to be uncovered, which for a scraper this
# targeted should usually be zero anyway. platform_breakdown() leaves
# these platforms' "missing" cell as NaN rather than 0 for that reason:
# 0 would claim "no missing sources need this", when the honest state is
# "this pipeline can't tell you that".
UNMEASURABLE_MISSING_PLATFORMS = {"notified_gated"}


def platform_breakdown(
    groups_by_slug: dict[str, list[str]],
    covered: list[dict],
    missing_df: pd.DataFrame,
) -> pd.DataFrame:
    """Return a per-platform configured/missing breakdown, indexed by platform.

    "configured" counts *covered* sources.yaml slugs, grouped by the
    scraper_config.yaml group they're configured under (mapped to the
    matching detect_platform() name via CONFIG_GROUP_TO_PLATFORM, e.g.
    "q4_ir" -> "q4"). Every group in scraper_config.yaml is fully
    enumerated, so a platform with no configured sources is a real 0
    here, not a gap in measurement -- "configured" is always filled.

    "missing" counts *missing_df* rows grouped by its already-computed
    "platform" column (see build_missing_df() -- detected from
    ir_platform.csv, "unknown" fallback included). "configured" and
    "missing" come from two different classification schemes -- one is
    "which scraper module is this slug configured under", the other is
    "what did detect_ir_platform.py's site fingerprint find" -- so a
    platform can legitimately appear in one without the other.

    Platforms in UNMEASURABLE_MISSING_PLATFORMS (currently
    "notified_gated") always get NaN for "missing", regardless of what
    (if anything) is in missing_df, since that pipeline has no way to
    positively identify an uncovered source as needing that variant --
    see UNMEASURABLE_MISSING_PLATFORMS's docstring. Every other platform
    with zero rows in missing_df is left as NaN too (rather than filled
    with 0), on the same "don't assert a count you didn't actually
    measure" principle, though in practice this only matters if a new,
    not-yet-detected platform shows up in scraper_config.yaml.
    """
    configured_counts = (
        pd.Series(
            [
                CONFIG_GROUP_TO_PLATFORM.get(group, group)
                for record in covered
                for group in groups_by_slug.get(record.get("slug", ""), [])
            ],
            dtype="object",
        )
        .value_counts()
        .rename("configured")
    )

    if missing_df.empty or "platform" not in missing_df.columns:
        missing_counts = pd.Series(dtype="float64", name="missing")
    else:
        missing_counts = missing_df["platform"].value_counts().rename("missing")

    table = pd.concat([configured_counts, missing_counts], axis=1)
    # "configured" is safe to fill with 0: scraper_config.yaml fully
    # enumerates its groups, so absence really does mean zero. "missing"
    # is deliberately left as NaN wherever it wasn't populated above --
    # see the docstring above and UNMEASURABLE_MISSING_PLATFORMS.
    table["configured"] = table["configured"].fillna(0).astype(int)
    for platform in UNMEASURABLE_MISSING_PLATFORMS:
        if platform in table.index:
            table.loc[platform, "missing"] = pd.NA

    # "total" and "percentage_done" treat a NaN "missing" as 0 -- i.e.
    # "assume none missing until proven otherwise" -- even though the
    # "missing" column itself stays blank for those rows (see above). This
    # means total/percentage_done are answering a slightly different,
    # more optimistic question than "missing" is: "how done are we,
    # assuming nothing currently unmeasurable turns out to be missing"
    # rather than "how done are we, for certain". Worth remembering when
    # reading notified_gated's 100%: it's a best-case assumption, not a
    # verified one, since detect_ir_platform.py's GATED_SLUGS override
    # (see UNMEASURABLE_MISSING_PLATFORMS's docstring) has no way to
    # surface an actual missing gated source if one exists.
    missing_filled = table["missing"].fillna(0)
    table["total"] = table["configured"] + missing_filled
    table["percentage_done"] = (100 * table["configured"] / table["total"]).round(1)
    table = table[["total", "configured", "missing", "percentage_done"]]

    return table.sort_values("missing", ascending=False, na_position="last")


def add_total_row(table: pd.DataFrame, label: str = "TOTAL") -> pd.DataFrame:
    """Return *table* with a final row summing "configured" and "missing".

    "missing" is summed with skipna=True (pandas' default), so a platform
    left NaN there (see platform_breakdown()) contributes nothing to the
    total -- which is correct, not a gap: an unmeasurable-platform slug
    that's actually uncovered is already counted under whatever platform
    it *was* detected as (see UNMEASURABLE_MISSING_PLATFORMS's docstring),
    so it's already in another row's "missing" and shouldn't be added
    again here. That's also why this total is expected to tie out to the
    top-of-report "Sources in sources.yaml" / "With automated scraper" /
    "Without automated scraper" figures -- it's a useful sanity check
    that the platform breakdown accounts for every source exactly once.
    """
    configured_sum = table["configured"].sum()
    missing_sum = table["missing"].sum()  # skipna=True by default
    total_sum = configured_sum + missing_sum
    pct_sum = round(100 * configured_sum / total_sum, 1) if total_sum else 0.0
    totals = pd.DataFrame(
        [[total_sum, configured_sum, missing_sum, pct_sum]],
        columns=["total", "configured", "missing", "percentage_done"],
        index=[label],
    )
    return pd.concat([table, totals])


def render_platform_breakdown(table: pd.DataFrame) -> str:
    """Render platform_breakdown()'s (optionally add_total_row()'d) table.

    A blank "missing"/"total"/"percentage_done" cell means "not
    measurable from current data" (see platform_breakdown()'s docstring)
    -- deliberately distinct from a "0" cell, which means "measured, and
    the count/percentage is zero".
    """
    header = (
        f"  {'platform':<16}{'total':>8}{'configured':>12}"
        f"{'missing':>10}{'percentage_done':>17}"
    )
    lines = ["", "Coverage by platform:", "", header]
    for platform, row in table.iterrows():
        total_display = "" if pd.isna(row["total"]) else str(int(row["total"]))
        missing_display = "" if pd.isna(row["missing"]) else str(int(row["missing"]))
        pct_display = "" if pd.isna(row["percentage_done"]) else f"{row['percentage_done']:.1f}%"
        lines.append(
            f"  {platform:<16}{total_display:>8}{int(row['configured']):>12}"
            f"{missing_display:>10}{pct_display:>17}"
        )
    return "\n".join(lines) + "\n"


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

    # Every mode except --missing-only ends up printing/writing render_summary()
    # output, and that output now includes the per-platform breakdown, so
    # platform_map (and therefore missing_df, built from it) is needed
    # everywhere there's anything uncovered -- not just --missing-only /
    # --write-reports as before.
    needs_platform_map = bool(uncovered)
    platform_map = load_platform_map(args.ir_platform) if needs_platform_map else pd.DataFrame()
    missing_df = build_missing_df(uncovered, platform_map)
    breakdown = add_total_row(platform_breakdown(groups_by_slug, covered, missing_df))

    exit_code = 1 if args.strict and (uncovered or problems) else 0

    if args.write_reports:
        # Single pass: both files are built from the same in-memory
        # uncovered/problems/missing_df computed above, so they can't
        # disagree the way two separate `check_scraper_coverage.py`
        # invocations could if sources.yaml or scraper_config.yaml changed
        # in between.
        REPORTS_LATEST_DIR.mkdir(parents=True, exist_ok=True)
        summary_text = render_summary(total, n_covered, pct, problems) + render_platform_breakdown(breakdown)
        SUMMARY_OUT_PATH.write_text(summary_text)
        MISSING_OUT_PATH.write_text(missing_coverage_csv(missing_df))
        print(f"wrote {SUMMARY_OUT_PATH.relative_to(REPO_ROOT)} "
              f"and {MISSING_OUT_PATH.relative_to(REPO_ROOT)}")
        return exit_code

    if args.missing_only:
        # Pure CSV, nothing else -- no summary lines, no problems section.
        print(missing_coverage_csv(missing_df), end="")
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
    print(render_platform_breakdown(breakdown), end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())