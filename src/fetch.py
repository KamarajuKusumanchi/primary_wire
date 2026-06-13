"""
primary_wire / src/fetch.py
---------------------------
Main entry point. Reads sources.yaml, fetches each RSS feed,
and appends new entries to the appropriate daily CSV file.

Run every 30 minutes via cron:
    */30 * * * * cd /home/user/primary_wire && uv run src/fetch.py
"""

import yaml
from pathlib import Path

from parser import fetch_entries
from dedup  import append_new_entries


# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT     = Path(__file__).parent.parent
SOURCES  = ROOT / "sources" / "sources.yaml"
DATA_DIR = ROOT / "data"


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_sources() -> list:
    """Return sources from sources.yaml that have an rss_url."""
    with open(SOURCES) as f:
        all_sources = yaml.safe_load(f)["sources"]

    active  = [s for s in all_sources if s.get("rss_url")]
    skipped = [s["slug"] for s in all_sources if not s.get("rss_url")]

    if skipped:
        print(f"Skipping sources with no RSS feed: {skipped}")

    return active


def csv_path(date_str: str) -> Path:
    """Return the CSV path for a given YYYY-MM-DD date string, creating dirs as needed."""
    path = DATA_DIR / date_str[:4] / f"{date_str}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    sources     = load_sources()
    total_added = 0

    for source in sources:
        print(f"Fetching {source['slug']} ...")
        try:
            entries_df = fetch_entries(source)
        except Exception as e:
            print(f"  Error: {e}")
            continue

        if entries_df.empty:
            print(f"  No entries found.")
            continue

        # Group by date so each entry lands in the correct daily CSV
        entries_df["date"] = entries_df["published_at"].str[:10]

        for date_str, group in entries_df.groupby("date"):
            path  = csv_path(date_str)
            added = append_new_entries(group.drop(columns="date"), path)
            total_added += added
            print(f"  {date_str}: +{added} new entries → {path.relative_to(ROOT)}")

    print(f"\nDone. Total new entries added: {total_added}")


if __name__ == "__main__":
    main()
