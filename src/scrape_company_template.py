#!/usr/bin/env python3
"""
scrape_COMPANY.py  <-- rename this file to match TARGET_SLUG

Scrape COMPANY's investor relations news page and merge results into
primary_wire's daily data/YYYY/YYYY-MM-DD.csv files.

To create a new wrapper:
  1. Copy this file to src/scrape_<slug>.py
  2. Set TARGET_SLUG to match the slug in sources/sources.yaml
  3. Set NEWS_PATH if the site uses a different path (rare)
  4. If the site's listing page omits dates from its cards (e.g. CDW), add
       needs_detail_page_dates: true
     to this company's record in sources/sources.yaml -- do NOT set a
     FETCH_DETAIL_PAGES constant here; that field is a durable fact about the
     site, so it belongs in sources.yaml, not in this wrapper. When unsure,
     leave it unset and run --dry-run; any items shown in the preview with
     date "????-??-??" mean you need to add the field.
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
from utils.sources_utils import join_url_path, load_source_record

# ---- Configure these two values per company ----
TARGET_SLUG = "CHANGEME"           # must match slug in sources/sources.yaml
NEWS_PATH = "/news/default.aspx"   # override only if the site differs
# --------------------------------------------------
# Whether detail-page date fetching is needed (e.g. CDW) is read from this
# source's needs_detail_page_dates field in sources.yaml, not set here --
# see step 4 above.


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
    # (via --slug below), so no special-casing is needed here.
    raw_args = sys.argv[1:]
    injected = ["--url", news_url, "--slug", TARGET_SLUG, "--ticker", ticker]

    return scrape_q4_ir.main(injected + raw_args)


if __name__ == "__main__":
    raise SystemExit(main())