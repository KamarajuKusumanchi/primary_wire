#!/usr/bin/env python3
"""
update_source.py

Interactively add or update a single company's entry in sources/sources.yaml.

Usage:
    python update_source.py

Requires:
    pip install yfinance ruamel.yaml
"""

import re
import sys
from pathlib import Path
from typing import Optional

try:
    import yfinance as yf
except ImportError:
    sys.exit("Missing dependency. Install with: pip install yfinance")

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("Missing dependency. Install with: pip install ruamel.yaml")

# Assumes this script lives in <repo_root>/src/update_source.py
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = REPO_ROOT / "sources" / "sources.yaml"

LEGAL_SUFFIXES = [
    "Incorporated", "Inc.", "Inc", "Corporation", "Corp.", "Corp",
    "Company", "Co.", "Co", "Limited", "Ltd.", "Ltd", "plc", "PLC",
    "Holding Company", "Holdings", "Holding", "Group", "L.L.C.", "LLC",
]


def make_slug(name: str) -> str:
    """Derive a short slug from a company's display name."""
    s = re.sub(r"\(.*?\)", "", name)  # drop parentheticals
    for suf in LEGAL_SUFFIXES:
        s = re.sub(rf",?\s*\b{re.escape(suf)}\b\.?\s*$", "", s)
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def fetch_company_name(ticker: str) -> Optional[str]:
    """Best-effort lookup of company name via yfinance. Returns None on failure."""
    try:
        info = yf.Ticker(ticker).info
        return info.get("longName") or info.get("shortName")
    except Exception as e:
        print(f"  Lookup failed ({e}).")
        return None


def prompt(text: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
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


def slug_taken(sources, slug: str, exclude_ticker: Optional[str] = None) -> bool:
    for s in sources:
        if s.get("ticker") == exclude_ticker:
            continue
        if s.get("slug") == slug:
            return True
    return False


def prompt_for_slug(sources, suggested: str, exclude_ticker: Optional[str] = None) -> str:
    while True:
        slug = prompt("Slug", default=suggested)
        if slug_taken(sources, slug, exclude_ticker=exclude_ticker):
            print(f"  Slug '{slug}' is already used by another entry. Choose a different one.")
            continue
        return slug


def handle_notes(existing) -> None:
    """Mutates `existing` in place: keep / update / delete the notes field."""
    current = existing.get("notes")
    display = current if current else "(none)"
    print(f"Current notes: {display}")
    while True:
        choice = input("Keep / Update / Delete notes? [k/u/d]: ").strip().lower()
        if choice in ("k", "keep", ""):
            return
        if choice in ("u", "update"):
            new_notes = input("New notes: ").strip()
            existing["notes"] = new_notes
            return
        if choice in ("d", "delete"):
            if "notes" in existing:
                del existing["notes"]
            return
        print("  Please answer k, u, or d.")


def main():
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)

    with open(SOURCES_PATH) as f:
        data = yaml.load(f)
    sources = data["sources"]

    ticker = prompt("Ticker").upper()
    existing = next((s for s in sources if s.get("ticker") == ticker), None)

    print(f"Looking up company name for {ticker}...")
    name = fetch_company_name(ticker)
    if not name:
        name = prompt("Could not auto-fetch name. Enter company name manually")

    if existing:
        print(f"Found existing entry for {ticker}.")
        suggested_slug = existing.get("slug", make_slug(name))
        slug = prompt_for_slug(sources, suggested_slug, exclude_ticker=ticker)
    else:
        print(f"No existing entry for {ticker}. Creating a new one.")
        suggested_slug = make_slug(name)
        slug = prompt_for_slug(sources, suggested_slug, exclude_ticker=None)

    print(f"Company name: {name}")

    if existing:
        ir_url = prompt("IR URL", default=existing.get("ir_url"))
    else:
        ir_url = prompt("IR URL")

    if existing:
        existing["slug"] = slug
        existing["name"] = name
        existing["ir_url"] = ir_url
        handle_notes(existing)
        entry = existing
        action = "Updated"
    else:
        entry = {"slug": slug, "name": name, "ticker": ticker, "ir_url": ir_url}
        sources.append(entry)
        action = "Added"

    print("\n--- Summary ---")
    print(f"slug:   {entry.get('slug')}")
    print(f"name:   {entry.get('name')}")
    print(f"ticker: {entry.get('ticker')}")
    print(f"ir_url: {entry.get('ir_url')}")
    print(f"notes:  {entry.get('notes', '(none)')}")
    print("---------------")

    if not confirm(f"{action} this entry in sources.yaml?"):
        print("Aborted. No changes written.")
        return

    sources.sort(key=lambda s: s.get("slug", ""))

    with open(SOURCES_PATH, "w") as f:
        yaml.dump(data, f)

    print(f"{action} {ticker} (slug: {slug}) in {SOURCES_PATH}")


if __name__ == "__main__":
    main()