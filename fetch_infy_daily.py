"""Fetch real daily INFY bars from the live broker bridge.

Pairs with infy_1min.csv so the end-to-end chain can run on real data:
daily = HTF (OCL pattern + buyer-initiated VWAP band)
1-minute = LTF (touch detection + z-ROC confirmation)

Credentials are used in the request body only and never printed.
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "infy_daily.csv"


def load_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


env = {}
for p in (ROOT / "openalgo" / ".env", ROOT / ".env"):
    env.update(load_env(p))

host = (env.get("HOST_SERVER") or "http://127.0.0.1:5000").rstrip("/")
api_key = env.get("OPENALGO_API_KEY", "")
if not api_key:
    print("OPENALGO_API_KEY missing")
    sys.exit(1)


def history(symbol, exchange, interval, start, end):
    body = json.dumps({
        "apikey": api_key, "symbol": symbol, "exchange": exchange,
        "interval": interval, "start_date": start, "end_date": end,
        "source": "api",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/v1/history", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


# Two years of daily gives the OCL pattern a real history to work with.
windows = [("2025-01-01", "2025-12-31"), ("2026-01-01", "2026-09-28")]

rows = []
for start, end in windows:
    try:
        resp = history("INFY", "NSE", "D", start, end)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        print(f"  {start}..{end}: HTTP {exc.code} {detail}")
        continue
    except Exception as exc:
        print(f"  {start}..{end}: {type(exc).__name__}: {exc}")
        continue
    if resp.get("status") != "success":
        print(f"  {start}..{end}: {resp.get('message')}")
        continue
    data = resp.get("data") or []
    print(f"  {start}..{end}: {len(data)} rows")
    rows.extend(data)

print(f"\ntotal daily rows: {len(rows)}")
if not rows:
    sys.exit(1)

cols = ["datetime", "open", "high", "low", "close", "volume"]
ts_key = next((k for k in ("timestamp", "datetime", "date", "time")
               if k in rows[0]), None)
print("source columns:", sorted(rows[0].keys()), "| ts:", ts_key)

written = 0
with OUT.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()
    for r in rows:
        try:
            w.writerow({
                "datetime": r.get(ts_key), "open": float(r["open"]),
                "high": float(r["high"]), "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume") or 0),
            })
            written += 1
        except (KeyError, TypeError, ValueError):
            continue

print(f"wrote {written} rows to {OUT}")
bad = sum(1 for r in rows if float(r.get("high", 0)) < float(r.get("low", 0)))
print(f"quality: {bad} rows with high<low")