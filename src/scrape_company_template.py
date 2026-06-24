#!/usr/bin/env python3
"""
scrape_COMPANY.py  <-- rename this file to match TARGET_SLUG

Scrape COMPANY's investor relations news page and merge results into
primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

To create a new wrapper:
  1. Copy this file to src/scrape_<slug>.py
  2. Set TARGET_SLUG to match the slug in sources/sources.yaml
  3. Set NEWS_PATH if the site uses a different path (rare)
  4. Set FETCH_DETAIL_PAGES:
       False  -- site embeds dates in listing cards (e.g. Costco, NVIDIA)
       True   -- dates only on individual detail pages (e.g. CDW)
     When unsure, start with False and run --dry-run; any items shown in the
     preview with date "????-??-??" mean you need True.
  5. Update the module docstring and Examples below.

Examples:
    python src/scrape_COMPANY.py --dry-run
    python src/scrape_COMPANY.py --year 2025
    python src/scrape_COMPANY.py --show-browser --debug-dump-html /tmp/COMPANY.html --dry-run

All other flags (--year, --start-year, --end-year, --since, --until,
--format, --dry-run, --verbose, etc.) are passed through to scrape_q4_ir.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scrape_q4_ir
from sources_utils import load_source_record

# ---- Configure these three values per company ----
TARGET_SLUG = "CHANGEME"           # must match slug in sources/sources.yaml
NEWS_PATH = "/news/default.aspx"   # override only if the site differs
FETCH_DETAIL_PAGES = False         # True if listing page omits dates (e.g. CDW)
# --------------------------------------------------


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