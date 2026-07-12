"""
tasks.py - task automation for primary_wire, using Invoke (https://www.pyinvoke.org/)

Regenerates the files under reports/latest/. ir_platform.csv and
missing_tickers.txt are produced the simple way: run the script in src/,
capture its stdout, write it to a file, replacing the old manual workflow
of:

    python.exe src/detect_ir_platform.py  > reports/latest/ir_platform.csv
    python.exe src/missing_tickers.py     > reports/latest/missing_tickers.txt

check_scraper_coverage.py doesn't fit that pattern, because its output
used to mix a prose summary with an embedded CSV block in a single file
(reports/latest/scraper_coverage.txt). It's now split into two
single-format files -- scraper_coverage_summary.txt (prose only) and
scraper_coverage_missing.csv (CSV only, header "slug,ticker,platform,
ir_url") -- so instead of tasks.py capturing stdout, the script writes
both files itself in one pass via its --write-reports flag:

    python.exe src/check_scraper_coverage.py --write-reports
    # writes reports/latest/scraper_coverage_summary.txt
    #    and reports/latest/scraper_coverage_missing.csv

One pass (rather than tasks.py running the script twice, once per file)
guarantees the prose summary and the CSV of gaps reflect the exact same
sources.yaml/scraper_config.yaml snapshot.

ir_platform.csv and scraper_coverage_missing.csv are machine-readable. To
view either as a human-friendly fixed-width table, run:

    uv run python src/print_csv_table.py reports/latest/ir_platform.csv
    uv run python src/print_csv_table.py reports/latest/scraper_coverage_missing.csv

Usage
-----
    invoke --list              # show all available tasks
    invoke reports              # regenerate all reports (default task)
    invoke ir-platform           # regenerate just reports/latest/ir_platform.csv
    invoke missing-tickers       # regenerate just reports/latest/missing_tickers.txt
    invoke scraper-coverage      # regenerate scraper_coverage_summary.txt + scraper_coverage_missing.csv
    invoke smoke-test            # quick "is anything broken?" check (see below)

ir-platform and missing-tickers are invoked as "uv run python <script>"
(using an absolute path to the script, so this works no matter which
directory you run `invoke` from), so this works whether or not a virtual
environment is currently activated. scraper-coverage is invoked the same
way, plus --write-reports -- see scraper_coverage() below.
"""

from pathlib import Path

from invoke import task

ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports" / "latest"

# name -> (script path relative to ROOT, output filename in reports/latest/).
# check_scraper_coverage.py isn't here: it writes its own two output files
# directly (via --write-reports) rather than going through this
# stdout-capture-and-redirect helper -- see scraper_coverage() below and
# that script's module docstring for why.
REPORT_SPECS = {
    "ir-platform": ("src/detect_ir_platform.py", "ir_platform.csv"),
    "missing-tickers": ("src/missing_tickers.py", "missing_tickers.txt"),
}


def _run_report(c, name):
    """Run one report script with uv and write its stdout to reports/latest/."""
    script, output_filename = REPORT_SPECS[name]
    script_path = ROOT / script
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / output_filename

    print(f"[{name}] running: uv run python {script_path}")
    result = c.run(f'uv run python "{script_path}"', hide=True, warn=True)

    out_path.write_text(result.stdout)

    if result.stderr:
        print(f"[{name}] stderr:\n{result.stderr}")

    if not result.ok:
        print(f"[{name}] WARNING: exited with code {result.return_code}; "
              f"{out_path.relative_to(ROOT)} was still written with whatever "
              f"stdout was produced before the failure.")
    else:
        n_lines = len(result.stdout.splitlines())
        print(f"[{name}] wrote {out_path.relative_to(ROOT)} ({n_lines} lines)")

    return result.ok


@task
def ir_platform(c):
    """Regenerate reports/latest/ir_platform.csv (detect_ir_platform.py --all)."""
    _run_report(c, "ir-platform")


@task
def missing_tickers(c):
    """Regenerate reports/latest/missing_tickers.txt (missing_tickers.py)."""
    _run_report(c, "missing-tickers")


@task
def scraper_coverage(c):
    """Regenerate scraper_coverage_summary.txt and scraper_coverage_missing.csv.

    Unlike the other report tasks, check_scraper_coverage.py writes both
    files itself in one pass (via --write-reports) rather than tasks.py
    capturing stdout: that guarantees the prose summary and the CSV of gaps
    come from the same sources.yaml/scraper_config.yaml snapshot, which
    running the script twice (once per file) couldn't promise.
    """
    script_path = ROOT / "src" / "check_scraper_coverage.py"
    cmd = f'uv run python "{script_path}" --write-reports'
    print(f"[scraper-coverage] running: {cmd}")
    result = c.run(cmd, hide=True, warn=True)

    if result.stdout.strip():
        print(f"[scraper-coverage] {result.stdout.strip()}")
    if result.stderr:
        print(f"[scraper-coverage] stderr:\n{result.stderr}")
    if not result.ok:
        print(f"[scraper-coverage] WARNING: exited with code {result.return_code}")

    return result.ok


@task(pre=[ir_platform, missing_tickers, scraper_coverage], default=True)
def reports(c):
    """Regenerate all reports under reports/latest/."""
    print("Done: reports/latest/ is up to date.")


@task
def smoke_test(c, seed=None):
    """Quick check that the scrapers aren't broken, without scraping everything.

    Runs src/scrape_all.py --smoke-test --dry-run, which picks one random
    source per distinct (scraper, extra-args) signature in
    config/scraper_config.yaml -- see that script's docstring for why that's
    the right unit of "category" to sample from. --dry-run means nothing is
    written to data/; this only checks that each code path still runs.

    Usage:
        invoke smoke-test
        invoke smoke-test --seed 42   # reproducible picks, e.g. for a bug report
    """
    script_path = ROOT / "src" / "scrape_all.py"
    cmd = f'uv run python "{script_path}" --smoke-test --dry-run'
    if seed is not None:
        cmd += f" --seed {seed}"
    c.run(cmd, pty=True)