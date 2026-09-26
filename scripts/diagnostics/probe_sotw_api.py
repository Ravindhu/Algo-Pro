"""Probe the Statistics of the World API with the supplied key.

Purpose: discover the REAL endpoint contract before wiring a provider, and
compare its India figures against the primary sources already verified in
``macro_master.json``.

Why the comparison matters: SOTW is an aggregator (IMF WEO + World Bank WDI).
Those are authoritative *annual* series, but they lag -- RBI/MoSPI publish
monthly/quarterly. So SOTW can be the right source for structural annual
indicators (debt/GDP, GDP per capita) and the WRONG source for anything the
engine needs to be current (CPI, FX, the 10Y yield).
usage limit API + MCP requests/day	1,000
Usage:
    openalgo\\Scripts\\python.exe Scripts\\probe_sotw_api.py <sotw_api_key>
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

KEY = sys.argv[1] if len(sys.argv) > 1 else ""
BASE = "https://statisticsoftheworld.com/api/v1"

# Candidate shapes - the docs advertise /rankings/<CODE>, so try the likely
# variants for a single-country lookup rather than guessing once.
CANDIDATES = [
    ("country indicators", "/countries/IND"),
    ("country indicators (alt)", "/country/IND"),
    ("indicator values", "/data/IND"),
    ("indicator + country", "/indicators/NY.GDP.MKTP.KD.ZG?country=IND"),
    ("rankings (all countries)", "/rankings/NY.GDP.MKTP.KD.ZG"),
    ("indicator list", "/indicators"),
]

# What we've already verified from primary sources (2026-09-26).
VERIFIED = {
    "GDP growth (%)": 8.2,
    "Inflation (%)": 2.5,
    "Unemployment (%)": 5.0,
    "Govt debt (% GDP)": 81.92,
    "USD/INR": 95.8918,
    "FX reserves (bn USD)": 765.9,
    "10Y yield (%)": 6.5,
}


def get(path: str, timeout: int = 30):
    url = BASE + path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {KEY}",
        "X-API-Key": KEY,
        "Accept": "application/json",
        "User-Agent": "blue-engine/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    if not KEY:
        print("No API key supplied.")
        return 1

    print("=" * 84)
    print(f"Statistics of the World API probe  (key ...{KEY[-8:]})")
    print("=" * 84)

    working = []
    for label, path in CANDIDATES:
        status, body = get(path)
        head = body[:160].replace("\n", " ")
        print(f"\n[{status}] {label}")
        print(f"      {BASE}{path}")
        print(f"      {head}")
        if status == 200:
            working.append((label, path, body))

    if not working:
        print("\nNo endpoint returned 200. The docs may use a different base "
              "path or require a header name we haven't tried.")
        return 1

    print()
    print("=" * 84)
    print("Endpoints that worked (for provider design)")
    print("=" * 84)
    for label, path, _ in working:
        print(f"  {label:<28} {path}")

    # Try to pull India's headline numbers out of whatever came back.
    print()
    print("=" * 84)
    print("India figures found vs primary-source verified values")
    print("=" * 84)
    for label, path, body in working:
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            continue
        flat = json.dumps(data).lower()
        hits = [k for k in ("gdp", "inflation", "unemployment", "debt",
                            "growth") if k in flat]
        if hits:
            print(f"  {path}: contains {hits}")
            print(f"     sample: {json.dumps(data)[:300]}")

    print()
    print("VERIFIED (primary sources, 2026-09-26) for comparison:")
    for k, v in VERIFIED.items():
        print(f"   {k:<24} {v}")
    print()
    print("NOTE: expect SOTW's annual IMF/WB figures to differ from the "
          "monthly/quarterly\nRBI/MoSPI numbers above. Where they differ, "
          "decide which the signal needs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())