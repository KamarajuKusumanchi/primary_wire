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

# Assumes this script lives in <repo_root>/src/update_release.py
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"

CSV_FIELDS = ["slug", "ticker", "title", "url", "publish_datetime"]


# ---------------------------------------------------------------------------
# Helpers
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


def load_sources():
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(SOURCES_PATH) as f:
        data = yaml.load(f)
    return data["sources"]


def find_source_by_url(sources, url: str) -> Optional[dict]:
    """
    Match a press-release URL against sources by checking whether the URL
    starts with (or shares the host of) any known ir_url.
    Falls back to hostname substring matching.
    """
    parsed_url = urlparse(url)
    url_host = parsed_url.netloc.lower().lstrip("www.")

    # Try strict prefix match on ir_url first
    for source in sources:
        ir_url = source.get("ir_url", "")
        if ir_url and url.startswith(ir_url):
            return source

    # Then hostname containment: url host contains source host or vice versa
    for source in sources:
        ir_url = source.get("ir_url", "")
        if not ir_url:
            continue
        ir_host = urlparse(ir_url).netloc.lower().lstrip("www.")
        if ir_host and (ir_host in url_host or url_host in ir_host):
            return source

    return None


def fetch_page(url: str):
    """Fetch URL and return a BeautifulSoup object, or None on failure."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"  Warning: could not fetch page ({e}).")
        return None


def extract_title(soup) -> Optional[str]:
    """Extract page title from a BeautifulSoup object."""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None


def extract_publish_datetime(soup) -> Optional[str]:
    """
    Try to extract a publish date/time string from a BeautifulSoup object.

    Tries, in order:
      1. Common meta tags (article:published_time, publishdate, date, etc.)
      2. JSON-LD structured data (datePublished)
      3. <time> elements with datetime attribute or itemprop
      4. Elements whose class or id contains date/time/publish hints
         (covers Business Wire, GlobeNewswire, Q4/Nasdaq IR, and similar)
      5. Press-release dateline in the first few paragraphs:
         e.g. "NEW YORK, June 1, 2026 /PRNewswire/ --"
         or   "DALLAS, June 1, 2026 --"
    """
    import json as _json

    # 1. Meta tags
    meta_candidates = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "publishdate"}),
        ("meta", {"name": "date"}),
        ("meta", {"name": "DC.date"}),
        ("meta", {"itemprop": "datePublished"}),
    ]
    for tag, attrs in meta_candidates:
        el = soup.find(tag, attrs)
        if el:
            val = el.get("content") or el.get("datetime") or (el.string or "")
            val = val.strip()
            if val:
                return val

    # 2. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(script.string or "")
            # data may be a dict or a list
            items = data if isinstance(data, list) else [data]
            for item in items:
                val = item.get("datePublished") or item.get("dateModified")
                if val:
                    return val.strip()
        except Exception:
            pass

    # 3. <time> elements
    for el in soup.find_all("time"):
        val = el.get("datetime") or el.get_text(strip=True)
        if val:
            return val.strip()

    # 4. Elements with date/publish hints in class or id
    # Covers: bwdate (Business Wire), release-date, publish-date,
    # article-date, date, dateline, pressrelease-date, etc.
    DATE_HINTS = re.compile(
        r"\b(date|publish|release.?date|dateline|article.?date|pr.?date|posted)\b",
        re.I,
    )
    for el in soup.find_all(True):
        cls = " ".join(el.get("class", []))
        eid = el.get("id", "")
        if DATE_HINTS.search(cls) or DATE_HINTS.search(eid):
            txt = el.get_text(separator=" ", strip=True)
            if txt and len(txt) < 120:  # avoid grabbing huge containers
                return txt

    # 5. Dateline in body paragraphs
    # Pattern: optional ALL-CAPS CITY, then "Month D, YYYY" possibly followed
    # by wire service tag and em-dash.
    # Examples:
    #   "NEW YORK, June 1, 2026 /PRNewswire/ --"
    #   "DALLAS, June 1, 2026 --"
    #   "June 1, 2026 /GlobeNewswire/"
    DATELINE_RE = re.compile(
        r"(?:[A-Z][A-Z ,\.]+,\s*)?"  # optional CITY prefix
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
        r"\s+\d{1,2},\s+\d{4}"  # Month D, YYYY
        r"(?:\s+\d{1,2}:\d{2}\s*[AP]M)?)"  # optional time
        r"(?:\s*/[^/]+/)?"  # optional /WireName/
        r"(?:\s+[-–])?",  # optional dash
        re.I,
    )
    for p in soup.find_all(["p", "div", "span"], limit=30):
        txt = p.get_text(separator=" ", strip=True)
        # Only look at short-ish elements that look like datelines
        if len(txt) > 300:
            continue
        m = DATELINE_RE.search(txt)
        if m:
            return m.group(1).strip()

    return None


def parse_and_normalize_date(raw: str) -> Optional[str]:
    """
    Parse a raw datetime string. Return it with the date portion converted to
    YYYY-MM-DD, preserving any time and timezone information as-is.

    Strategy:
      1. If raw contains YYYY-MM-DD (possibly with ISO T separator), normalise
         in place and keep the rest of the string verbatim.
      2. Otherwise try common written forms ("June 1, 2026 05:30 AM ET"),
         preserving the time/tz suffix that strptime cannot consume.
    """
    raw = raw.strip()

    # 1a. ISO 8601 with T separator: "2026-06-10T08:30:00-05:00"
    #     Collapse the T to a space; keep the rest (time + tz offset) as-is.
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(.+)", raw)
    if m:
        return f"{m.group(1)} {m.group(2)}"

    # 1b. YYYY-MM-DD already present (possibly followed by time/tz)
    m = re.match(r"(\d{4}-\d{2}-\d{2})(.*)", raw)
    if m:
        rest = m.group(2).strip()
        return f"{m.group(1)} {rest}".strip() if rest else m.group(1)

    # 2. Written-form dates: try progressively less specific patterns,
    #    but preserve any trailing time/tz text that strptime ignores.
    #
    #    We extract just the date-and-optional-time part, convert it to
    #    YYYY-MM-DD, then re-attach any trailing text (e.g. timezone).
    #
    #    Patterns ordered from most to least specific so the right one
    #    matches first.
    DATE_PART_RE = re.compile(
        r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2},?\s+\d{4}"
        r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)?)"  # optional time
        r"(.*)",  # trailing text (tz etc.)
        re.I,
    )
    m = DATE_PART_RE.match(raw)
    if m:
        date_and_time = m.group(1).strip()
        trailing = m.group(2).strip()  # e.g. "ET", "EST", "Eastern Time"

        patterns = [
            "%B %d, %Y %I:%M:%S %p",
            "%B %d, %Y %I:%M %p",
            "%B %d %Y %I:%M %p",
            "%B %d, %Y",
            "%b %d, %Y %I:%M:%S %p",
            "%b %d, %Y %I:%M %p",
            "%b %d %Y %I:%M %p",
            "%b %d, %Y",
        ]
        for pat in patterns:
            try:
                dt = datetime.strptime(date_and_time, pat)
                date_str = dt.strftime("%Y-%m-%d")
                # Reconstruct: keep time portion from original string if present
                # by re-attaching everything after the date.
                time_re = re.search(
                    r"\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M", date_and_time, re.I
                )
                time_part = time_re.group(0).strip() if time_re else ""
                parts = [date_str]
                if time_part:
                    parts.append(time_part)
                if trailing:
                    parts.append(trailing)
                return " ".join(parts)
            except ValueError:
                pass

    # 3. Numeric: MM/DD/YYYY [H:MM AM tz]
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})(.*)", raw)
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)}/{m.group(2)}/{m.group(3)}", "%m/%d/%Y")
            rest = m.group(4).strip()
            date_str = dt.strftime("%Y-%m-%d")
            return f"{date_str} {rest}".strip() if rest else date_str
        except ValueError:
            pass

    return None


def csv_path_for_date(date_str: str) -> Path:
    """Return data/YYYY/YYYY-MM-DD.csv path for a given YYYY-MM-DD date string."""
    year = date_str[:4]
    return REPO_ROOT / "data" / year / f"{date_str}.csv"


def extract_date_from_publish_datetime(publish_datetime: str) -> Optional[str]:
    """Pull YYYY-MM-DD from a publish_datetime string."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", publish_datetime)
    return m.group(1) if m else None


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def entry_exists(rows: list[dict], slug: str, ticker: str, url: str) -> bool:
    for row in rows:
        if row.get("slug") == slug and row.get("ticker") == ticker and row.get("url") == url:
            return True
    return False


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (
        r.get("slug", ""),
        r.get("ticker", ""),
        r.get("title", ""),
        r.get("publish_datetime", ""),
    ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # 1. Prompt for URL
    url = prompt("Press-release URL").strip()

    # 2. Determine slug and ticker from sources.yaml
    sources = load_sources()
    source = find_source_by_url(sources, url)

    if source is None:
        print("\nCould not match this URL to any entry in sources/sources.yaml.")
        print("Please add the source first.")
        print("  Hint: src/update_source.py can probably be used to add it.")
        sys.exit(1)

    slug = source["slug"]
    ticker = source.get("ticker", "")
    print(f"\nMatched source: slug='{slug}', ticker='{ticker}' ({source.get('name', '')})")

    # 3 & 4. Fetch page once, extract title and publish_datetime
    print("\nFetching page...")
    soup = fetch_page(url)

    # Title
    fetched_title = extract_title(soup) if soup else None
    if fetched_title:
        print(f"Detected title: {fetched_title}")
        if not confirm("Does this title look correct?"):
            title = prompt("Enter correct title")
        else:
            title = fetched_title
    else:
        print("Could not auto-detect title.")
        title = prompt("Enter title")

    # Publish datetime
    raw_dt = extract_publish_datetime(soup) if soup else None
    suggested_dt = None
    if raw_dt:
        suggested_dt = parse_and_normalize_date(raw_dt)
        if not suggested_dt:
            print(f"Found raw date string but could not parse it: {raw_dt!r}")

    if suggested_dt:
        print(f"Detected publish_datetime: {suggested_dt}")
        if not confirm("Does this publish_datetime look correct?"):
            raw_input = prompt("Enter publish_datetime (date must be YYYY-MM-DD; include time/tz if known)")
            normalized = parse_and_normalize_date(raw_input)
            publish_datetime = normalized if normalized else raw_input
        else:
            publish_datetime = suggested_dt
    else:
        print("Could not auto-detect publish date/time.")
        raw_input = prompt("Enter publish_datetime (date must be YYYY-MM-DD; include time/tz if known)")
        normalized = parse_and_normalize_date(raw_input)
        publish_datetime = normalized if normalized else raw_input

    # 5. Confirm final values
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

    # Determine target CSV path
    date_str = extract_date_from_publish_datetime(publish_datetime)
    if not date_str:
        print(f"Error: could not extract a YYYY-MM-DD date from publish_datetime '{publish_datetime}'.")
        sys.exit(1)

    csv_path = csv_path_for_date(date_str)
    rows = load_csv(csv_path)

    new_entry = {
        "slug": slug,
        "ticker": ticker,
        "title": title,
        "url": url,
        "publish_datetime": publish_datetime,
    }

    if entry_exists(rows, slug, ticker, url):
        print(f"\nEntry already exists in {csv_path.relative_to(REPO_ROOT)}. No changes made.")
        return

    rows.append(new_entry)
    rows = sort_rows(rows)
    write_csv(csv_path, rows)

    action = "Created" if len(rows) == 1 else "Updated"
    print(f"\n{action} {csv_path.relative_to(REPO_ROOT)} ({len(rows)} entr{'y' if len(rows) == 1 else 'ies'} total).")


if __name__ == "__main__":
    main()