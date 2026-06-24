#!/usr/bin/env python3
"""
scrape_costco.py

Scrape Costco's investor relations news page and merge results into
primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

Reads the Costco record from sources/sources.yaml (slug = "costco") to get the
ir_url and ticker, appends /news/default.aspx, then delegates all scraping
and output work to scrape_q4_ir.

Costco's Q4 IR theme embeds dates in listing-page cards, so detail-page
fetches are not needed. Pass --fetch-detail-pages to enable them anyway.

Examples:
    # Preview what would be written, without writing anything
    python src/scrape_costco.py --dry-run

    # Scrape and write to data/YYYY/YYYY-MM-DD.csv
    python src/scrape_costco.py

    # Scrape a specific year
    python src/scrape_costco.py --year 2025

    # Debug: show browser window and save rendered HTML
    python src/scrape_costco.py --show-browser --debug-dump-html /tmp/costco.html --dry-run

All other flags (--year, --start-year, --end-year, --since, --until,
--format, --dry-run, --verbose, etc.) are passed through to scrape_q4_ir.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scrape_q4_ir
from sources_utils import load_source_record

TARGET_SLUG = "costco"
NEWS_PATH = "/news/default.aspx"
# Costco's Q4 theme embeds dates in listing cards, so detail-page fetches
# are not needed to resolve them. Set to True for themes that omit dates.
FETCH_DETAIL_PAGES = False


def main() -> int:
    record = load_source_record(TARGET_SLUG)

    ir_url = record.get("ir_url", "")
    if not ir_url:
        sys.exit(f"sources.yaml '{TARGET_SLUG}' record has no ir_url")
    ticker = record.get("ticker", "")
    if not ticker:
        sys.exit(f"sources.yaml '{TARGET_SLUG}' record has no ticker")

    news_url = ir_url.rstrip("/") + NEWS_PATH

    # Strip --no-fetch-detail-pages before forwarding (scrape_q4_ir doesn't
    # know that flag; we just omit --fetch-detail-pages instead).
    raw_args = sys.argv[1:]
    no_fetch = "--no-fetch-detail-pages" in raw_args
    if no_fetch:
        raw_args = [a for a in raw_args if a != "--no-fetch-detail-pages"]

    injected = ["--url", news_url, "--slug", TARGET_SLUG, "--ticker", ticker]
    if FETCH_DETAIL_PAGES and not no_fetch:
        injected.append("--fetch-detail-pages")

    return scrape_q4_ir.main(injected + raw_args)


if __name__ == "__main__":
    raise SystemExit(main())
