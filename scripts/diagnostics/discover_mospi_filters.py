"""Discover the exact filter vocabularies for CPI and NAS.

Two get_data calls were rejected with precise reasons:

  CPI: `level` is not a valid param. The real valid_params list is
       [base_year, series, year, month_code, state_code, group_code,
        subgroup_code, sector_code, division_code, class_code, ...]

  NAS: base_year is REQUIRED in addition to indicator_code.

Rather than guess again, this asks get_metadata for each dataset, which is the
server's own documented way to retrieve valid filter values.

Usage:
    openalgo\\Scripts\\python.exe Scripts\\discover_mospi_filters.py
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


def show(label, obj, limit=1100):
    print(f"\n  {label}")
    print(f"    {json.dumps(obj)[:limit]}")


def main() -> int:
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "blue-engine", "version": "1.0"}})

    print("=" * 98)
    print("CPI - metadata to get valid filter values")
    print("=" * 98)
    for code in ("1", "base_year", "Group"):
        m = tool("get_metadata", {"dataset": "CPI", "indicator_code": code})
        show(f"get_metadata(CPI, indicator_code={code!r})", m, 900)

    print()
    print("=" * 98)
    print("CPI - try get_data with only the params the error said are valid")
    print("=" * 98)
    trials = [
        {"base_year": "2024", "series": "Current"},
        {"base_year": "2024", "series": "Current", "limit": "6"},
        {"base_year": "2024", "series": "Current", "month_code": "8"},
        {"base_year": "2012", "series": "Current", "limit": "6"},
    ]
    for f in trials:
        out = tool("get_data", {"dataset": "CPI", "filters": f})
        bad = isinstance(out, dict) and out.get("valid") is False
        print(f"\n  filters={json.dumps(f)}")
        if bad:
            print(f"    REJECTED: {json.dumps(out)[:250]}")
        else:
            recs = None
            if isinstance(out, dict):
                for k in ("records", "data", "rows", "results"):
                    if isinstance(out.get(k), list):
                        recs = out[k]
                        break
            print(f"    OK - {len(recs) if recs else 0} records")
            if recs:
                print(f"    first: {json.dumps(recs[0])[:300]}")

    print()
    print("=" * 98)
    print("NAS - base_year is required; find its valid values")
    print("=" * 98)
    for by in ("2022-23", "2011-12", "Current"):
        out = tool("get_data", {"dataset": "NAS", "filters": {
            "base_year": by, "indicator_code": "5",
            "series": "Current", "frequency_code": "1", "limit": "6"}})
        bad = isinstance(out, dict) and out.get("valid") is False
        print(f"\n  base_year={by!r}")
        if bad:
            print(f"    REJECTED: {json.dumps(out)[:250]}")
        else:
            recs = None
            if isinstance(out, dict):
                for k in ("records", "data", "rows", "results"):
                    if isinstance(out.get(k), list):
                        recs = out[k]
                        break
            print(f"    OK - {len(recs) if recs else 0} records")
            if recs:
                print(f"    first: {json.dumps(recs[0])[:300]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())