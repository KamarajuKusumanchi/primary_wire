#!/usr/bin/env python3
"""
print_csv_table.py

Generic helper: read a CSV file (any headers, any number of columns) and
print it to stdout as a clean, human-friendly fixed-width table. This is the
counterpart to scripts like detect_ir_platform.py that emit machine-readable
CSV — pipe or point this at their output to get the old human-friendly view
back.

Usage
-----
  # Read from a file
  python src/print_csv_table.py reports/latest/ir_platform.csv

  # Read from stdin (e.g. piped straight from a report script)
  python src/detect_ir_platform.py --all | python src/print_csv_table.py
  cat reports/latest/ir_platform.csv | python src/print_csv_table.py

Output
------
Fixed-width table, plain ASCII, no ANSI codes — trivially redirectable with
>, tee, etc. Header + separator on the first two lines, columns auto-sized
to their widest value (including the header).
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable, Sequence, TextIO


def read_rows(fh: TextIO) -> tuple[list[str], list[list[str]]]:
    """Read CSV from *fh* and return (header, rows).

    Every field is treated as plain text — this script only formats columns,
    it doesn't interpret or convert values.
    """
    reader = csv.reader(fh)
    try:
        header = next(reader)
    except StopIteration:
        return [], []
    rows = [row for row in reader]
    return header, rows


def format_table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Render *header* + *rows* as a fixed-width table and return the text.

    Column widths auto-size to the widest value in each column (including
    the header itself). Missing trailing fields in a row are treated as "".
    """
    rows = list(rows)
    n_cols = len(header)
    widths = [len(h) for h in header]
    for row in rows:
        for i in range(n_cols):
            value = row[i] if i < len(row) else ""
            widths[i] = max(widths[i], len(value))

    def fmt_row(values: Sequence[str]) -> str:
        padded = [values[i] if i < len(values) else "" for i in range(n_cols)]
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(padded))

    lines = [fmt_row(header), "  ".join("-" * w for w in widths)]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=Path,
        default=None,
        help="Path to the CSV file to render. Reads from stdin if omitted.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.csv_path is None:
        header, rows = read_rows(sys.stdin)
    else:
        if not args.csv_path.exists():
            print(f"error: file not found: {args.csv_path}", file=sys.stderr)
            return 1
        with args.csv_path.open("r", encoding="utf-8", newline="") as fh:
            header, rows = read_rows(fh)

    if not header:
        print("error: no data to display (empty input)", file=sys.stderr)
        return 1

    print(format_table(header, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())