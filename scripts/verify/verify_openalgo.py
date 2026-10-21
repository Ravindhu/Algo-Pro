"""End-to-end verification probe for a running OpenAlgo instance.

Read-only by design: every endpoint exercised here fetches data. No order is
placed, modified or cancelled, so this is safe to run against a live account.

Usage (from the openalgo/ folder):
    .venv\\Scripts\\python.exe ..\\Scripts\\verify_openalgo.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5000"
APIKEY = sys.argv[1] if len(sys.argv) > 1 else ""

# Auth contract (openalgo.in API docs / restx_api/schemas.py):
#   * POST routes take `apikey` as a FIELD IN THE JSON BODY.
#   * GET routes take it as an `?apikey=` QUERY PARAM.
# A Bearer header is NOT accepted and yields
#   {"status":"error","message":{"apikey":["Missing data for required field."]}}
#
# (label, path, method, extra_body_fields) -- all read-only, no order is placed.
PROBES = [
    ("ping", "/api/v1/ping", "POST", {}),
    ("funds", "/api/v1/funds", "POST", {}),
    ("orderbook", "/api/v1/orderbook", "POST", {}),
    ("tradebook", "/api/v1/tradebook", "POST", {}),
    ("positionbook", "/api/v1/positionbook", "POST", {}),
    ("openposition", "/api/v1/openposition", "POST",
     {"strategy": "verify-probe", "symbol": "RELIANCE", "exchange": "NSE",
      "product": "MIS"}),
    ("analyzer_status", "/api/v1/analyzer/", "POST", {}),
    ("intervals", "/api/v1/intervals/", "POST", {}),
    ("market_timings", "/api/v1/market/timings/", "POST", {"date": "2026-09-25"}),
    ("holidays", "/api/v1/market/holidays", "POST", {}),
    ("search", "/api/v1/search", "POST", {"query": "RELIANCE"}),
    ("quotes", "/api/v1/quotes", "POST", {"symbol": "RELIANCE", "exchange": "NSE"}),
    ("depth", "/api/v1/depth", "POST", {"symbol": "RELIANCE", "exchange": "NSE"}),
    ("quote_nifty", "/api/v1/quotes", "POST", {"symbol": "NIFTY", "exchange": "NSE_INDEX"}),
    ("history", "/api/v1/history", "POST",
     {"symbol": "RELIANCE", "exchange": "NSE", "interval": "5m",
      "start_date": "2026-09-01", "end_date": "2026-09-20"}),
    ("symbol_lookup", "/api/v1/symbol", "POST",
     {"symbol": "RELIANCE", "exchange": "NSE"}),
    ("expiry", "/api/v1/expiry", "POST",
     {"symbol": "NIFTY", "exchange": "NFO", "instrumenttype": "options"}),
    ("gttorderbook", "/api/v1/gttorderbook", "POST", {}),
    ("holidays_cal", "/api/v1/market/holidays", "POST", {}),
    ("telegram_config", "/api/v1/telegram/config", "POST", {}),
]


def call(path: str, method: str, body) -> tuple[int, str]:
    """Call one endpoint using OpenAlgo's documented auth contract."""
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if method == "GET":
        if APIKEY:
            url += ("&" if "?" in url else "?") + "apikey=" + APIKEY
        req = urllib.request.Request(url, headers=headers, method="GET")
    else:
        payload = dict(body or {})
        if APIKEY:
            payload["apikey"] = APIKEY
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def summarise(raw: str, limit: int = 170) -> str:
    try:
        d = json.loads(raw)
    except Exception:
        return raw[:limit].replace("\n", " ")
    if isinstance(d, dict):
        status = d.get("status")
        parts = []
        for key in ("status", "message", "mode", "data"):
            if key in d:
                v = d[key]
                if key == "data":
                    if isinstance(v, list):
                        parts.append(f"data=list[{len(v)}]")
                    elif isinstance(v, dict):
                        parts.append(f"data=dict{list(v.keys())[:6]}")
                    else:
                        parts.append(f"data={str(v)[:60]}")
                else:
                    parts.append(f"{key}={str(v)[:60]}")
        out = " ".join(parts) if parts else json.dumps(d)[:limit]
        return out[:limit]
    if isinstance(d, list):
        return f"list[{len(d)}] first={json.dumps(d[0])[:110] if d else '-'}"
    return str(d)[:limit]


def main() -> int:
    print(f"OpenAlgo read-only verification against {BASE}")
    print(f"API key: {APIKEY[:4]}****{APIKEY[-4:] if len(APIKEY) > 8 else ''} "
          f"(len {len(APIKEY)})")
    print("=" * 100)
    ok = bad = 0
    rows = []
    for label, path, method, body in PROBES:
        code, raw = call(path, method, body)
        if code == 200:
            ok += 1
            mark = "PASS"
        else:
            bad += 1
            mark = "FAIL"
        rows.append((mark, code, label, path, summarise(raw)))
    for mark, code, label, path, summary in rows:
        print(f"[{mark}] {code:<4} {label:<18} {path}")
        print(f"       {summary}")
    print("=" * 100)
    print(f"TOTAL {len(rows)}  HTTP-200 {ok}  NON-200 {bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())