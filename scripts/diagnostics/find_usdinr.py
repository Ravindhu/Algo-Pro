"""Find the USDINR contract this broker can actually serve.

The economic engine showed USD/local FX = 84.50, hardcoded in
macro_master.json. The live path is supposed to overwrite it, so the first
question is whether a usable USDINR series exists here at all.

The SDK's history() returns a pandas DataFrame (not a dict), which is why a
naive probe reports "no history" for instruments that work fine. This script
handles both shapes.

Usage (from my-trading-system/):
    openalgo\\Scripts\\python.exe Scripts\\find_usdinr.py <apikey>
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Blue_engine"))

KEY = sys.argv[1] if len(sys.argv) > 1 else ""

CANDIDATES = [
    ("USDINR25SEP26FUT", "CDS"),
    ("USDINR01OCT26FUT", "CDS"),
    ("USDINR28OCT26FUT", "CDS"),
    ("USDINR", "CDS"),
    ("USDINR", "NSE"),
    ("USDINR", "BSE"),
    ("USDINR", "MCX"),
]


def frame_close(result):
    """Return (n_rows, last_close) for either a DataFrame or a dict response."""
    if result is None:
        return 0, None
    # pandas DataFrame
    if hasattr(result, "empty") and hasattr(result, "columns"):
        if result.empty:
            return 0, None
        cols = {c.lower(): c for c in result.columns}
        if "close" in cols:
            try:
                return len(result), float(result[cols["close"]].iloc[-1])
            except Exception:  # noqa: BLE001
                return len(result), None
        return len(result), None
    # dict response
    if isinstance(result, dict):
        if result.get("status") != "success":
            return 0, None
        rows = result.get("data") or []
        if rows:
            return len(rows), rows[-1].get("close")
    return 0, None


def main() -> int:
    from blue_engine.core.client import init_client

    client = init_client(api_key=KEY)
    end = datetime.now()
    start = end - timedelta(days=60)

    print(f"{'symbol':<22} {'exchange':<9} {'rows':>5}  last close")
    print("-" * 60)
    winners = []
    for sym, exch in CANDIDATES:
        try:
            res = client.history(symbol=sym, exchange=exch, interval="D",
                                 start_date=start.strftime("%Y-%m-%d"),
                                 end_date=end.strftime("%Y-%m-%d"))
            n, last = frame_close(res)
        except Exception as exc:  # noqa: BLE001
            n, last = 0, f"EXC {type(exc).__name__}"
        print(f"{sym:<22} {exch:<9} {n:>5}  {last}")
        if n > 0:
            winners.append((sym, exch, last))

    print("-" * 60)
    if winners:
        print("USABLE USDINR SERIES:")
        for sym, exch, last in winners:
            print(f"   {sym} / {exch}  -> latest {last}")
    else:
        print("No USDINR series available from this broker account.")
        print("This broker/segment is not subscribed for CDS currency data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())