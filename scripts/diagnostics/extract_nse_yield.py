"""Extract the 10Y G-Sec yield and repo rate from NSE's allIndices payload.

Findings that shape this:
  * NSE /api/allIndices returns 200 + 113KB JSON with NO cookie priming needed
    (earlier it looked gated; it is not, given a Referer header).
  * FRED is unreachable from this host (ConnectionReset / Timeout), so it is
    not an option.
  * rbi.org.in did NOT yield a repo rate by regex this time, so the earlier
    "repo present" hit was probably a nav label rather than a value.

So this looks inside allIndices for the bond series and prints every entry
whose name looks bond-like, plus the full list of index names available.

Run:
    openalgo\\Scripts\\python.exe Scripts\\extract_nse_yield.py
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

URL = "https://www.nseindia.com/api/allIndices"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def main() -> int:
    req = urllib.request.Request(URL, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/",
        "Accept-Language": "en-IN,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30,
                                context=ssl.create_default_context()) as r:
        doc = json.loads(r.read().decode("utf-8", "replace"))

    rows = doc.get("data") or []
    print(f"total indices returned: {len(rows)}")

    print()
    print("=" * 96)
    print("Every index name available (so nothing bond-like is missed)")
    print("=" * 96)
    for r in rows:
        print(f"  {str(r.get('indexSymbol')):<28} {str(r.get('index'))[:54]}")

    print()
    print("=" * 96)
    print("Bond-like entries, full detail")
    print("=" * 96)
    keys = ("bond", "gsec", "g-sec", "sovereign", "gs", "yield", "debt")
    found = []
    for r in rows:
        blob = f"{r.get('indexSymbol','')} {r.get('index','')}".lower()
        if any(k in blob for k in keys):
            found.append(r)
            print(f"\n  {json.dumps(r, indent=2)[:400]}")

    if not found:
        print("  none - allIndices carries no bond series")

    print()
    print("=" * 96)
    print("VERDICT")
    print("=" * 96)
    print(f"  bond entries in allIndices: {len(found)}")
    print("  If 0, the 10Y yield is NOT available from this endpoint and there")
    print("  is no scriptable source among NSE / RBI-homepage / FRED / FBIL /")
    print("  CCIL. In that case 'bond_return' cannot be made live and should")
    print("  stay a dated static value -- do not substitute the NIFTYGS10YR")
    print("  index level (2647.02) for a yield (6.68%).")
    return 0


if __name__ == "__main__":
    sys.exit(main())