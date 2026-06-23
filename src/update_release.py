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
from urllib.parse import urlparse

import pandas as pd

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("Missing dependency. Install with: pip install ruamel.yaml")

# Assumes this script lives in <repo_root>/src/update_release.py
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"
CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_datetime"]
SORT_FIELDS = ["publish_datetime", "slug", "ticker", "title", "url"]


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
# Source matching
# ---------------------------------------------------------------------------

def find_source_by_url(sources: list, url: str) -> Optional[dict]:
    """
    Match a press-release URL to a source in sources.yaml.
    Tries prefix match on ir_url first, then falls back to hostname match.
    """
    for source in sources:
        ir_url = source.get("ir_url", "")
        if ir_url and url.startswith(ir_url):
            return source

    url_host = urlparse(url).netloc.lstrip("www.")
    for source in sources:
        ir_url = source.get("ir_url", "")
        if ir_url and urlparse(ir_url).netloc.lstrip("www.") == url_host:
            return source

    return None


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

    # 2. Match URL to a source in sources/sources.yaml
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(SOURCES_PATH) as f:
        sources = yaml.load(f)["sources"]

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

    # 4. Prompt for publish time (optional — press Enter to skip)
    publish_time = prompt("Publish time (press Enter to skip)", allow_empty=True)

    # 5. Derive publish_datetime
    if publish_time:
        publish_datetime = publish_date + " " + publish_time
    else:
        publish_datetime = publish_date

    # 6. Prompt for title
    title = prompt("\nTitle")

    # 7. Confirm the entry
    print("\n--- Confirm entry ---")
    print(f"  slug:             {slug}")
    print(f"  ticker:           {ticker}")
    print(f"  title:            {title}")
    print(f"  url:              {url}")
    print(f"  publish_datetime: {publish_datetime}")
    print("---------------------")

    if not confirm("Save this entry?"):
        print("Aborted. No changes written.")
        return

    # 8-10. Load (or create) the CSV file and update it
    csv_path = csv_path_for_date(publish_date)
    df = load_csv(csv_path)

    new_row = pd.DataFrame([{
        "slug": slug,
        "ticker": ticker,
        "title": title,
        "url": url,
        "publish_datetime": publish_datetime,
    }])

    url_found = url in df["url"].values
    df = df[df["url"] != url]                        # drop existing row if present
    df = pd.concat([df, new_row], ignore_index=True) # append new row

    # 11. Sort and write
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