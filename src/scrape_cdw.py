#!/usr/bin/env python3
"""
scrape_cdw.py

Scrape CDW's investor relations news page and merge results into
primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

Reads the CDW record from sources/sources.yaml (slug = "cdw") to get the
ir_url and ticker, appends /news/default.aspx, then delegates all scraping
and output work to scrape_q4_ir.

CDW's Q4 IR theme does not embed dates in listing-page cards, so
--fetch-detail-pages is on by default here (unlike the generic script where
it is opt-in). Pass --no-fetch-detail-pages to disable it.

Examples:
    # Preview what would be written, without writing anything
    python src/scrape_cdw.py --dry-run

    # Scrape and write to data/YYYY/YYYY-MM-DD.csv
    python src/scrape_cdw.py

    # Scrape a specific year
    python src/scrape_cdw.py --year 2025

    # Debug: show browser window and save rendered HTML
    python src/scrape_cdw.py --show-browser --debug-dump-html /tmp/cdw.html --dry-run

All other flags (--year, --start-year, --end-year, --since, --until,
--format, --dry-run, --verbose, etc.) are passed through to scrape_q4_ir.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sure src/ siblings are importable when running as python src/scrape_cdw.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("Missing dependency. Install with: pip install ruamel.yaml")

import scrape_q4_ir

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"
TARGET_SLUG = "cdw"
NEWS_PATH = "/news/default.aspx"


def load_cdw_record(sources_path: Path) -> dict:
    if not sources_path.exists():
        sys.exit(f"sources.yaml not found at {sources_path}")
    yaml = YAML()
    with open(sources_path) as f:
        data = yaml.load(f)
    for record in data.get("sources", []):
        if record.get("slug", "").lower() == TARGET_SLUG:
            return record
    sys.exit(f"No record with slug '{TARGET_SLUG}' found in {sources_path}")


def build_news_url(ir_url: str) -> str:
    return ir_url.rstrip("/") + NEWS_PATH


def main() -> int:
    record = load_cdw_record(SOURCES_PATH)

    ir_url = record.get("ir_url", "")
    if not ir_url:
        sys.exit(f"sources.yaml CDW record has no ir_url")

    ticker = record.get("ticker", "")
    if not ticker:
        sys.exit(f"sources.yaml CDW record has no ticker")

    news_url = build_news_url(ir_url)

    # Build argv for scrape_q4_ir, injecting CDW-specific values and
    # defaulting --fetch-detail-pages to on (CDW's theme requires it).
    # Any flags the user passed on the command line come after, and
    # argparse last-write-wins means the user can still override everything.
    import argparse

    # Pull --no-fetch-detail-pages out before forwarding, since scrape_q4_ir
    # doesn't know that flag -- we just omit --fetch-detail-pages instead.
    raw_args = sys.argv[1:]
    no_fetch = "--no-fetch-detail-pages" in raw_args
    if no_fetch:
        raw_args = [a for a in raw_args if a != "--no-fetch-detail-pages"]

    injected = [
        "--url", news_url,
        "--slug", TARGET_SLUG,
        "--ticker", ticker,
    ]
    if not no_fetch:
        injected.append("--fetch-detail-pages")

    argv = injected + raw_args
    return scrape_q4_ir.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())