====== primary_wire ======

**Work in progress.** This project is at an early stage. Coverage is sparse —
sources.yaml currently has entries for roughly 60 companies out of the ~500 in
the S&P 500. Press release links are added manually on an ad hoc basis. If you
are interested in helping expand coverage, contributions are welcome.

==== Goal ====

Build an open, community-maintained aggregator of official press release links,
covering S&P 500 companies and government agencies (Fed, BLS, etc.).

This project exists to support personal research into the relationship between
company announcements and market behavior — for example, quantifying the stock
price impact following a specific press release, or working backward from an
unusual price move to identify whether a press release preceded it.

Free to use. Free to build on. If you make a million dollars off it, good for
you. If you want to say thanks, the author drinks tea — preferably with masala
bathani or cut mirchi on the side.

==== Motivation ====

The obvious question is: why not just use an existing data source? The short
answer is that none of the free ones are actually suitable for this purpose.

Business Wire, PR Newswire, and GlobeNewswire are open publishing platforms —
any company or individual can pay to distribute through them. Their feeds
contain a mix of official company announcements, third-party commentary,
sponsored content, and noise. Filtering that down to only genuine press releases
from a specific company is a non-trivial problem, and even then you are relying
on the company choosing to distribute through that wire service, which is not
always the case.

SEC EDGAR is a different problem. Companies file 8-Ks for material events, and
press releases are sometimes attached as exhibits. But many press releases —
product announcements, partnerships, executive appointments below C-suite level
— never trigger an 8-K filing at all. EDGAR gives you a biased sample skewed
toward regulatory disclosures, not the full picture of what a company is
communicating publicly. An 8-K and a press release are not the same thing.

The only clean approach is to go directly to each company's investor relations
page — the same page the company points investors to. That is what this project
does. It is more work, but it is the only way to be confident that what you have
is actually what the company intended to say, sourced from where the company
intended it to be read.

A secondary benefit is that consolidating these links into a single,
machine-readable dataset makes quantitative research more accessible. Studying
the relationship between press releases and market behavior currently requires
either expensive data subscriptions or a lot of manual work. A clean, open,
structured index of primary sources lowers that barrier and makes the research
itself easier to reproduce and share.

==== Scope ====

The project primarily aims to cover S&P 500 companies, plus government
agencies (Fed, BLS, etc.). Contributors may also add companies outside the
S&P 500 provided they trade in U.S. markets.

No third-party articles. No editorializing. Just the primary source.

This project stores URLs only. No press release content is reproduced or
cached. All links point to the originating company's own servers.

Data is maintained as plain CSV files in a git repository — one file per day.
Anyone can contribute by submitting a pull request.

Data is provided as-is with no guarantees of completeness or accuracy. This
is not financial advice.

===== How it works =====

  - Press release links are currently added manually to daily CSV files
  - Each file covers one calendar date: ''data/YYYY/YYYY-MM-DD.csv''
  - Contributions are made via GitHub pull requests
  - Automated link collection is welcome, provided contributors follow the
    guidelines below. Links are verified before merging.

===== Scrapers =====

==== scrape_q4_ir.py ====

The main scraper. Collects press release links from any investor relations site
powered by Q4 Inc. — a widely used IR platform. Many S&P 500 companies use Q4,
including Costco, CDW, Qualcomm, Corning, and ON Semiconductor. Q4
sites share a common URL structure and page layout, so a single scraper handles
all of them.

Because Q4 pages are rendered client-side, a plain HTTP request returns only a
"Loading..." placeholder. The scraper drives Chrome via Playwright to render the
full page, then parses the DOM with BeautifulSoup. No private API is used — it
reads exactly what a human visiting the page would see.

Date extraction works in two stages:

  1. Listing-page parse (fast, zero extra requests): looks for a date in the
     HTML near each news link. Works on many Q4 themes (e.g. Costco).

  2. Detail-page fallback (opt-in via --fetch-detail-pages): for items where
     no date was found in stage 1, fetches each individual press release page
     and extracts the date from there. Required for some Q4 themes (e.g. CDW)
     where dates are not present in the listing-page HTML. Fetches are spaced
     by --polite-delay.

Usage:

  # Costco — dates found on listing page, no detail fetches needed
  python src/scrape_q4_ir.py --dry-run

  # CDW — dates only on detail pages; --fetch-detail-pages is required
  python src/scrape_q4_ir.py \
      --url https://investor.cdw.com/news/default.aspx \
      --fetch-detail-pages --dry-run

  # Any Q4 IR site by slug or ticker (looked up from sources.yaml)
  python src/scrape_q4_ir.py --slug cdw --fetch-detail-pages --dry-run
  python src/scrape_q4_ir.py --ticker CDW --fetch-detail-pages --dry-run

  # Scrape a specific year
  python src/scrape_q4_ir.py --year 2025

  # Scrape a range of years and output as JSON
  python src/scrape_q4_ir.py --start-year 2023 --end-year 2025 \
      --format json --output out.json --dry-run

  # Watch the browser and save rendered HTML for debugging
  python src/scrape_q4_ir.py --show-browser --debug-dump-html /tmp/page.html --dry-run

Chrome is assumed to already be installed. No ''playwright install'' download
is needed.

==== scrape_costco.py ====

A thin wrapper around scrape_q4_ir.py for Costco specifically. Reads the
Costco entry from sources.yaml to get the IR URL and ticker, then delegates
all scraping and output to scrape_q4_ir. Costco's Q4 theme embeds dates in
listing-page cards, so detail-page fetches are not needed.

Usage:

  # Preview what would be written, without writing anything
  python src/scrape_costco.py --dry-run

  # Scrape and write to data/YYYY/YYYY-MM-DD.csv
  python src/scrape_costco.py

  # Scrape a specific year
  python src/scrape_costco.py --year 2025

All flags supported by scrape_q4_ir.py (--year, --start-year, --end-year,
--since, --until, --format, --dry-run, --verbose, etc.) are passed through.

==== scrape_cdw.py ====

A thin wrapper around scrape_q4_ir.py for CDW specifically. Identical in
structure to scrape_costco.py, except that CDW's Q4 theme does not embed dates
in listing-page cards — so --fetch-detail-pages is enabled by default. Pass
--no-fetch-detail-pages to disable it.

Usage:

  # Preview what would be written, without writing anything
  python src/scrape_cdw.py --dry-run

  # Scrape and write to data/YYYY/YYYY-MM-DD.csv
  python src/scrape_cdw.py

All flags supported by scrape_q4_ir.py are passed through.

===== Guidelines for automated contributions =====

Scrapers are welcome, but must be courteous to the servers they access:

  - Space requests at least 10–30 seconds apart per domain
  - Run scrapers at most once per day — more frequent polling is unnecessary
    and inconsiderate
  - If a server returns errors or rate-limit responses, back off immediately
    and do not retry aggressively
  - Treat these servers as a shared public resource, not a firehose

Scrapers that ignore these guidelines will not have their PRs accepted.

===== Data format =====

Each daily CSV file has five columns:

^ Column           ^ Description                                                  ^
| slug             | Short identifier for the organization (e.g. ''fedex'')       |
| ticker           | Stock ticker symbol (empty for govt sources)                 |
| title            | Press release title                                          |
| url              | Link to the full press release                               |
| publish_datetime | Date published in YYYY-MM-DD format. Time added if available |

Example: ''data/2026/2026-06-01.csv''

  slug,ticker,title,url,publish_datetime
  fedex,FDX,FedEx Completes Spin-Off of FedEx Freight,https://newsroom.fedex.com/...,2026-06-01 05:30 AM

===== Project structure =====

This project is a work in progress. The scripts listed under src/ are
functional but not yet complete, and more tooling is planned.

  primary_wire/
    src/
      scrape_q4_ir.py     Scrape any Q4 Inc. IR site for press release links
      scrape_costco.py    Wrapper: scrape Costco's IR page via scrape_q4_ir
      scrape_cdw.py       Wrapper: scrape CDW's IR page via scrape_q4_ir
      update_source.py    Interactively add or update an entry in sources.yaml
      update_release.py   Interactively add a press release to a daily CSV file
      missing_tickers.py  Show S&P 500 tickers not yet in sources.yaml
    tests/
      src/                Tests for scripts in src/ (forthcoming)
    docs/
      contributing.txt    How to add new data via pull request
      setup.txt           How to install tools and get started
      sources.txt         How to add and manage sources
    sources/
      sources.yaml        Master list of sources and their IR page URLs
    data/
      2026/
        2026-06-01.csv
    reports/
      latest/             Generated reports (see below); regenerate with `invoke reports`
    pyproject.toml
    tasks.py              Invoke task definitions (see docs/tasks.txt)
    README.txt

To see which S&P 500 companies are not yet covered, run:

  python src/missing_tickers.py

To regenerate all of reports/latest/ in one step, run:

  invoke reports

===== Getting started =====

See [[docs/setup.txt]] for installation instructions.
See [[docs/tasks.txt]] to learn about the `invoke` task runner.
See [[docs/contributing.txt]] to learn how to add new press release links.

===== License =====

MIT