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
    python src/whatsapp_digest.py data/YYYY/YYYY-MM-DD-1.csv data/YYYY/YYYY-MM-DD-2.csv

# ---------------------------------------------------------------------------
# Why we dropped the "cat multiple CSVs into stdin" pattern
# ---------------------------------------------------------------------------
# It used to be tempting to combine several days like this:
#
#     cat data/2026/2026-07-08.csv data/2026/2026-07-10.csv | python.exe src/whatsapp_digest.py
#
# This is broken. csv.DictReader only treats the very first line of the
# whole input stream as the header. When cat concatenates two CSVs, the
# second file's header row ("ticker,title,url,publish_date,publish_time")
# doesn't get recognized as a header at all -- it lands mid-stream and gets
# parsed as an ordinary DATA row, using the column names from the first
# file's header. The result is a garbage row where ticker="ticker",
# title="title", publish_date="publish_date", etc. That literal string
# "publish_date" then pollutes the date-range calculation and produces
# nonsense output like "==== press releases on 2026-07-08 to publish_date ====".
#
# Workaround considered and rejected: strip duplicate headers with awk
# before piping, e.g.
#
#     awk 'FNR==1 && NR!=1{next}{print}' data/2026/2026-07-08.csv data/2026/2026-07-10.csv \
#         | python.exe src/whatsapp_digest.py
#
# This "works" but is a band-aid, not a fix:
#   - It silently assumes every file's header line is IDENTICAL to the
#     first file's header. If a file's columns are reordered, renamed, or
#     have a missing/extra column, awk won't catch it -- it just blindly
#     drops "line 1 of every file after the first" and the corruption can
#     resurface in subtier ways (e.g. a column got reordered rather than
#     duplicated).
#   - It pushes CSV-structure knowledge (which line is a header, whether
#     headers match across files) out of the tool that actually understands
#     CSV (the `csv` module) and into a shell one-liner that doesn't
#     understand CSV at all -- it's just counting lines.
#   - It's easy to forget to type when running this ad hoc, and there's no
#     error message if you forget it or get it wrong -- you just silently
#     get corrupted output again.
#
# The real fix: accept multiple file paths as separate argparse arguments
# and run a fresh csv.DictReader per file, so each file's header is
# stripped correctly and validated independently. See csv_files below.
# ---------------------------------------------------------------------------
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
        "csv_files",
        nargs="*",
        type=argparse.FileType("r", encoding="utf-8"),
        default=[sys.stdin],
        help="Path(s) to CSV file(s). Defaults to stdin. Pass multiple paths "
        "to combine several days into one digest -- do NOT `cat` files "
        "together instead (see module docstring for why).",
    )
    args = parser.parse_args()

    required = {"ticker", "title", "url", "publish_date", "publish_time"}

    # Parse each file with its own DictReader so each file's header line is
    # correctly consumed as a header (not as a data row), and so a
    # malformed/mismatched file is caught by name instead of silently
    # producing a garbage row.
    rows = []
    for csv_file in args.csv_files:
        reader = csv.DictReader(csv_file)
        missing = required - set(reader.fieldnames or [])
        if missing:
            name = getattr(csv_file, "name", "<stdin>")
            sys.exit(f"error: {name} is missing expected column(s): {', '.join(sorted(missing))}")
        rows.extend(reader)

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

    print(f"```")
    print(f"==== press releases on {date_label} ====\n")
    print("\n\n".join(blocks))
    print(f"```")


if __name__ == "__main__":
    main()