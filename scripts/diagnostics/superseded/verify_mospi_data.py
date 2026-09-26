"""Confirm MoSPI returns LIVE values for the parameters the engine needs.

Codes resolved from the server itself (get_indicators):

    CPI   base_year=2024 (latest; data 2026+), series=Current, level=Group
    NAS   indicator_code=5  -> "Gross Domestic Product"
    PLFS  indicator_code=3  -> "UR (Unemployment Rate)"
    IIP   base_year=2011-12, frequency=Monthly

Each pull is cross-checked against the primary values already verified for this
project, so a "success" that returns the wrong series is caught:

    inflation    expected ~2.5%   (MoSPI CPI Aug 2026)
    gdp_growth   expected ~8.2%   (Q1 FY27)
    employment   expected ~5.0%   (PLFS urban)

Usage:
    openalgo\\Scripts\\python.exe Scripts\\verify_mospi_data.py
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


def rpc(method, params=None, timeout=70):
    _id[0] += 1
    p = {"jsonrpc": "2.0", "id": _id[0], "method": method}
    if params is not None:
        p["params"] = params
    req = urllib.request.Request(MCP_URL, data=json.dumps(p).encode(),
                                 headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace")[:400]
    except Exception as exc:  # noqa: BLE001
        return f"ERR {type(exc).__name__}: {exc}"


def _env(raw):
    if not raw:
        return None
    t = raw.strip()
    if t.startswith("event:") or "data:" in t[:120]:
        ch = [l[5:].strip() for l in t.splitlines() if l.startswith("data:")]
        t = next((c for c in reversed(ch) if c and c != "[DONE]"), "")
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        return None


def tool(name, args, timeout=70):
    doc = _env(rpc("tools/call", {"name": name, "arguments": args}, timeout))
    if not isinstance(doc, dict):
        return None
    res = doc.get("result")
    if not isinstance(res, dict):
        return doc.get("error")
    if res.get("structuredContent") is not None:
        return res["structuredContent"]
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:  # noqa: BLE001
                return c["text"]
    return res


# (label, dataset, filters, expected value, note)
PULLS = [
    ("inflation (CPI)", "CPI",
     {"base_year": "2024", "series": "Current", "level": "Group", "limit": "8"},
     2.5, "Aug 2026 CPI YoY"),
    ("gdp_growth (NAS)", "NAS",
     {"indicator_code": "5", "series": "Current", "frequency_code": "1",
      "limit": "8"},
     8.2, "Q1 FY27 real GDP YoY"),
    ("employment (PLFS)", "PLFS",
     {"indicator_code": "3", "frequency_code": "1", "limit": "8"},
     5.0, "PLFS urban UR"),
    ("iip (industrial)", "IIP",
     {"base_year": "2011-12", "frequency": "Monthly", "type": "General",
      "limit": "8"},
     None, "new signal"),
]


def find_records(out):
    """Dig out a list of records from whatever shape the server returned."""
    if isinstance(out, list):
        return out
    if not isinstance(out, dict):
        return None
    for k in ("records", "rows", "results", "data"):
        v = out.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("records", "rows", "results", "data"):
                if isinstance(v.get(k2), list):
                    return v[k2]
    return None


def main() -> int:
    print("=" * 98)
    print("MoSPI LIVE data verification")
    print("=" * 98)

    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "blue-engine", "version": "1.0"}})

    working, broken = [], []
    for label, ds, filters, expected, note in PULLS:
        print(f"\n{'-' * 98}")
        print(f"{label}  [{ds}]")
        print(f"  filters: {json.dumps(filters)}")
        out = tool("get_data", {"dataset": ds, "filters": filters})

        if isinstance(out, dict) and out.get("valid") is False:
            print(f"  REJECTED: {json.dumps(out)[:230]}")
            broken.append(label)
            continue

        recs = find_records(out)
        if not recs:
            print(f"  no records: {json.dumps(out)[:300]}")
            broken.append(label)
            continue

        print(f"  records: {len(recs)}")
        # Print the most recent-looking few rows compactly.
        for r in recs[:3]:
            print(f"    {json.dumps(r)[:250]}")

        # Try to spot the headline number and compare with the expectation.
        if expected is not None:
            nums = []
            for r in recs[:12]:
                for k, v in (r.items() if isinstance(r, dict) else []):
                    try:
                        f = float(v)
                        if 0.0 <= f <= 100.0:
                            nums.append((k, f))
                    except (TypeError, ValueError):
                        continue
            close = [n for n in nums if abs(n[1] - expected) < 1.0]
            print(f"  expected ~{expected} ({note})")
            print(f"  close matches: {close[:4] if close else 'NONE'}")
            if close:
                working.append(label)
            else:
                broken.append(f"{label} (value not matched)")
        else:
            working.append(label)

    print()
    print("=" * 98)
    print("RESULT")
    print("=" * 98)
    print(f"  confirmed live : {working}")
    print(f"  not confirmed  : {broken}")
    return 0


if __name__ == "__main__":
    sys.exit(main())