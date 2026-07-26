#!/usr/bin/env python3
"""
update_release.py

Interactively add or update a press-release entry in the appropriate
data/YYYY/YYYY-MM-DD.csv file.

Usage:
    python src/update_release.py

Requires:
    pip install pandas ruamel.yaml
"""

import sys
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.sources_utils import REPO_ROOT, SOURCES_PATH, find_source_by_url, load_sources  # noqa: E402

CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_date"]
SORT_FIELDS = ["publish_date", "slug", "ticker", "title", "url"]


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def prompt(text: str, default: Optional[str] = None, allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        val = input(f"{text}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default
        if allow_empty:
            return ""
        print("  This field is required.")


def confirm(text: str) -> bool:
    while True:
        val = input(f"{text} [y/n]: ").strip().lower()
        if val in ("y", "yes"):
            return True
        if val in ("n", "no"):
            return False
        print("  Please answer y or n.")


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def csv_path_for_date(date_str: str) -> Path:
    """Return the Path for data/YYYY/YYYY-MM-DD.csv."""
    return REPO_ROOT / "data" / date_str[:4] / f"{date_str}.csv"


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=CSV_FIELDS)
    return pd.read_csv(path, dtype=str).fillna("")


def write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # 1. Prompt for URL
    url = prompt("Press-release URL")

    # 2. Match URL to a source in sources/sources.yaml.
    # find_source_by_url() checks both "ir_url" and the optional "news_url"
    # field, since a press-release URL for a source like Lockheed Martin
    # lives under news_url's host, not ir_url's.
    sources = load_sources(SOURCES_PATH)

    source = find_source_by_url(sources, url)
    if source is None:
        print("\nCould not match this URL to any entry in sources/sources.yaml.")
        print("Please update sources/sources.yaml with the appropriate source entry.")
        print("  Hint: src/update_source.py can probably be used to add it.")
        sys.exit(1)

    slug = source["slug"]
    ticker = source.get("ticker", "")
    print(f"\nMatched source: slug='{slug}', ticker='{ticker}' ({source.get('name', '')})")

    # 3. Prompt for publish date (YYYY-MM-DD)
    while True:
        publish_date = prompt("\nPublish date (YYYY-MM-DD)")
        # Basic format validation
        parts = publish_date.split("-")
        if (
            len(parts) == 3
            and len(parts[0]) == 4
            and len(parts[1]) == 2
            and len(parts[2]) == 2
            and all(p.isdigit() for p in parts)
        ):
            break
        print("  Invalid format. Please enter the date as YYYY-MM-DD (e.g. 2026-06-22).")

    # 4. Prompt for title
    # NOTE: there is no publish_time column yet, so time-of-day is not
    # collected here. Once a publish_time column exists, re-add a prompt for
    # it here (see the old publish_datetime combination logic in git history).
    title = prompt("\nTitle")

    # 5. Confirm the entry
    print("\n--- Confirm entry ---")
    print(f"  slug:         {slug}")
    print(f"  ticker:       {ticker}")
    print(f"  title:        {title}")
    print(f"  url:          {url}")
    print(f"  publish_date: {publish_date}")
    print("---------------------")

    if not confirm("Save this entry?"):
        print("Aborted. No changes written.")
        return

    # 6-8. Load (or create) the CSV file and update it
    csv_path = csv_path_for_date(publish_date)
    df = load_csv(csv_path)

    new_row = pd.DataFrame([{
        "slug": slug,
        "ticker": ticker,
        "title": title,
        "url": url,
        "publish_date": publish_date,
    }])

    url_found = url in df["url"].values
    df = df[df["url"] != url]                        # drop existing row if present
    df = pd.concat([df, new_row], ignore_index=True) # append new row

    # 9. Sort and write
    df = df.sort_values(SORT_FIELDS).reset_index(drop=True)
    write_csv(csv_path, df)

    if url_found:
        action = "Updated existing entry in"
    elif len(df) == 1:
        action = "Created"
    else:
        action = "Added entry to"

    n = len(df)
    print(
        f"\n{action} {csv_path.relative_to(REPO_ROOT)} "
        f"({n} entr{'y' if n == 1 else 'ies'} total)."
    )


if __name__ == "__main__":
    main()