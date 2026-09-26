"""Find the correct PLFS slice for India's headline unemployment rate.

The provider reported UR = 1.9 for 2022. That IS a real PLFS figure -- for
`gender=male, sector=rural, AgeGroup=15 years and above`. It is simply not the
headline.

PLFS rows are dimensioned by SIX fields, so row[0] is essentially never the
aggregate:

    state, gender, sector, AgeGroup, weekly_status, year_type

The published all-India headline is roughly:

    state=All India, gender=person, sector=rural+urban (or combined),
    AgeGroup="All ages", weekly_status="PS+SS"

This enumerates the distinct values of each dimension so the correct
combination can be selected from evidence rather than assumed.

Run:
    openalgo\\Scripts\\python.exe Scripts\\find_plfs_headline.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

MCP_URL = "https://mcp.mospi.gov.in"
HEADERS = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream",
           "User-Agent": "blue-engine/1.0"}
_id = [0]


def _strip(raw):
    t = raw.strip()
    if t.startswith("event:") or "data:" in t[:120]:
        ch = [l[5:].strip() for l in t.splitlines() if l.startswith("data:")]
        t = next((c for c in reversed(ch) if c and c != "[DONE]"), "")
    return t


def rpc(method, params=None, timeout=70):
    _id[0] += 1
    p = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        p["params"] = params
    req = urllib.request.Request(MCP_URL, data=json.dumps(p).encode(),
                                 headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(_strip(r.read().decode("utf-8", "replace")))
    except Exception as exc:  # noqa: BLE001
        return {"_err": f"{type(exc).__name__}: {exc}"}


def tool(name, args, timeout=70):
    doc = rpc("tools/call", {"name": name, "arguments": args}, timeout)
    res = (doc or {}).get("result") if isinstance(doc, dict) else None
    if not isinstance(res, dict):
        return doc
    if res.get("structuredContent") is not None:
        return res["structuredContent"]
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:  # noqa: BLE001
                return c["text"]
    return res


def rows_of(out):
    if isinstance(out, list):
        return out
    if isinstance(out, dict):
        for k in ("records", "data", "rows", "results"):
            if isinstance(out.get(k), list):
                return out[k]
    return []


def main() -> int:
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "blue-engine", "version": "1.0"}})

    # Pull the default page and report the dimensional spread. A large
    # `limit` is rejected, so page through instead if more rows are needed.
    all_rows = []
    for dataset_filters in (
        {"indicator_code": "3", "frequency_code": "1"},
        {"indicator_code": "3", "frequency_code": "3"},
    ):
        out = tool("get_data", {"dataset": "PLFS", "filters": dataset_filters})
        got = rows_of(out)
        print(f"\nfilters={dataset_filters} -> {len(got)} rows")
        all_rows.extend(got)

    rows = all_rows
    print("\n" + "=" * 96)
    print(f"PLFS UR rows fetched: {len(rows)}")
    print("=" * 96)

    if not rows:
        print("no rows")
        return 1

    dims = ["state", "gender", "sector", "AgeGroup", "weekly_status",
            "year_type", "frequency", "year"]
    for d in dims:
        vals = sorted({str(r.get(d)) for r in rows})
        print(f"\n  {d:<14} ({len(vals)}) {vals[:14]}")

    # Which combination looks like the headline?
    print()
    print("=" * 96)
    print("Candidate headline slices")
    print("=" * 96)
    for r in rows:
        g = str(r.get("gender", "")).lower()
        ag = str(r.get("AgeGroup", "")).lower()
        if g in ("person", "total", "all") or ag == "all ages":
            print(f"  {json.dumps(r)[:250]}")

    print()
    print("=" * 96)
    print("Values at plausible headline slices (expect ~5%)")
    print("=" * 96)
    hits = []
    for r in rows:
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        g = str(r.get("gender", "")).lower()
        ag = str(r.get("AgeGroup", "")).lower()
        st = str(r.get("state", "")).lower()
        if (st.startswith("all india") and g in ("person", "total", "all")
                and ag == "all ages"):
            hits.append((r.get("year"), r.get("sector"),
                         r.get("weekly_status"), v))
    for h in sorted(hits, key=lambda x: str(x[0]), reverse=True)[:12]:
        print(f"  year={h[0]} sector={h[1]} status={h[2]} UR={h[3]}")

    if not hits:
        print("  none matched; printing all distinct values for inspection")
        seen = set()
        for r in rows:
            key = (r.get("gender"), r.get("AgeGroup"))
            if key in seen:
                continue
            seen.add(key)
            print(f"  gender={r.get('gender')!r} AgeGroup={r.get('AgeGroup')!r} "
                  f"value={r.get('value')}")

    print()
    print("=" * 96)
    print("Monthly series (frequency_code=3) - the most current option")
    print("=" * 96)
    mrows = [r for r in rows if str(r.get("frequency")) == "Monthly"]
    print(f"monthly rows: {len(mrows)}")
    best = []
    for r in mrows:
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        ag = str(r.get("AgeGroup", "")).lower()
        st = str(r.get("state", "")).lower()
        if st.startswith("all india") and ag == "all ages":
            best.append((r.get("year"), r.get("month"), r.get("sector"),
                         r.get("gender"), v))
    for b in sorted(best, key=lambda x: (str(x[0]), str(x[1])),
                    reverse=True)[:12]:
        print(f"  {b[0]} {b[1]:<10} sector={b[2]:<8} gender={b[3]:<8} UR={b[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())