====== primary_wire ======

**Work in progress.** This project is at an early stage. Coverage is sparse —
sources.yaml currently has entries for roughly 30 companies out of the ~500 in
the S&P 500. Press release links are added manually on an ad hoc basis. If you
are interested in helping expand coverage, contributions are welcome.

==== Goal ====

Build an open, community-maintained aggregator of official press release links,
covering S&P 500 companies and government agencies (Fed, BLS, etc.). The
long-term aim is to make link collection as automated as possible — scrapers and
automated PRs are explicitly welcome. Manual curation is the starting point, not
the end state.

==== Scope ====

The project primarily aims to cover S&P 500 companies, plus government
agencies (Fed, BLS, etc.). Contributors may also add companies outside the
S&P 500 provided they trade in U.S. markets.

No third-party articles. No editorializing. Just the primary source.

Data is maintained as plain CSV files in a git repository — one file per day.
Anyone can contribute by submitting a pull request.

===== How it works =====

  - Press release links are currently added manually to daily CSV files
  - Each file covers one calendar date: ''data/YYYY/YYYY-MM-DD.csv''
  - Contributions are made via GitHub pull requests
  - Automated link collection is welcome — if you build a scraper and raise a
    pull request, it will be accepted. Links are verified before merging.

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
    requirements.txt
    README.txt

To see which S&P 500 companies are not yet covered, run:

  python src/missing_tickers.py

===== Getting started =====

See [[docs/setup.txt]] for installation instructions.
See [[docs/contributing.txt]] to learn how to add new press release links.

===== License =====

MIT