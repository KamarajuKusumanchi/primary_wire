#!/usr/bin/env python3
"""
scrape_cdw.py

Scrape CDW's investor relations news page and merge results into
primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

Reads the CDW record from sources/sources.yaml (slug = "cdw") to get the
ir_url and ticker, appends /news/default.aspx, then delegates all scraping
and output work to scrape_q4_ir.

CDW's Q4 IR theme does not embed dates in listing-page cards. sources.yaml
marks the "cdw" record with needs_detail_page_dates: true, so scrape_q4_ir
enables detail-page fetching by default for this source automatically (no
--fetch-detail-pages needed here). Pass --no-fetch-detail-pages to disable it.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scrape_q4_ir
from utils.sources_utils import join_url_path, load_source_record

TARGET_SLUG = "cdw"
NEWS_PATH = "/news/default.aspx"


def main() -> int:
    record = load_source_record(TARGET_SLUG)

    ir_url = record.get("ir_url", "")
    if not ir_url:
        sys.exit(f"sources.yaml '{TARGET_SLUG}' record has no ir_url")
    ticker = record.get("ticker", "")
    if not ticker:
        sys.exit(f"sources.yaml '{TARGET_SLUG}' record has no ticker")

    news_url = join_url_path(ir_url, NEWS_PATH)

    # --fetch-detail-pages / --no-fetch-detail-pages, if present, are forwarded
    # as-is: scrape_q4_ir.py understands both directly and, absent either,
    # falls back to this record's needs_detail_page_dates field on its own
    # (via --slug cdw below), so no special-casing is needed here.
    raw_args = sys.argv[1:]
    injected = ["--url", news_url, "--slug", TARGET_SLUG, "--ticker", ticker]

    return scrape_q4_ir.main(injected + raw_args)


if __name__ == "__main__":
    raise SystemExit(main())