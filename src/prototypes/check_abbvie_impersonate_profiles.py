"""
prototypes/check_abbvie_impersonate_profiles.py

One-off diagnostic: test AbbVie's IR site (https://investors.abbvie.com/) --
Notified/Drupal, Akamai-fronted -- against several curl_cffi impersonation
profiles, to find out which ones currently get through.

Not part of the maintained package -- no project imports, nothing else
depends on this. Skips any candidate profile not supported by the
installed curl_cffi (see list_curl_cffi_profiles.py), and reports the
exception type alongside each failure so an invalid profile name can't be
mistaken for a real rejection by the site.

Usage:
  python src/prototypes/check_abbvie_impersonate_profiles.py
"""

import curl_cffi
from curl_cffi import requests
from curl_cffi.requests.impersonate import BrowserType

URL = "https://investors.abbvie.com/news-releases"
ATTEMPTS_PER_PROFILE = 5
TIMEOUT = 15

# Candidates to test, newest-first. Filtered below against whatever this
# curl_cffi install actually supports -- see list_curl_cffi_profiles.py to
# see the full list, or to add newer profiles here as curl_cffi adds them.
CANDIDATE_PROFILES = [
    "chrome150", "chrome146", "chrome145", "chrome142",
    "chrome136", "chrome133a", "chrome131", "chrome124",
]


def main() -> None:
    print(f"curl_cffi version: {curl_cffi.__version__}\n")

    available = {name for name in dir(BrowserType) if not name.startswith("_")}
    profiles = [p for p in CANDIDATE_PROFILES if p in available]
    skipped = [p for p in CANDIDATE_PROFILES if p not in available]
    if skipped:
        print(f"Skipping profile(s) not supported by this curl_cffi install: {', '.join(skipped)}")
        print("(run list_curl_cffi_profiles.py to see what IS available)\n")

    for profile in profiles:
        ok = 0
        # error type -> one example message, so repeats don't spam the output
        errors: dict[str, str] = {}
        for _ in range(ATTEMPTS_PER_PROFILE):
            try:
                session = requests.Session(impersonate=profile)
                session.get(URL, timeout=TIMEOUT)
                ok += 1
            except Exception as exc:
                errors.setdefault(type(exc).__name__, str(exc))
        fail = ATTEMPTS_PER_PROFILE - ok
        print(f"{profile}: {ok} ok, {fail} failed (out of {ATTEMPTS_PER_PROFILE})")
        for error_type, message in errors.items():
            print(f"    {error_type}: {message}")


if __name__ == "__main__":
    main()