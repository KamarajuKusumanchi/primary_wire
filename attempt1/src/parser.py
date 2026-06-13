"""
primary_wire / src/parser.py
----------------------------
Fetches and parses an RSS feed for a given source.
Returns a DataFrame with columns: slug, ticker, title, url, published_at.
"""

import feedparser
import pandas as pd
from datetime import datetime, timezone


def fetch_entries(source: dict) -> pd.DataFrame:
    """
    Fetch RSS feed for a source and return a DataFrame of entries.

    Each row has:
        slug          - short identifier from sources.yaml (e.g. "fedex")
        ticker        - stock ticker if applicable (e.g. "FDX"), empty string otherwise
        title         - press release title
        url           - link to the full press release
        published_at  - ISO 8601 UTC timestamp (e.g. "2026-06-01T08:32:00Z")

    Entries with no published date are skipped with a warning.
    """
    feed = feedparser.parse(source["rss_url"])
    rows = []

    for entry in feed.entries:
        if not (hasattr(entry, "published_parsed") and entry.published_parsed):
            print(f"  Warning: no published date for '{entry.get('title', '?')}', skipping.")
            continue

        published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

        rows.append({
            "slug":         source["slug"],
            "ticker":       source.get("ticker", ""),
            "title":        entry.get("title", "").strip(),
            "url":          entry.get("link", "").strip(),
            "published_at": published_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    return pd.DataFrame(rows, columns=["slug", "ticker", "title", "url", "published_at"])
