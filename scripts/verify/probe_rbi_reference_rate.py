"""Probe whether the RBI/FBIL USD/INR reference rate is reliably fetchable.

The reference rate is published by FBIL and republished by RBI on the homepage
under "Current Rates -> Reference Rate" (labelled "(Source : FBIL)").

This checks the raw HTML structure so a parser can be written against what the
page actually emits, rather than against a guess. It does NOT cache or persist
anything.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\probe_rbi_reference_rate.py
"""

from __future__ import annotations

import re
import ssl
import sys
import urllib.request

URL = "https://www.rbi.org.in/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# "INR / 1 USD" ... ":" ... "<value>"  across arbitrary whitespace/tags.
INR_USD_RE = re.compile(
    r"INR\s*/\s*1\s*USD\s*</th>\s*<td>\s*:\s*([0-9]+\.[0-9]+)",
    re.IGNORECASE | re.DOTALL,
)
# "(As at 1.00pm of September 25, 2026)"
AS_AT_RE = re.compile(r"As at\s+([^<)]+)\)", re.IGNORECASE)


def fetch(url: str, timeout: int = 30) -> str:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode("utf-8", "replace")


def main() -> int:
    print(f"Fetching {URL} ...")
    try:
        html = fetch(URL)
    except Exception as exc:  # noqa: BLE001
        print(f"FETCH FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(f"  bytes downloaded: {len(html):,}")

    m = INR_USD_RE.search(html)
    if not m:
        print("  USD/INR value: NOT FOUND")
        print("  (page structure may have changed - inspect the template)")
        return 1
    print(f"  USD/INR reference rate: {m.group(1)}")

    a = AS_AT_RE.search(html)
    if a:
        print(f"  as-at stamp            : {a.group(1).strip()}")

    # Confirm the labels the rate carries, so provenance can be recorded.
    for label in ("Reference Rate", "FBIL", "Current Rates"):
        print(f"  contains {label!r}: {label in html}")

    # Reject a suspicious value rather than trusting a bad parse.
    val = float(m.group(1))
    if not (30.0 <= val <= 300.0):
        print(f"  !! value {val} outside plausible USD/INR band - abort")
        return 1
    print("\nOK: value parsed and within a plausible range.")
    return 0


if __name__ == "__main__":
    sys.exit(main())