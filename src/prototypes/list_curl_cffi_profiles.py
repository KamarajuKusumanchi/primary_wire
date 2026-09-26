"""
prototypes/list_curl_cffi_profiles.py

One-off diagnostic: print the curl_cffi version installed in this
environment and every browser-impersonation profile it supports (the
strings valid for curl_cffi's requests.Session(impersonate=...)).

Not part of the maintained package -- no project imports, nothing else
depends on this. Useful whenever an IR site starts rejecting whatever
impersonation profile is currently in use (e.g. AbbVie's Notified/Drupal
site deterministically resetting HTTP/2 connections against "chrome124"
while "chrome131"-"chrome145" passed clean): re-run this first to see
what's even available to test, then feed candidates into
check_abbvie_impersonate_profiles.py (or an equivalent per-site script)
before assuming it's a new, different bug.

Usage:
  python src/prototypes/list_curl_cffi_profiles.py
"""

import curl_cffi
from curl_cffi.requests.impersonate import BrowserType

print(f"curl_cffi version: {curl_cffi.__version__}\n")

# BrowserType subclasses str, so dir() also returns str's own methods
# (capitalize, casefold, ...) alongside the actual profile names -- filter
# those out by keeping only names that start with a known browser prefix.
BROWSER_PREFIXES = ("chrome", "edge", "firefox", "safari", "tor")

profiles = sorted(
    name for name in dir(BrowserType)
    if not name.startswith("_") and name.startswith(BROWSER_PREFIXES)
)

print(f"{len(profiles)} impersonation profile(s) available:")
for name in profiles:
    print(f"  {name}")