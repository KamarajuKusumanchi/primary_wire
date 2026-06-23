====== primary_wire ======

**Work in progress.** This project is at an early stage. Coverage is sparse —
sources.yaml currently has entries for roughly 30 companies out of the ~500 in
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