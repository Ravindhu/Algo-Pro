"""Verify the MoSPI CPI reading before it changes any scored config.

This matters: macro_master.json currently holds inflation = 2.5%, and MoSPI's
2024-base series just returned 5.23% for August 2026. One of the two is wrong,
and inflation is a scored signal, so writing the wrong one into the engine
would silently misprice risk.

So: pull every recent month from MoSPI, print the whole series, and compare
against the alternatives. No config is written here.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\verify_cpi_series.py
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


def recs(out):
    if not isinstance(out, dict):
        return []
    for k in ("records", "data", "rows", "results"):
        if isinstance(out.get(k), list):
            return out[k]
    return []


def main() -> int:
    print("=" * 100)
    print("CPI verification: what is India's ACTUAL current inflation?")
    print("=" * 100)

    # --- 2024 base (new series) -------------------------------------------
    print("\n[1] CPI base_year=2024, series=Current  (new base, from 2025)")
    out = tool("get_data", {"dataset": "CPI", "filters": {
        "base_year": "2024", "series": "Current"}})
    rows = recs(out)
    print(f"    total rows returned: {len(rows)}")

    print(f"\n    {'base':<6} {'year':<6} {'month':<10} {'sector':<8} "
          f"{'division':<16} {'index':>8} {'inflation':>10}")
    print("    " + "-" * 74)
    for r in rows:
        print(f"    {str(r.get('base_year')):<6} {str(r.get('year')):<6} "
              f"{str(r.get('month')):<10} {str(r.get('sector')):<8} "
              f"{str(r.get('division'))[:15]:<16} "
              f"{str(r.get('index')):>8} {str(r.get('inflation')):>10}")

    # --- 2012 base (old series) for comparison ----------------------------
    print("\n[2] CPI base_year=2012, series=Current  (old base)")
    out2 = tool("get_data", {"dataset": "CPI", "filters": {
        "base_year": "2012", "series": "Current"}})
    rows2 = recs(out2)
    print(f"    total rows returned: {len(rows2)}")
    for r in rows2[:8]:
        print(f"    {str(r.get('baseyear')):<6} {str(r.get('year')):<6} "
              f"{(str(r.get('month')) or ''):<10} "
              f"{str(r.get('group'))[:28]:<30} "
              f"index={str(r.get('index')):>7} infl={str(r.get('inflation')):>8}")

    print()
    print("=" * 100)
    print("READING THIS")
    print("=" * 100)
    print("'division' == 'CPI (General)' is the headline all-India CPI.")
    print("Rows are split Rural / Urban, so the headline is their combination,")
    print("not a single row. Do NOT take rows[0] blindly -- that is Rural only.")
    print()
    print("macro_master.json currently says inflation = 2.5%. Reconcile that")
    print("against the figures above before wiring anything into the engine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())