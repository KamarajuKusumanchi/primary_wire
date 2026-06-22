#!/usr/bin/env python3
import sys
from ddgs import DDGS


def main():
    if len(sys.argv) < 2:
        print("Usage: python ddg_search.py <search query>", file=sys.stderr)
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=1))

    if results:
        print(results[0]["href"])
    else:
        print("No results found.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()