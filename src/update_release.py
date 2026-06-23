#!/usr/bin/env python3
"""
update_release.py

Interactively add a press-release entry to the appropriate data/YYYY/YYYY-MM-DD.csv file.

Usage:
    python update_release.py

Requires:
    pip install ruamel.yaml requests beautifulsoup4
"""

import csv
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Install with: pip install requests")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Install with: pip install beautifulsoup4")

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("Missing dependency. Install with: pip install ruamel.yaml")


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"
CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_datetime"]
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Matches a press-release dateline in the body, e.g.:
#   "NEW YORK, June 1, 2026 /PRNewswire/ --"
#   "DALLAS, June 2, 2026 --"
#   "June 3, 2026 /GlobeNewswire/"
DATELINE_RE = re.compile(
    r"(?:[A-Z][A-Z ,\.]+,\s*)?"                               # optional CITY,
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"   # month abbrev
    r"[a-z]*\s+\d{1,2},\s+\d{4}"                             # D, YYYY
    r"(?:\s+\d{1,2}:\d{2}\s*[AP]M)?)",                       # optional time
)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def prompt(text: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        val = input(f"{text}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default
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
# Page fetching and extraction
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, timeout=15, headers=HEADERS)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"  Warning: could not fetch page ({e}).")
        return None


def extract_title(soup: BeautifulSoup) -> Optional[str]:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None


def extract_dateline(soup: BeautifulSoup) -> Optional[str]:
    """
    Search the first 30 short elements for a press-release dateline, e.g.:
      "NEW YORK, June 1, 2026 /PRNewswire/ --"
    Returns just the date (and time if present), e.g. "June 1, 2026 05:30 AM".
    """
    for el in soup.find_all(["p", "div", "span"], limit=30):
        txt = el.get_text(separator=" ", strip=True)
        if len(txt) < 300:
            m = DATELINE_RE.search(txt)
            if m:
                return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

def normalize_datetime(raw: str) -> Optional[str]:
    """
    Convert a dateline date string like "June 1, 2026 05:30 AM ET" to
    "YYYY-MM-DD [time] [tz]" format. Returns None if it cannot be parsed.
    """
    raw = raw.strip()
    # Split into the part strptime can handle and any trailing timezone text
    # e.g. "June 1, 2026 05:30 AM" + " ET"
    m = re.match(
        r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2},?\s+\d{4}"
        r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)?)"  # optional HH:MM[:SS] AM/PM
        r"(.*)",                                          # trailing timezone
        raw, re.I,
    )
    if not m:
        return None

    date_time_part = m.group(1).strip()
    trailing_tz = m.group(2).strip()

    for fmt in [
        "%B %d, %Y %I:%M:%S %p", "%B %d, %Y %I:%M %p", "%B %d, %Y",
        "%b %d, %Y %I:%M:%S %p", "%b %d, %Y %I:%M %p", "%b %d, %Y",
    ]:
        try:
            dt = datetime.strptime(date_time_part, fmt)
            date_str = dt.strftime("%Y-%m-%d")
            time_m = re.search(r"\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M", date_time_part, re.I)
            time_part = time_m.group(0).strip() if time_m else ""
            return " ".join(filter(None, [date_str, time_part, trailing_tz]))
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def csv_path_for_date(date_str: str) -> Path:
    return REPO_ROOT / "data" / date_str[:4] / f"{date_str}.csv"


def load_csv(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # 1. URL
    url = prompt("Press-release URL")

    # 2. Match to a source
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(SOURCES_PATH) as f:
        sources = yaml.load(f)["sources"]

    source = find_source_by_url(sources, url)
    if source is None:
        print("\nCould not match this URL to any entry in sources/sources.yaml.")
        print("Please add the source first.")
        print("  Hint: src/update_source.py can probably be used to add it.")
        sys.exit(1)

    slug = source["slug"]
    ticker = source.get("ticker", "")
    print(f"\nMatched source: slug='{slug}', ticker='{ticker}' ({source.get('name', '')})")

    # 3 & 4. Fetch page once; extract title and publish_datetime
    print("\nFetching page...")
    soup = fetch_page(url)

    fetched_title = extract_title(soup) if soup else None
    if fetched_title:
        print(f"Detected title: {fetched_title}")
        title = fetched_title if confirm("Does this title look correct?") else prompt("Enter correct title")
    else:
        print("Could not auto-detect title.")
        title = prompt("Enter title")

    raw_dt = extract_dateline(soup) if soup else None
    suggested_dt = normalize_datetime(raw_dt) if raw_dt else None

    if suggested_dt:
        print(f"Detected publish_datetime: {suggested_dt}")
        if confirm("Does this publish_datetime look correct?"):
            publish_datetime = suggested_dt
        else:
            raw = prompt("Enter publish_datetime (e.g. 2026-06-01 or June 1, 2026 05:30 AM ET)")
            publish_datetime = normalize_datetime(raw) or raw
    else:
        print("Could not auto-detect publish date/time.")
        raw = prompt("Enter publish_datetime (e.g. 2026-06-01 or June 1, 2026 05:30 AM ET)")
        publish_datetime = normalize_datetime(raw) or raw

    # 5. Confirm and save
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

    date_m = re.match(r"(\d{4}-\d{2}-\d{2})", publish_datetime)
    if not date_m:
        print(f"Error: could not extract a YYYY-MM-DD date from '{publish_datetime}'.")
        sys.exit(1)

    csv_path = csv_path_for_date(date_m.group(1))
    rows = load_csv(csv_path)

    if any(row.get("url") == url for row in rows):
        print(f"\nEntry already exists in {csv_path.relative_to(REPO_ROOT)}. No changes made.")
        return

    rows = sorted(
        rows + [{"slug": slug, "ticker": ticker, "title": title,
                 "url": url, "publish_datetime": publish_datetime}],
        key=lambda r: (r.get("slug", ""), r.get("ticker", ""),
                       r.get("title", ""), r.get("publish_datetime", "")),
    )
    write_csv(csv_path, rows)

    action = "Created" if len(rows) == 1 else "Updated"
    print(f"\n{action} {csv_path.relative_to(REPO_ROOT)} ({len(rows)} entr{'y' if len(rows) == 1 else 'ies'} total).")


if __name__ == "__main__":
    main()