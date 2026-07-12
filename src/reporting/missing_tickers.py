#!/usr/bin/env python3
"""
Show S&P 500 tickers that are missing from sources/sources.yaml.

Tickers are printed in yfinance format (e.g. BRK-B instead of BRK.B).

Usage:
    python src/reporting/missing_tickers.py
    python src/reporting/missing_tickers.py --sources path/to/sources.yaml
"""

import argparse
import io
import sys
from pathlib import Path

import pandas as pd
import requests

try:
    from ruamel.yaml import YAML
except ImportError:
    sys.exit("Missing dependency. Install with: pip install ruamel.yaml")


def get_sp500_tickers() -> set[str]:
    """Scrape S&P 500 tickers from Wikipedia and return in yfinance format."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; primary_wire/1.0)"}
    html = requests.get(url, headers=headers, timeout=30).text
    # pandas 2.x requires a file-like object; bare HTML strings are treated as file paths by lxml
    # So instead of doing
    #   tables = pd.read_html(html)
    # do
    #   tables = pd.read_html(io.StringIO(html))
    tables = pd.read_html(io.StringIO(html))
    df = tables[0]
    tickers = df["Symbol"].str.strip().tolist()
    # Wikipedia uses BRK.B; yfinance expects BRK-B
    return {t.replace(".", "-") for t in tickers}


def get_sources_tickers(sources_path: Path) -> set[str]:
    """Extract tickers from sources.yaml in yfinance format."""
    yaml = YAML()
    with open(sources_path) as f:
        data = yaml.load(f)

    tickers = set()
    for source in data.get("sources", []):
        ticker = source.get("ticker")
        if ticker:
            tickers.add(ticker.strip().replace(".", "-"))
    return tickers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path(__file__).parent.parent.parent / "sources" / "sources.yaml",
        help="Path to sources.yaml (default: sources/sources.yaml relative to the repo root)",
    )
    args = parser.parse_args()

    if not args.sources.exists():
        raise FileNotFoundError(f"sources.yaml not found at {args.sources}")

    print("Fetching S&P 500 tickers from Wikipedia...", file=sys.stderr)
    sp500 = get_sp500_tickers()

    print(f"Loading sources from {args.sources}...", file=sys.stderr)
    covered = get_sources_tickers(args.sources)

    missing = sorted(sp500 - covered)

    print(f"\nS&P 500 companies:  {len(sp500)}")
    print(f"Covered in sources: {len(covered & sp500)}")
    print(f"Missing:            {len(missing)}")
    print("\nMissing tickers:")
    for ticker in missing:
        print(f"  {ticker}")


if __name__ == "__main__":
    main()