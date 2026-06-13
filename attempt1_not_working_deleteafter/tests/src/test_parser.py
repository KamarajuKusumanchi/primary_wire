"""
tests/src/test_parser.py
------------------------
Tests for src/parser.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from unittest.mock import patch, MagicMock
from parser import fetch_entries


def make_entry(title, link, published_parsed):
    entry = MagicMock()
    entry.title            = title
    entry.link             = link
    entry.published_parsed = published_parsed
    return entry


VALID_PARSED = (2026, 6, 1, 8, 32, 0, 0, 0, 0)

SOURCE_WITH_TICKER    = {"slug": "fedex", "ticker": "FDX",  "rss_url": "https://example.com/rss"}
SOURCE_WITHOUT_TICKER = {"slug": "bls",                     "rss_url": "https://example.com/rss"}


def test_fetch_entries_returns_correct_columns():
    mock_feed         = MagicMock()
    mock_feed.entries = [make_entry("Test Title", "https://example.com/1", VALID_PARSED)]

    with patch("parser.feedparser.parse", return_value=mock_feed):
        df = fetch_entries(SOURCE_WITH_TICKER)

    assert list(df.columns) == ["slug", "ticker", "title", "url", "published_at"]


def test_fetch_entries_correct_values_with_ticker():
    mock_feed         = MagicMock()
    mock_feed.entries = [make_entry("Test Title", "https://example.com/1", VALID_PARSED)]

    with patch("parser.feedparser.parse", return_value=mock_feed):
        df = fetch_entries(SOURCE_WITH_TICKER)

    assert df.iloc[0]["slug"]         == "fedex"
    assert df.iloc[0]["ticker"]       == "FDX"
    assert df.iloc[0]["title"]        == "Test Title"
    assert df.iloc[0]["url"]          == "https://example.com/1"
    assert df.iloc[0]["published_at"] == "2026-06-01T08:32:00Z"


def test_fetch_entries_empty_ticker_for_govt_source():
    mock_feed         = MagicMock()
    mock_feed.entries = [make_entry("CPI Report", "https://bls.gov/1", VALID_PARSED)]

    with patch("parser.feedparser.parse", return_value=mock_feed):
        df = fetch_entries(SOURCE_WITHOUT_TICKER)

    assert df.iloc[0]["slug"]   == "bls"
    assert df.iloc[0]["ticker"] == ""


def test_fetch_entries_skips_missing_date():
    entry_no_date              = make_entry("No Date", "https://example.com/2", None)
    entry_no_date.published_parsed = None
    mock_feed                  = MagicMock()
    mock_feed.entries          = [entry_no_date]

    with patch("parser.feedparser.parse", return_value=mock_feed):
        df = fetch_entries(SOURCE_WITH_TICKER)

    assert df.empty


def test_fetch_entries_empty_feed():
    mock_feed         = MagicMock()
    mock_feed.entries = []

    with patch("parser.feedparser.parse", return_value=mock_feed):
        df = fetch_entries(SOURCE_WITH_TICKER)

    assert df.empty
