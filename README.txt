====== primary_wire ======

A community-maintained aggregator of official press release links.

==== Scope ====

The project primarily aims to cover S&P 500 companies, plus government
agencies (Fed, BLS, etc.). Contributors may also add companies outside the
S&P 500 provided they trade in U.S. markets.

No third-party articles. No editorializing. Just the primary source.

Data is maintained as plain CSV files in a git repository — one file per day.
Anyone can contribute by submitting a pull request.

===== How it works =====

  - Press release links are added manually to daily CSV files
  - Each file covers one calendar date: ''data/YYYY/YYYY-MM-DD.csv''
  - Contributions are made via GitHub pull requests
  - Automated link collection is welcome — if you build a scraper and raise a
    pull request, it will be accepted. Links are verified before merging.

===== Data format =====

Each daily CSV file has five columns:

^ Column       ^ Description                                      ^
| slug         | Short identifier for the organization (e.g. ''fedex'') |
| ticker       | Stock ticker symbol (empty for govt sources)     |
| title        | Press release title                              |
| url          | Link to the full press release                   |
| publish_date | Date published in YYYY-MM-DD format              |

Example: ''data/2026/2026-06-01.csv''

  slug,ticker,title,url,publish_date
  fedex,FDX,FedEx Completes Spin-Off of FedEx Freight,https://newsroom.fedex.com/...,2026-06-01
  bls,,CPI May 2026,https://bls.gov/...,2026-06-01

===== Project structure =====

  primary_wire/
    src/                  Python scripts (forthcoming)
    tests/
      src/                Tests for scripts in src/
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

===== Getting started =====

See [[docs/setup.txt]] for installation instructions.
See [[docs/contributing.txt]] to learn how to add new press release links.

===== License =====

MIT
