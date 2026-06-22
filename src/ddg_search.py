#!/usr/bin/env python3
import sys
from ddgs import DDGS


def main():
    if len(sys.argv) < 2:
        print("Usage: python ddg_search.py <search query>", file=sys.stderr)
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=5))

    if results:
        for i in range(len(results)):
          print(results[i]["href"])
    else:
        print("No results found.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
