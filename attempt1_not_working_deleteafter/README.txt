====== primary_wire ======

An aggregator of official press releases, published directly by their source organizations.
No third-party articles. No editorializing. Just the primary source.

Covers S&P 500 companies and government agencies (Fed, BLS, etc.).
Data is stored as plain CSV files in a git repository — one file per day.

===== How it works =====

  - A cron job on a Digital Ocean droplet runs ''src/fetch.py'' every 30 minutes
  - fetch.py reads ''sources/sources.yaml'' and polls each RSS feed
  - New entries are appended to ''data/YYYY/YYYY-MM-DD.csv''
  - Once a day at midnight, changes are committed and pushed to GitHub

===== Data format =====

Each daily CSV file has four columns:

^ Column        ^ Description                              ^
| slug          | Short slug identifying the organization  |
| ticker        | Stock ticker symbol (empty for govt sources) |
| title         | Press release title                      |
| url           | Link to the full press release           |
| published_at  | ISO 8601 UTC timestamp                   |

Example: ''data/2026/2026-06-01.csv''

  slug,ticker,title,url,published_at
  fedex,FedEx Completes Spin-Off of FedEx Freight,https://newsroom.fedex.com/...,2026-06-01T08:32:00Z
  nvidia,NVDA,NVIDIA Announces Q1 Results,https://nvidianews.nvidia.com/...,2026-06-01T09:00:00Z

===== Project structure =====

  primary_wire/
    src/
      fetch.py          Main entry point (run via cron)
      parser.py         RSS fetching and parsing
      dedup.py          Deduplication logic
    tests/
      src/
        test_parser.py
        test_dedup.py
    docs/
      setup.txt         How to install and run the project
      cron.txt          How to automate via cron on Digital Ocean
      sources.txt       How to add and manage sources
    sources/
      sources.yaml      Master list of sources and RSS feed URLs
    data/
      2026/
        2026-06-01.csv
    requirements.txt
    README.txt

===== Getting started =====

See [[docs/setup.txt]] for installation and setup instructions.

===== License =====

MIT
