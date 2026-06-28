#!/usr/bin/env python3
"""
scrape_all.py

Orchestrate scraping across all sources configured in config/scraper_config.yaml.

Usage:
    python src/scrape_all.py --year 2026
    python src/scrape_all.py --year 2026 --dry-run
    python src/scrape_all.py --year 2026 --slug cdw      # single source
    python src/scrape_all.py --year 2026 --dry-run -v    # verbose
"""

from __future__ import annotations

import argparse
import importlib
import logging
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int, help="Year to scrape (omit for site default)")
    parser.add_argument("--slug", help="Scrape only this slug (omit for all configured)")
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run to every scraper")
    parser.add_argument("--between-delay", type=float, default=5.0,
                        help="Seconds to wait between sources (default: 5)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_scraper_config()
    failures: list[str] = []
    ran = 0

    for group_name, group in config.items():
        module_name = group["scraper"]
        for entry in group["sources"]:
            slug = entry["slug"]
            if args.slug and slug.lower() != args.slug.lower():
                continue

            extra_args = list(entry.get("args", []))
            scraper_argv = build_argv(slug, args.year, extra_args, args.dry_run)

            logger.info("=== %s  [%s]  argv: %s ===", slug, module_name, scraper_argv)
            if ran > 0:
                time.sleep(args.between_delay)

            rc = run_scraper(module_name, scraper_argv)
            ran += 1
            if rc != 0:
                logger.error("%s: scraper exited with code %d", slug, rc)
                failures.append(slug)

    if ran == 0:
        logger.error("No matching entries found in scraper_config.yaml for slug=%r", args.slug)
        return 1

    if failures:
        logger.error("Failed: %s", ", ".join(failures))
        return 1

    logger.info("Done. %d scraper(s) ran, 0 failures.", ran)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())