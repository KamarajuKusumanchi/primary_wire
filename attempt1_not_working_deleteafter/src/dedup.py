"""
primary_wire / src/dedup.py
---------------------------
Handles reading existing CSV files and appending only new entries.
Uses URL as the unique key for deduplication.
"""

import pandas as pd
from pathlib import Path

COLUMNS = ["slug", "ticker", "title", "url", "published_at"]


def load_existing(path: Path) -> pd.DataFrame:
    """Load an existing daily CSV, or return an empty DataFrame if it doesn't exist."""
    if path.exists():
        return pd.read_csv(path, dtype=str).fillna("")
    return pd.DataFrame(columns=COLUMNS)


def append_new_entries(new_df: pd.DataFrame, path: Path) -> int:
    """
    Append entries from new_df that are not already in the CSV at path.
    Uses URL as the unique key.
    Returns the number of new entries added.
    """
    existing_df   = load_existing(path)
    existing_urls = set(existing_df["url"])

    truly_new = new_df[~new_df["url"].isin(existing_urls)]

    if truly_new.empty:
        return 0

    combined = pd.concat([existing_df, truly_new], ignore_index=True)
    combined.to_csv(path, index=False)
    return len(truly_new)
