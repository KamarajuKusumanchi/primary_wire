#!/usr/bin/env python3
"""
sources_utils.py

Shared utilities for reading sources/sources.yaml.

Imported by get_source.py, update_source.py, scrape_q4_ir.py, and any
company-specific scraper wrappers (scrape_cdw.py, scrape_costco.py, ...).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("Missing dependency. Install with: pip install ruamel.yaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"


def load_sources(sources_path: Path = SOURCES_PATH) -> list[dict]:
    """Return all records from sources.yaml as a list of dicts.

    Preserves YAML comments and ordering via ruamel.yaml round-trip mode,
    so callers that write the file back (e.g. update_source.py) won't mangle
    it. Read-only callers can ignore that detail.
    """
    if not sources_path.exists():
        sys.exit(f"sources.yaml not found at {sources_path}")
    yaml = YAML()
    with open(sources_path) as f:
        data = yaml.load(f)
    return data.get("sources", [])


def find_source(sources: list[dict], query: str) -> Optional[dict]:
    """Return the first record matching *query* as a slug or ticker.

    *query* is matched case-insensitively. Returns None if not found.
    """
    q = query.strip().lower()
    for record in sources:
        if record.get("slug", "").lower() == q:
            return record
        if record.get("ticker", "").lower() == q:
            return record
    return None


def find_source_by_ir_url(sources: list[dict], url: str) -> Optional[dict]:
    """Return the first record whose ir_url matches the host of *url*.

    Matching is by hostname only (scheme-insensitive, www-stripped) so that
    ``https://investor.cdw.com/news/default.aspx`` finds the record whose
    ir_url is ``https://investor.cdw.com/``. Returns None if not found.
    """
    from urllib.parse import urlparse

    def _host(u: str) -> str:
        return urlparse(u).netloc.lower().lstrip("www.")

    target = _host(url)
    if not target:
        return None
    for record in sources:
        ir_url = record.get("ir_url", "")
        if ir_url and _host(ir_url) == target:
            return record
    return None


def load_source_record(slug: str, sources_path: Path = SOURCES_PATH) -> dict:
    """Return the sources.yaml record for *slug*, exiting on failure.

    Convenience wrapper used by scraper scripts that require exactly one
    record by slug and treat a missing entry as a fatal misconfiguration.
    """
    sources = load_sources(sources_path)
    record = find_source(sources, slug)
    if record is None:
        sys.exit(f"No record with slug '{slug}' found in {sources_path}")
    return record