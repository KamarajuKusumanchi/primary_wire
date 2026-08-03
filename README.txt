====== primary_wire ======

**Work in progress.** This project is at an early stage. Coverage is sparse —
Coverage relative to the S&P 500 is incomplete — see
''reports/latest/missing_tickers.txt'' for the current count of covered vs.
missing tickers. Press release links are added manually on an ad hoc basis,
with a small and growing set of sources covered by automated scrapers (see
''Scrapers'' below). If you are interested in helping expand coverage,
contributions are welcome. (That report counts only current S&P 500
tickers — see [[docs/sources.txt]] for why it doesn't match a raw count of
''sources.yaml'' entries.)

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
  - ''config/scraper_config.yaml'' drives automated scraping for a subset
    of these sources, across five IR platforms (Q4, InvestorRoom,
    Notified, Investis Digital, Adobe Experience Manager) — see
    ''reports/latest/scraper_coverage_summary.txt'' for the current count
    and per-platform breakdown. The rest are still added by hand.

===== Scrapers =====

Automated scraping is organized by IR platform, since companies on the same
platform share the same page structure:

  * ''scrape_all.py'' -- runs every scraper configured in
    ''config/scraper_config.yaml'' in one command
  * ''scrape_q4_ir.py'' -- Q4 Inc. sites (e.g. Costco, CDW, Qualcomm)
  * ''scrape_investorroom.py'' -- InvestorRoom sites (e.g. Chipotle, Centene)
  * ''scrape_notified.py'' -- Notified/Drupal sites (e.g. AbbVie, AMD)
  * ''scrape_notified_gated.py'' -- Notified/Drupal sites that are also
    behind bot mitigation such as Akamai (e.g. TJX); same platform as
    scrape_notified.py, just a different way of getting past the gate
  * ''scrape_investis.py'' -- Investis Digital sites (e.g. Home Depot)
  * ''scrape_aem.py'' -- Adobe Experience Manager sites (e.g. BNY)

See [[docs/scrapers.txt]] for what each scraper does, its usage examples,
and its dependencies -- worth reading before running one of these or adding
a new source.

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

^ Column       ^ Description                                            ^
| slug         | Short identifier for the organization (e.g. ''fedex'') |
| ticker       | Stock ticker symbol (empty for govt sources)           |
| title        | Press release title                                    |
| url          | Link to the full press release                         |
| publish_date | Date published, in YYYY-MM-DD format                   |

A ''publish_time'' column is planned for a future release, to record
time-of-day separately when the source provides it. For now, ''publish_date''
holds a plain date only.

Example: ''data/2026/2026-06-01.csv''

  slug,ticker,title,url,publish_date
  fedex,FDX,FedEx Completes Spin-Off of FedEx Freight,https://newsroom.fedex.com/...,2026-06-01

===== Project structure =====

This project is a work in progress. The scripts listed under src/ are
functional but not yet complete, and more tooling is planned.

  primary_wire/
    src/
      scrape_all.py       Orchestrate all scrapers in scraper_config.yaml (incl. --smoke-test)
      scrape_q4_ir.py     Scrape any Q4 Inc. IR site for press release links
      scrape_investorroom.py  Scrape any InvestorRoom-powered IR site
      scrape_notified.py       Scrape any Notified/Drupal IR site
      scrape_notified_gated.py  Scrape Notified/Drupal IR sites behind Akamai-style bot mitigation
      scrape_investis.py  Scrape any Investis Digital-powered IR site
      scrape_aem.py       Scrape any Adobe Experience Manager-powered IR site
      update_source.py    Interactively add or update an entry in sources.yaml
      update_release.py   Interactively add a press release to a daily CSV file
      reporting/          Read-only diagnostic scripts (see docs/reporting.txt)
      utils/
        csv_utils.py      Shared daily-CSV read/merge/write helpers
        scrape_utils.py   Shared scraper argparse/date/NewsItem helpers
        scrape_notified_utils.py  Shared helpers used by both Notified scrapers
        sources_utils.py  Shared sources.yaml read/lookup helpers
    tests/
      src/                Tests for scripts in src/ (forthcoming)
    docs/
      contributing.txt    How to add new data via pull request
      setup.txt           How to install tools and get started
      sources.txt         How to add and manage sources
      reporting.txt       What lives in src/reporting/ and how to add to it
      scrapers.txt        What each scraper does, usage examples, dependencies
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

  python src/reporting/missing_tickers.py

To regenerate all of reports/latest/ in one step, run:

  invoke reports

===== Getting started =====

See [[docs/setup.txt]] for installation instructions.
See [[docs/tasks.txt]] to learn about the `invoke` task runner.
See [[docs/contributing.txt]] to learn how to add new press release links.

===== License =====

MIT