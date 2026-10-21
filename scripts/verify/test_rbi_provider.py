"""Test the RBI/FBIL reference-rate provider: parser + live fetch.

Two layers, deliberately tested separately:

  1. Parser against synthetic markup - runs offline, pins the screen-scrape
     contract so a template change surfaces as a failing test rather than a
     silently-wrong FX rate.
  2. Live fetch - proves the real page still parses today.

Usage (from my-trading-system/):
    openalgo\\Scripts\\python.exe Scripts\\test_rbi_provider.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Blue_engine"))

from blue_engine.economics.providers.rbi_provider import (  # noqa: E402
    RbiReferenceRateProvider as P,
)

# --- fixtures ---------------------------------------------------------------
GOOD = """
<table><tr><th>INR / 1 USD</th><td>: 95.8918</td></tr></table>
<p>(As at 1.00pm of September 25, 2026)</p>
<p>(Source : FBIL)</p>
"""

NOT_FBIL = """
<table><tr><th>INR / 1 USD</th><td>: 95.8918</td></tr></table>
<p>Card rates, not reference rates</p>
"""

ABSURD = """
<table><tr><th>INR / 1 USD</th><td>: 1.25</td></tr></table>
<p>(Source : FBIL)</p>
"""

WHITESPACE = """
<th class="tableheader">  INR / 1 USD </th>
<td class="tabletd" align="center">   :   96.1234   </td>
<p>(Source: FBIL)</p>
"""

FAILURES = []


def check(label: str, got, expect) -> None:
    ok = got == expect
    if not ok:
        FAILURES.append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expect={expect!r}")


def main() -> int:
    print("=" * 74)
    print("1. Parser unit tests (offline, against synthetic markup)")
    print("=" * 74)

    r = P.parse_reference_rate(GOOD)
    check("valid page parses", r is not None, True)
    check("  rate extracted", (r or {}).get("rate"), 95.8918)
    check("  as-at extracted",
          (r or {}).get("as_at"), "1.00pm of September 25, 2026")

    check("rejects page without FBIL marker",
          P.parse_reference_rate(NOT_FBIL), None)
    check("rejects implausible value (1.25)",
          P.parse_reference_rate(ABSURD), None)
    check("handles extra whitespace/attrs",
          (P.parse_reference_rate(WHITESPACE) or {}).get("rate"), 96.1234)
    check("empty input returns None", P.parse_reference_rate(""), None)

    print()
    print("=" * 74)
    print("2. Live fetch from rbi.org.in")
    print("=" * 74)
    prov = P({"timeout": 30.0})
    payload = prov.fetch()
    meta = payload.get("meta") or {}
    print(f"  ok      : {meta.get('ok')}")
    print(f"  warnings: {meta.get('warnings')}")
    cur = payload.get("currency")
    if cur:
        print(f"  currency: value={cur.get('value')} unit={cur.get('unit')}")
        print(f"  as_of   : {cur.get('as_of')}")
        print(f"  source  : {cur.get('source')}")
        val = float(cur["value"])
        in_band = 90.0 <= val <= 100.0
        print(f"  plausible (90-100): {in_band}")
        if not in_band:
            FAILURES.append("live value outside expected band")
    else:
        print("  currency: NOT PRODUCED")
        FAILURES.append("live fetch produced no currency block")

    print()
    print("=" * 74)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILURE(S): {FAILURES}")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())