#!/usr/bin/env python3
"""
scrape_all.py

Orchestrate scraping across all sources configured in config/scraper_config.yaml.

Usage:
    python src/scrape_all.py                                   # current year (default)
    python src/scrape_all.py --year 2024
    python src/scrape_all.py --all-years                       # full history, every source
    python src/scrape_all.py --year 2026 --dry-run
    python src/scrape_all.py --year 2026 --slug cdw            # single source
    python src/scrape_all.py --year 2026 --platform investorroom  # single platform
    python src/scrape_all.py --year 2026 --platform notified --slug abbvie  # both (ANDed)
    python src/scrape_all.py --year 2026 --dry-run -v          # verbose
    python src/scrape_all.py --smoke-test --dry-run            # quick "is anything broken?" check
    python src/scrape_all.py --smoke-test --dry-run --seed 42  # reproducible smoke test
    python src/scrape_all.py --smoke-test --platform notified --dry-run
                                                                 # smoke-test just one platform,
                                                                 # e.g. after touching its scraper

--smoke-test runs one randomly-picked source per distinct (scraper, extra
args, durable source facts) signature instead of every configured source --
see pick_smoke_test_selection() and group_sources_by_signature() below for
why that's the right unit of "category" rather than the YAML group name.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import logging
import random
import sys
import time
from pathlib import Path

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("Missing dependency: pip install ruamel.yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRAPER_CONFIG_PATH = REPO_ROOT / "config" / "scraper_config.yaml"
SOURCES_YAML_PATH = REPO_ROOT / "sources" / "sources.yaml"
SRC_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"

# Needed to import the reporting package below regardless of how this
# module was invoked (`python src/scrape_all.py` already puts src/ on
# sys.path, but tests and other importers may not) -- same pattern
# run_scraper() below uses before importing a scraper module.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from reporting.check_press_release_counts import (  # noqa: E402
    DEFAULT_COUNTS_CSV,
    check_found_release_counts,
    check_release_counts,
)
from utils.scrape_utils import (  # noqa: E402
    count_items_by_year,
    get_last_run_items,
    reset_last_run_items,
)

logger = logging.getLogger("scrape_all")


def load_scraper_config(path: Path = SCRAPER_CONFIG_PATH) -> dict:
    if not path.exists():
        sys.exit(f"scraper_config.yaml not found at {path}")
    yaml = YAML()
    with open(path) as f:
        return yaml.load(f)


def load_sources_lookup(path: Path = SOURCES_YAML_PATH) -> dict[str, dict]:
    """Load sources.yaml into a dict keyed by slug, for cross-referencing
    durable per-source facts (e.g. needs_detail_page_dates) against
    scraper_config.yaml entries."""
    if not path.exists():
        sys.exit(f"sources.yaml not found at {path}")
    yaml = YAML()
    with open(path) as f:
        data = yaml.load(f)
    return {s["slug"]: s for s in data["sources"]}


# Per-scraper-module durable fields (from sources.yaml) that change which
# code path a source exercises, and therefore must factor into its
# smoke-test signature alongside 'args'. Values can be any YAML scalar type
# (bool, str, int, ...) or structured (list/dict) -- add an entry here
# whenever a scraper is taught to branch on a new durable per-source fact,
# regardless of that field's type. No other code needs to change.
DURABLE_SIGNATURE_FIELDS: dict[str, tuple[str, ...]] = {
    "scrape_q4_ir": ("needs_detail_page_dates",),
    # e.g. once a hypothetical scrape_pr_newswire.py branches on a
    # 'release_feed_format' string field in sources.yaml:
    #   "scrape_pr_newswire": ("release_feed_format",),
}


def build_argv(slug: str, year: int | None, extra_args: list[str], dry_run: bool) -> list[str]:
    argv = ["--slug", slug]
    if year is not None:
        argv += ["--year", str(year)]
    argv += extra_args
    if dry_run:
        argv += ["--dry-run"]
    return argv


def run_scraper(module_name: str, argv: list[str]) -> int:
    """Import the scraper module and call its main() directly — no subprocess."""
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    reset_last_run_items()
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        logger.error("Could not import %s: %s", module_name, e)
        return 1
    return mod.main(argv)


def iter_selected_sources(config: dict, platform: str | None, slug: str | None):
    """Yield (group_name, module_name, entry) for every source that matches
    the optional --platform and --slug filters.

    group_name is the platform key in scraper_config.yaml (e.g. 'investorroom');
    module_name is that platform's scraper module (e.g. 'scrape_investorroom').

    If both platform and slug are given, they are ANDed together: a source
    must be under that platform group AND have that slug to be yielded. This
    is mainly useful for disambiguating a slug that happens to exist under
    more than one platform (e.g. --platform notified --slug abbvie). If the
    slug isn't actually under that platform, nothing is yielded for it.
    """
    for group_name, group in config.items():
        if platform and group_name.lower() != platform.lower():
            continue

        module_name = group["scraper"]
        for entry in group["sources"]:
            if slug and entry["slug"].lower() != slug.lower():
                continue
            yield group_name, module_name, entry


Source = tuple[str, str, dict]  # (group_name, module_name, entry) -- same shape iter_selected_sources yields
DurableValue = tuple[str, object]  # (field_name, normalized field value)
Signature = tuple[str, tuple[str, ...], tuple[DurableValue, ...]]
# (module_name, sorted extra-args, durable (field_name, value) pairs in
# DURABLE_SIGNATURE_FIELDS order -- see _normalize_for_signature for why
# values are pre-normalized before landing here.)


def _normalize_for_signature(value: object) -> object:
    """Coerce a sources.yaml field value into something hashable, so any
    scalar, list, or nested-dict field can safely sit inside a Signature
    tuple -- not just the booleans/strings we happen to have today."""
    if isinstance(value, list):
        return tuple(_normalize_for_signature(v) for v in value)
    if isinstance(value, dict):
        return tuple((k, _normalize_for_signature(v)) for k, v in sorted(value.items()))
    return value  # str, int, float, bool, None -- already hashable


def group_sources_by_signature(
    config: dict, sources_lookup: dict[str, dict], platform: str | None = None
) -> dict[Signature, list[Source]]:
    """Group every configured source by its (scraper module, extra args,
    durable source facts) signature.

    This is the grouping --smoke-test samples from, and it's deliberately
    *not* the same as the YAML group name (e.g. 'q4_ir'), and not just
    'args' either. Two sources under the same scraper module but with
    different extra args exercise different code paths and each need their
    own smoke-test coverage; two sources with identical module + args are
    interchangeable for smoke-testing purposes -- *unless* they also differ
    on a durable, per-source fact declared in sources.yaml that the scraper
    branches on (see DURABLE_SIGNATURE_FIELDS), in which case they still
    need separate coverage even though 'args' alone doesn't show it.

    Concretely, in scraper_config.yaml today: costco and coinbase share a
    signature (both pass --fallback-to-visible, neither has a durable fact
    registered for scrape_q4_ir) and are truly interchangeable for
    smoke-testing. cdw carries no 'args' override, but sources.yaml marks it
    with needs_detail_page_dates: true, which DURABLE_SIGNATURE_FIELDS
    registers for scrape_q4_ir -- so cdw gets its own signature, distinct
    from costco/coinbase, reflecting that it actually exercises
    scrape_q4_ir.py's detail-page-fallback branch.

    Args are sorted before hashing so that e.g. [--a, --b] and [--b, --a]
    are treated as the same signature (order doesn't affect which code path
    a boolean flag triggers). Durable field values are normalized via
    _normalize_for_signature so this works regardless of whether a given
    field is a bool, string, number, or structured value -- adding a new
    durable field to a new or existing scraper only requires registering it
    in DURABLE_SIGNATURE_FIELDS, no changes needed here.

    If `platform` is given, only sources under that platform group are
    considered -- this is what lets --smoke-test --platform X scope its
    signature coverage to just X instead of the whole config.

    Returns a dict preserving config order, mapping each signature to the
    list of sources that share it.
    """
    groups: dict[Signature, list[Source]] = {}
    for group_name, module_name, entry in iter_selected_sources(config, platform=platform, slug=None):
        extra_args = tuple(sorted(entry.get("args", [])))

        source_facts = sources_lookup.get(entry["slug"], {})
        durable_fields = DURABLE_SIGNATURE_FIELDS.get(module_name, ())
        durable_values: tuple[DurableValue, ...] = tuple(
            (field, _normalize_for_signature(source_facts.get(field)))
            for field in durable_fields
        )

        signature = (module_name, extra_args, durable_values)
        groups.setdefault(signature, []).append((group_name, module_name, entry))
    return groups


def pick_smoke_test_selection(
    config: dict, sources_lookup: dict[str, dict], rng: random.Random,
    platform: str | None = None,
) -> list[Source]:
    """Pick one representative source per (scraper, args, durable facts) signature.

    Every distinct signature is guaranteed exactly one representative each
    run -- nothing is ever skipped entirely. Within a signature that has
    more than one interchangeable candidate (e.g. costco vs. coinbase),
    the representative is chosen at random via `rng`, so repeated runs
    rotate coverage across siblings instead of always testing the same one.

    `rng` is injected (rather than using the `random` module's global state)
    so callers can pass a seeded random.Random for reproducible picks.

    If `platform` is given, only signatures found under that platform group
    are sampled from -- e.g. --smoke-test --platform notified picks one
    random slug from 'notified' (or, if that platform happens to span more
    than one signature, one per signature within it) instead of touching
    every configured source.
    """
    selection: list[Source] = []
    for signature, candidates in group_sources_by_signature(config, sources_lookup, platform=platform).items():
        chosen = rng.choice(candidates)
        if len(candidates) > 1:
            siblings = ", ".join(entry["slug"] for _, _, entry in candidates)
            logger.debug("signature %s: picked %r from [%s]", signature, chosen[2]["slug"], siblings)
        selection.append(chosen)
    return selection


def check_scraped_release_counts(
    sources: list[Source],
    year: int | None,
    args: argparse.Namespace,
    sources_lookup: dict[str, dict],
    found_counts: dict[tuple[int, str], int],
) -> None:
    """Compare release counts against the baseline snapshot, restricted to
    the (year, slug) pairs *sources* just covered, and log any mismatch.
    Never raises or changes the process exit code -- a mismatch here is a
    signal for a human to look into (new press releases vs. a possible
    scraper regression, see check_press_release_counts.py's module
    docstring), not by itself proof that this run failed.

    Runs in both --dry-run and normal invocations, using whichever count
    source actually reflects that mode:
      - Normal (wet) runs: recompute counts from data/ on disk, via
        check_release_counts() -- the scrapers just wrote there, so
        that's the definitive per-(year, slug) tally.
      - --dry-run: data/ is untouched, so there's nothing there to read
        back. Instead this uses *found_counts*, which main()'s scrape
        loop tallies from each scraper's in-memory results as it goes
        (see get_last_run_items()/count_items_by_year() in
        utils/scrape_utils.py) -- the only count available under
        --dry-run.

    year=None (i.e. --all-years was passed) means every year is in scope
    for the scraped slugs, matching what was actually scraped.

    Factored out of main() as its own function -- rather than inlined --
    so a future regression test (see item 2 in the project's discussion of
    this feature) can drive the same comparison directly against whatever
    (year, slug) pairs and/or found_counts it collects, without needing to
    invoke scrape_all.py as a subprocess just to get this check.
    """
    if args.skip_count_check:
        return

    years = None if year is None else {year}
    slugs = {entry["slug"] for _, _, entry in sources}

    try:
        if args.dry_run:
            ticker_lookup = {
                slug: sources_lookup.get(slug, {}).get("ticker", "") for slug in slugs
            }
            mismatches = check_found_release_counts(
                counts_csv=args.counts_csv, found_counts=found_counts,
                ticker_lookup=ticker_lookup, years=years, slugs=slugs,
            )
        else:
            mismatches = check_release_counts(
                data_dir=DATA_DIR, counts_csv=args.counts_csv, years=years, slugs=slugs,
            )
    except FileNotFoundError as e:
        logger.warning("Skipping release-count check: %s", e)
        return

    mode = "dry-run, in-memory" if args.dry_run else "data/ on disk"
    if not mismatches:
        logger.info("Release-count check (%s): %d slug(s) match the baseline.", mode, len(slugs))
        return

    for m in mismatches:
        logger.warning("release-count check: %s", m.describe())
    logger.warning(
        "release-count check (%s): %d of %d slug(s) differ from %s -- see warnings above. "
        "This can mean new press releases were found, or that scrape_all.py or one of "
        "the underlying scrapers is broken -- investigate before regenerating the "
        "baseline with `invoke press-release-counts`.",
        mode, len(mismatches), len(slugs), args.counts_csv,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int,
                        help="Year to scrape (default: current year)")
    parser.add_argument("--all-years", action="store_true",
                        help="Scrape full history for every source, ignoring --year")
    parser.add_argument("--slug", help="Scrape only this slug (omit for all configured)")
    parser.add_argument("--platform",
                        help="Scrape only sources under this IR platform group in "
                             "scraper_config.yaml, e.g. 'investorroom' (omit for all). "
                             "Combine with --smoke-test to smoke-test just this platform.")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Scrape one randomly-picked source per distinct (scraper, "
                             "extra args, durable source facts) signature, instead of "
                             "every configured source -- a quick 'is anything broken?' "
                             "check. Combine with --platform to scope the signatures "
                             "sampled from to just that platform (e.g. after changing "
                             "code for one platform's scraper). Cannot be combined with "
                             "--slug. Combine with --dry-run to avoid writing data.")
    parser.add_argument("--seed", type=int,
                        help="Random seed for --smoke-test, for reproducible picks")
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run to every scraper")
    parser.add_argument("--between-delay", type=float, default=5.0,
                        help="Seconds to wait between sources (default: 5)")
    parser.add_argument("--skip-count-check", action="store_true",
                        help="Don't compare release counts against "
                             "reports/latest/press_release_counts.csv after running "
                             "(against data/ on disk normally, or against what was "
                             "found in memory under --dry-run, since --dry-run never "
                             "writes to data/).")
    parser.add_argument("--counts-csv", type=Path, default=DEFAULT_COUNTS_CSV,
                        help="Baseline release-counts CSV to check against "
                             f"(default: {DEFAULT_COUNTS_CSV})")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.smoke_test and args.slug:
        parser.error("--smoke-test cannot be combined with --slug")
    if args.seed is not None and not args.smoke_test:
        parser.error("--seed only has an effect together with --smoke-test")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.all_years:
        year = None
    else:
        year = args.year if args.year is not None else datetime.date.today().year

    config = load_scraper_config()
    sources_lookup = load_sources_lookup()
    failures: list[str] = []
    found_counts: dict[tuple[int, str], int] = {}
    ran = 0

    if args.smoke_test:
        sources = pick_smoke_test_selection(
            config, sources_lookup, random.Random(args.seed), platform=args.platform)
        signature_count = len(group_sources_by_signature(config, sources_lookup, platform=args.platform))
        scope = f" in platform %r" % args.platform if args.platform else ""
        logger.info("Smoke test: %d source(s) selected across %d signature group(s)%s: %s",
                    len(sources), signature_count, scope,
                    ", ".join(entry["slug"] for _, _, entry in sources))
    else:
        sources = list(iter_selected_sources(config, args.platform, args.slug))

    for _group_name, module_name, entry in sources:
        slug = entry["slug"]
        extra_args = list(entry.get("args", []))
        scraper_argv = build_argv(slug, year, extra_args, args.dry_run)

        logger.info("=== %s  [%s]  argv: %s ===", slug, module_name, scraper_argv)
        if ran > 0:
            time.sleep(args.between_delay)

        rc = run_scraper(module_name, scraper_argv)
        ran += 1
        if rc != 0:
            logger.error("%s: scraper exited with code %d", slug, rc)
            failures.append(slug)

        for found_year, count in count_items_by_year(get_last_run_items()).items():
            found_counts[(found_year, slug)] = found_counts.get((found_year, slug), 0) + count

    if ran == 0:
        if args.smoke_test:
            if args.platform:
                logger.error(
                    "No sources found in scraper_config.yaml to smoke-test for platform=%r",
                    args.platform,
                )
            else:
                logger.error("No sources found in scraper_config.yaml to smoke-test")
        else:
            logger.error(
                "No matching entries found in scraper_config.yaml for platform=%r slug=%r",
                args.platform, args.slug,
            )
        return 1

    check_scraped_release_counts(sources, year, args, sources_lookup, found_counts)

    if failures:
        logger.error("Failed: %s", ", ".join(failures))
        return 1

    logger.info("Done. %d scraper(s) ran, 0 failures.", ran)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())