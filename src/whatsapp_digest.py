#!/usr/bin/env python3
"""
Convert a primary_wire daily CSV (slug,ticker,title,url,publish_date,publish_time)
into a plain-text, WhatsApp-friendly digest: a "==== press releases on
YYYY-MM-DD ====" title line (date taken from the CSV's publish_date column),
followed by one two-line block per row ("TICKER: title (date, time)" then a
bare URL on its own line so WhatsApp auto-links it). No markdown links or
bold, since WhatsApp doesn't render either.

Usage:
    cat data/YYYY/YYYY-MM-DD.csv | python src/whatsapp_digest.py
    python src/whatsapp_digest.py data/YYYY/YYYY-MM-DD.csv
"""
import argparse
import csv
import sys


def format_row(row: dict) -> str:
    date = row.get("publish_date", "").strip()
    time = row.get("publish_time", "").strip()
    if date and time:
        when = f" ({date}, {time})"
    elif date:
        when = f" ({date})"
    else:
        when = ""

    # URL stays on its own bare line so WhatsApp auto-links it.
    return f"{row['ticker']}: {row['title']}{when}\n{row['url']}"


def main():
    parser = argparse.ArgumentParser(
        description="Format a primary_wire daily CSV as a WhatsApp-friendly digest."
    )
    parser.add_argument(
        "csv_file",
        nargs="?",
        type=argparse.FileType("r", encoding="utf-8"),
        default=sys.stdin,
        help="Path to CSV file. Defaults to stdin.",
    )
    args = parser.parse_args()

    reader = csv.DictReader(args.csv_file)

    required = {"ticker", "title", "url", "publish_date", "publish_time"}
    missing = required - set(reader.fieldnames or [])
    if missing:
        sys.exit(f"error: CSV is missing expected column(s): {', '.join(sorted(missing))}")

    rows = list(reader)

    if not rows:
        print("==== no press releases ====")
        return

    # Rows are expected to all share one publish_date (one CSV = one day).
    # If they don't, fall back to showing the full range rather than guessing.
    dates = sorted({row.get("publish_date", "").strip() for row in rows} - {""})
    if len(dates) == 1:
        date_label = dates[0]
    elif dates:
        date_label = f"{dates[0]} to {dates[-1]}"
    else:
        date_label = "unknown date"

    blocks = [format_row(row) for row in rows]

    print(f"==== press releases on {date_label} ====\n")
    print("\n\n".join(blocks))


if __name__ == "__main__":
    main()