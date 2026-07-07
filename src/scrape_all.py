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

--smoke-test runs one randomly-picked source per distinct (scraper, extra
args) signature instead of every configured source -- see
pick_smoke_test_selection() below for why that's the right unit of
"category" rather than the YAML group name.
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
SRC_DIR = Path(__file__).resolve().parent

logger = logging.getLogger("scrape_all")


def load_scraper_config(path: Path = SCRAPER_CONFIG_PATH) -> dict:
    if not path.exists():
        sys.exit(f"scraper_config.yaml not found at {path}")
    yaml = YAML()
    with open(path) as f:
        return yaml.load(f)


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
Signature = tuple[str, tuple[str, ...]]  # (module_name, sorted extra-args)


def group_sources_by_signature(config: dict) -> dict[Signature, list[Source]]:
    """Group every configured source by its (scraper module, extra args) signature.

    This is the grouping --smoke-test samples from, and it's deliberately
    *not* the same as the YAML group name (e.g. 'q4_ir'). Two sources under
    the same scraper module but with different extra args exercise different
    code paths and each need their own smoke-test coverage; two sources with
    identical module + args are interchangeable for smoke-testing purposes,
    since either one exercises exactly the same code.

    Concretely, in scraper_config.yaml today: costco and coinbase share a
    signature (both pass --fallback-to-visible) and are truly interchangeable
    for smoke-testing -- testing either one is representative of testing both.

    TODO: cdw no longer carries a distinguishing 'args' entry -- its need for
    detail-page-fetched dates moved to sources.yaml's needs_detail_page_dates
    field (a durable site fact, not a per-run scraper flag) -- so it now
    groups with costco/coinbase here even though it exercises a different
    code path in scrape_q4_ir.py (the detail-page-fallback branch) that they
    never touch. Until this grouping logic is taught to also key off
    sources.yaml fields, cdw risks being randomized away by --smoke-test.
    Tracked as a separate follow-up.

    Args are sorted before hashing so that e.g. [--a, --b] and [--b, --a]
    are treated as the same signature (order doesn't affect which code path
    a boolean flag triggers).

    Returns a dict preserving config order, mapping each signature to the
    list of sources that share it.
    """
    groups: dict[Signature, list[Source]] = {}
    for group_name, module_name, entry in iter_selected_sources(config, platform=None, slug=None):
        signature = (module_name, tuple(sorted(entry.get("args", []))))
        groups.setdefault(signature, []).append((group_name, module_name, entry))
    return groups


def pick_smoke_test_selection(config: dict, rng: random.Random) -> list[Source]:
    """Pick one representative source per (scraper, args) signature.

    Every distinct signature is guaranteed exactly one representative each
    run -- nothing is ever skipped entirely. Within a signature that has
    more than one interchangeable candidate (e.g. costco vs. coinbase),
    the representative is chosen at random via `rng`, so repeated runs
    rotate coverage across siblings instead of always testing the same one.

    `rng` is injected (rather than using the `random` module's global state)
    so callers can pass a seeded random.Random for reproducible picks.
    """
    selection: list[Source] = []
    for signature, candidates in group_sources_by_signature(config).items():
        chosen = rng.choice(candidates)
        if len(candidates) > 1:
            siblings = ", ".join(entry["slug"] for _, _, entry in candidates)
            logger.debug("signature %s: picked %r from [%s]", signature, chosen[2]["slug"], siblings)
        selection.append(chosen)
    return selection


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
                             "scraper_config.yaml, e.g. 'investorroom' (omit for all)")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Scrape one randomly-picked source per distinct (scraper, "
                             "extra args) signature, instead of every configured source "
                             "-- a quick 'is anything broken?' check. Cannot be combined "
                             "with --slug/--platform. Combine with --dry-run to avoid "
                             "writing data.")
    parser.add_argument("--seed", type=int,
                        help="Random seed for --smoke-test, for reproducible picks")
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run to every scraper")
    parser.add_argument("--between-delay", type=float, default=5.0,
                        help="Seconds to wait between sources (default: 5)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.smoke_test and (args.slug or args.platform):
        parser.error("--smoke-test cannot be combined with --slug or --platform")
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
    failures: list[str] = []
    ran = 0

    if args.smoke_test:
        sources = pick_smoke_test_selection(config, random.Random(args.seed))
        signature_count = len(group_sources_by_signature(config))
        logger.info("Smoke test: %d source(s) selected across %d signature group(s): %s",
                    len(sources), signature_count,
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

    if ran == 0:
        if args.smoke_test:
            logger.error("No sources found in scraper_config.yaml to smoke-test")
        else:
            logger.error(
                "No matching entries found in scraper_config.yaml for platform=%r slug=%r",
                args.platform, args.slug,
            )
        return 1

    if failures:
        logger.error("Failed: %s", ", ".join(failures))
        return 1

    logger.info("Done. %d scraper(s) ran, 0 failures.", ran)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())