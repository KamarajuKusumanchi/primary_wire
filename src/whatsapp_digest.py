#!/usr/bin/env python3
"""
Convert a primary_wire daily CSV (slug,ticker,title,url,publish_date,publish_time)
into a plain-text, WhatsApp-friendly digest: one bulleted block per row, no
markdown links or bold (WhatsApp doesn't render either), just ticker header +
bullets + a bare URL on its own line so WhatsApp auto-links it.

Usage:
    cat data/YYYY/YYYY-MM-DD.csv | python src/whatsapp_digest.py
    python src/whatsapp_digest.py data/YYYY/YYYY-MM-DD.csv
"""
import argparse
import csv
import sys


def format_row(row: dict) -> str:
    lines = [row["ticker"]]

    lines.append(f"- {row['title']}")

    date = row.get("publish_date", "").strip()
    time = row.get("publish_time", "").strip()
    if date and time:
        lines.append(f"- {date}, {time}")
    elif date:
        lines.append(f"- {date}")

    lines.append(f"- {row['url']}")

    return "\n".join(lines)


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

    blocks = [format_row(row) for row in reader]

    print("\n\n".join(blocks))


if __name__ == "__main__":
    main()