"""Inspect the real PLFS payload to find the correct unemployment row.

The provider returned UR = 1.9 for year 2022. India's urban unemployment rate
is ~5%, and 2022 is 4 years stale, so something is selecting the wrong row or
the wrong series.

Likely cause: rows are ordered oldest-first, and/or the payload mixes
frequency (annual vs quarterly) and area (rural/urban/combined) the same way
CPI does. This dumps the raw structure instead of guessing.

Run:
    openalgo\\Scripts\\python.exe Scripts\\inspect_plfs.py
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
            return json.loads(_strip(r.read().decode("utf-8", "replace")))
    except Exception as exc:  # noqa: BLE001
        return {"_err": f"{type(exc).__name__}: {exc}"}


def _strip(raw):
    t = raw.strip()
    if t.startswith("event:") or "data:" in t[:120]:
        ch = [l[5:].strip() for l in t.splitlines() if l.startswith("data:")]
        t = next((c for c in reversed(ch) if c and c != "[DONE]"), "")
    return t


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

    print("=" * 96)
    print("PLFS indicators available")
    print("=" * 96)
    inds = tool("get_indicators", {"dataset": "PLFS"})
    print(json.dumps(inds)[:1500])

    print()
    print("=" * 96)
    print("PLFS ALL ROWS for indicator_code=3 (unemployment rate)")
    print("=" * 96)
    out = tool("get_data", {"dataset": "PLFS",
                            "filters": {"indicator_code": "3",
                                        "frequency_code": "1"}})
    rows = rows_of(out)
    print(f"rows: {len(rows)}")
    if rows:
        print(f"\ncolumn keys: {sorted(rows[0].keys())}")
        print()
        # Print every row - we need to see ordering and area/frequency fields.
        for i, r in enumerate(rows):
            print(f"  [{i}] {json.dumps(r)[:230]}")

    print()
    print("=" * 96)
    print("Try other frequency codes and indicator codes")
    print("=" * 96)
    for freq in ("1", "2", "3"):
        for code in ("3", "1", "2"):
            o = tool("get_data", {"dataset": "PLFS", "filters": {
                "indicator_code": code, "frequency_code": freq}})
            rr = rows_of(o)
            bad = isinstance(o, dict) and o.get("valid") is False
            if bad:
                print(f"  freq={freq} code={code}: REJECTED")
                continue
            print(f"  freq={freq} code={code}: {len(rr)} rows")
            for r in rr[:4]:
                print(f"      {json.dumps(r)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())