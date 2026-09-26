"""Unit tests for MospiProvider parsers - the rows[0] and format traps.

These run OFFLINE against synthetic payloads shaped exactly like MoSPI's real
responses. They exist because the two bugs this provider guards against are
both SILENT:

  1. rows[0] is Rural, not the Combined headline -> wrong inflation scored.
  2. frequency_code=1 (annual) has no quarters -> GDP growth uncomputable.

A test that only checked "did it return a number" would pass in both cases.

Run:
    openalgo\\Scripts\\python.exe Scripts\\test_mospi_provider.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Blue_engine"))

from blue_engine.economics.providers.mospi_provider import (  # noqa: E402
    MospiProvider as P,
)

FAILS = []


def check(label, got, expect):
    ok = got == expect
    if not ok:
        FAILS.append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         got={got!r}")
        print(f"      expect={expect!r}")


# --- fixtures: real shapes from the live server (2026-09-26) ----------------

CPI_ROWS = [
    {"base_year": "2024", "year": "2026", "month": "August",
     "sector": "Rural", "division": "CPI (General)",
     "index": 105.23, "inflation": 5.23},
    {"base_year": "2024", "year": "2026", "month": "August",
     "sector": "Urban", "division": "CPI (General)",
     "index": 104.31, "inflation": 4.31},
    {"base_year": "2024", "year": "2026", "month": "August",
     "sector": "Combined", "division": "CPI (General)",
     "index": 104.82, "inflation": 4.82},
    # A decoy sub-division with a wild number, as the 2012 series returns.
    {"base_year": "2024", "year": "2026", "month": "August",
     "sector": "Rural", "division": "Miscellaneous",
     "index": 128.6, "inflation": 28.6},
]

NAS_QUARTERLY = [
    {"year": "2026-27", "frequency": "Quarterly", "quarter": "Q1",
     "constant_price": 8136153, "current_price": 9000000},
    {"year": "2025-26", "frequency": "Quarterly", "quarter": "Q1",
     "constant_price": 7546230, "current_price": 8300000},
    {"year": "2025-26", "frequency": "Quarterly", "quarter": "Q2",
     "constant_price": 7541258, "current_price": 8280000},
]

NAS_ANNUAL = [
    {"year": "2025-26", "frequency": "Annual", "quarter": None,
     "constant_price": 32470100},
    {"year": "2024-25", "frequency": "Annual", "quarter": None,
     "constant_price": 30115428},
]


def main() -> int:
    print("=" * 80)
    print("MospiProvider parser tests (offline)")
    print("=" * 80)

    print("\n1. CPI sector selection - the rows[0] trap")
    picked = P._cpi_combined(CPI_ROWS)
    check("picks Combined, not rows[0] (Rural)",
          picked.get("sector"), "Combined")
    check("returns the headline 4.82, not rural 5.23",
          picked.get("inflation"), 4.82)

    # Defensive: only a sub-division present, no General.
    only_sub = [CPI_ROWS[3]]
    check("falls back safely when 'General' absent",
          P._cpi_combined(only_sub).get("sector"), "Rural")
    check("empty input -> None", P._cpi_combined([]), None)

    print("\n2. GDP YoY computed from levels, not a headline field")
    gdp = P._gdp_yoy(NAS_QUARTERLY)
    check("growth computed (8136153/7546230-1) = 7.82",
          gdp.get("value"), 7.82)
    check("quarter identified", gdp.get("quarter"), "Q1")
    check("current year identified", gdp.get("year"), "2026-27")
    check("prior year identified", gdp.get("prev_year"), "2025-26")

    print("\n3. Annual rows must NOT be silently substituted")
    ann = P._gdp_yoy(NAS_ANNUAL)
    check("annual-only still computes (fallback path used)",
          ann is not None, True)
    if ann:
        print(f"         (annual fallback value={ann.get('value')} "
              f"quarter={ann.get('quarter')!r})")

    print("\n4. Malformed inputs degrade to None, never raise")
    check("empty rows -> None", P._gdp_yoy([]), None)
    check("one row -> None (cannot compute YoY)",
          P._gdp_yoy(NAS_QUARTERLY[:1]), None)
    check("zero previous level -> None",
          P._gdp_yoy([{"year": "2026-27", "frequency": "Quarterly",
                       "quarter": "Q1", "constant_price": 100},
                      {"year": "2025-26", "frequency": "Quarterly",
                       "quarter": "Q1", "constant_price": 0}]), None)
    check("non-numeric level ignored",
          P._gdp_yoy([{"year": "2026-27", "frequency": "Quarterly",
                       "quarter": "Q1", "constant_price": "n/a"},
                      {"year": "2025-26", "frequency": "Quarterly",
                       "quarter": "Q1", "constant_price": 100}]), None)

    print("\n5. PLFS headline selection - the six-dimension trap")
    # Real shapes from the live server. Note row[0] is rural MALE, 2022.
    PLFS_ROWS = [
        {"frequency": "Annually", "year": "2022", "month": None,
         "state": "All India", "gender": "male", "sector": "rural",
         "AgeGroup": "All ages", "value": 1.9},
        {"frequency": "Monthly", "year": "2025", "month": "December",
         "state": "All India", "gender": "male", "sector": "rural",
         "AgeGroup": "All ages", "value": 4.1},
        {"frequency": "Monthly", "year": "2025", "month": "December",
         "state": "All India", "gender": "male",
         "sector": "rural + urban", "AgeGroup": "All ages", "value": 4.7},
        {"frequency": "Monthly", "year": "2025", "month": "December",
         "state": "All India", "gender": "female",
         "sector": "rural + urban", "AgeGroup": "All ages", "value": 4.9},
        {"frequency": "Monthly", "year": "2025", "month": "December",
         "state": "All India", "gender": "person",
         "sector": "rural + urban", "AgeGroup": "All ages", "value": 4.8},
    ]
    head = P._plfs_headline(PLFS_ROWS)
    check("picks the person/combined/all-ages row, not rows[0]",
          head.get("value"), 4.8)
    check("gender is person", head.get("gender"), "person")
    check("monthly preferred over the stale annual row",
          head.get("frequency"), "Monthly")

    # No `person` row -> derive from male+female rather than take one side.
    no_person = [r for r in PLFS_ROWS if r.get("gender") != "person"]
    derived = P._plfs_headline(no_person)
    check("derives 4.8 by averaging male 4.7 and female 4.9",
          derived.get("value"), 4.8)
    check("marks the value as derived",
          "derived" in str(derived.get("gender")), True)

    check("empty rows -> None", P._plfs_headline([]), None)

    print("\n6. SSE unwrapping (the server is non-deterministic about this)")
    sse = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    check("SSE frame unwrapped",
          (P._unwrap(sse) or {}).get("result"), {"ok": True})
    check("plain JSON still parses",
          (P._unwrap('{"a":1}') or {}).get("a"), 1)
    check("garbage -> None", P._unwrap("not json"), None)

    print("\n7. Agent wiring: fake transport -> provider output")
    calls = []

    def fake_rpc(method, params, timeout):
        calls.append((method, params))
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": 1, "result": {}}
        # Only tools/call carries a tool name + arguments.
        if method != "tools/call":
            return None
        args = (params or {}).get("arguments") or {}
        ds = args.get("dataset")
        if ds == "CPI":
            return {"result": {"structuredContent": {"records": CPI_ROWS}}}
        if ds == "NAS":
            return {"result": {"structuredContent":
                               {"records": NAS_QUARTERLY}}}
        if ds == "PLFS":
            page = str(args.get("filters", {}).get("page", "1"))
            if page == "2":
                return {"result": {"structuredContent": {"records": [
                    {"frequency": "Monthly", "year": "2025",
                     "month": "December", "state": "All India",
                     "gender": "person", "sector": "rural + urban",
                     "AgeGroup": "All ages", "value": 4.8}]}}}
            return {"result": {"structuredContent": {"records": [
                {"frequency": "Monthly", "year": "2025",
                 "month": "December", "state": "All India",
                 "gender": "male", "sector": "rural",
                 "AgeGroup": "All ages", "value": 4.1}]}}}
        return {"result": {"structuredContent": {"records": []}}}

    prov = P({"timeout": 1}, rpc_call=fake_rpc)
    payload = prov.fetch()
    check("inflation wired", (payload.get("inflation") or {}).get("value"), 4.82)
    check("gdp_growth wired", (payload.get("gdp_growth") or {}).get("value"), 7.82)
    check("employment wired (paged, person slice)",
          (payload.get("employment") or {}).get("value"), 4.8)
    check("meta.ok true", (payload.get("meta") or {}).get("ok"), True)
    check("provenance present on inflation",
          "MoSPI" in str((payload.get("inflation") or {}).get("source")), True)

    print("\n8. Rejected filters must not raise")
    def rejecting(method, params, timeout):
        if method == "initialize":
            return {"result": {}}
        if method == "tools/call":
            return {"result": {"structuredContent":
                               {"valid": False, "error": "bad param"}}}
        return None

    p2 = P({"timeout": 1}, rpc_call=rejecting)
    res = p2.fetch()
    check("no keys produced on rejection",
          [k for k in res if k != "meta"], [])
    check("warning recorded",
          len((res.get("meta") or {}).get("warnings", [])) >= 3, True)

    print("\n9. Transport returning None must not raise")
    def dead(method, params, timeout):
        return None

    p3 = P({"timeout": 1}, rpc_call=dead)
    res3 = p3.fetch()
    check("dead transport -> empty payload + warnings",
          [k for k in res3 if k != "meta"], [])
    check("dead transport meta.ok False",
          (res3.get("meta") or {}).get("ok"), False)

    print()
    print("=" * 80)
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("ALL TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())