"""Verify the OpenAlgo strategy / execution subsystem over the REST API.

Read-only: lists strategies, runs and events. It does NOT start or stop a
strategy, because starting one in live (non-analyzer) mode can place orders.

Usage (from the openalgo/ folder):
    .venv\\Scripts\\python.exe ..\\Scripts\\verify_strategy_api.py <apikey>
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5000/api/v1/"
KEY = sys.argv[1] if len(sys.argv) > 1 else ""

PROBES = [
    # (label, path, method, body)
    ("strategy_list", "strategy/list", "POST", {}),
    ("pnl_symbols", "pnl/symbols", "POST", {}),
    # GET routes take the key as ?apikey= (no body)
    ("portfolio_benchmarks", "portfolio/benchmarks", "GET", {}),
    ("sip_frequencies", "sip/frequencies", "GET", {}),
    ("portfolio_holdings", "portfolio/holdings", "POST", {}),
    # These require a real strategy_id; the empty-strategy response is the
    # proof that the route exists and validates, so the status is expected.
    ("strategy_runs", "strategy/runs", "POST", {"strategy_id": "__verify_none__"}),
    ("strategy_events", "strategy/events", "POST", {"strategy_id": "__verify_none__"}),
    ("strategy_orders", "strategy/orders", "POST", {"strategy_id": "__verify_none__"}),
]


def post(path: str, method: str, body: dict) -> tuple[int, str]:
    url = BASE + path
    if method == "GET":
        url += ("&" if "?" in url else "?") + "apikey=" + KEY
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps({**body, "apikey": KEY}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def main() -> int:
    ok = 0
    print(f"Strategy / portfolio subsystem over {BASE}")
    print("=" * 92)
    for label, path, method, body in PROBES:
        code, raw = post(path, method, body)
        mark = "PASS" if code == 200 else "FAIL"
        if code == 200:
            ok += 1
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                bits = []
                for k in ("status", "message", "mode", "data"):
                    if k in d:
                        v = d[k]
                        if isinstance(v, list):
                            bits.append(f"{k}=list[{len(v)}]")
                        elif isinstance(v, dict):
                            bits.append(f"{k}=dict{list(v.keys())[:5]}")
                        else:
                            bits.append(f"{k}={str(v)[:70]}")
                summary = " ".join(bits)[:190]
            else:
                summary = str(d)[:190]
        except Exception:
            summary = raw[:190].replace("\n", " ")
        print(f"[{mark}] {code:<4} {label:<20} /{path}")
        print(f"       {summary}")
    print("=" * 92)
    print(f"TOTAL {len(PROBES)}  HTTP-200 {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())