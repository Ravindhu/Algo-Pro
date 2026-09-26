"""Verify India's GDP growth from MoSPI NAS, and settle the 6.8 vs 8.2 question.

Context: the config has passed through three different GDP values in this
project (6.8 original, 8.2 after my correction), and MoSPI's own NAS endpoint is
the authoritative answer. This pulls it and computes the growth rate from the
constant-price series rather than trusting a headline figure.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\verify_gdp_series.py
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
    print("Nuance check: does MoSPI NAS expose QUARTERLY GDP (the 8.2% figure)?")
    print("=" * 100)

    # Quarterly is what Q1 FY27 8.2% refers to. Try the frequency_code that
    # maps to quarterly and see whether a `quarter` field appears.
    for freq in ("1", "2", "3"):
        out = tool("get_data", {"dataset": "NAS", "filters": {
            "base_year": "2022-23", "indicator_code": "5",
            "series": "Current", "frequency_code": freq}})
        rows = recs(out)
        print(f"\n  frequency_code={freq!r} -> {len(rows)} rows")
        for r in rows[:5]:
            print(f"    year={r.get('year')} freq={r.get('frequency')} "
                  f"quarter={r.get('quarter')} "
                  f"const={r.get('constant_price')} "
                  f"rev={r.get('revision')}")

    print()
    print("=" * 100)
    print("Annual GDP level and implied YoY growth (constant prices)")
    print("=" * 100)
    for by in ("2011-12", "2022-23"):
        out = tool("get_data", {"dataset": "NAS", "filters": {
            "base_year": by, "indicator_code": "5",
            "series": "Current", "frequency_code": "1"}})
        rows = recs(out)
        print(f"\n  base_year={by}: {len(rows)} rows")
        print(f"    {'year':<9} {'freq':<9} {'quarter':<8} "
              f"{'current(cr)':>13} {'constant(cr)':>14} revision")
        print("    " + "-" * 84)
        for r in rows:
            print(f"    {str(r.get('year')):<9} {str(r.get('frequency')):<9} "
                  f"{str(r.get('quarter')):<8} "
                  f"{str(r.get('current_price')):>13} "
                  f"{str(r.get('constant_price')):>14} "
                  f"{str(r.get('revision'))[:26]}")

    print()
    print("=" * 100)
    print("HOW TO READ THIS")
    print("=" * 100)
    print("  * 'constant_price' is real GDP; YoY growth = this year / last - 1.")
    print("  * If quarter is null everywhere, this endpoint serves ANNUAL GDP")
    print("    only, and the quarterly 8.2% figure is NOT available here.")
    print("  * An annual level for 2025-26 is not the same series as a quarterly")
    print("    Q1 FY27 growth rate, so do not substitute one for the other.")
    return 0


if __name__ == "__main__":
    sys.exit(main())