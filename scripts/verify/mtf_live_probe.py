"""One read-only look at the live account for the MTF decision (owner 2026-11-25).

Reads only: positionbook, holdings and one margin quote. It places, cancels and modifies nothing.
It prints FIELD NAMES, counts and the broker's margin figures. It never prints the API key, symbols
held, or holding values. Run it yourself on the machine that has the OpenAlgo server:

    OPENALGO_API_KEY=... python scripts/verify/mtf_live_probe.py --symbol INFY --price 1000 --qty 1

Flow stage: Risk gate (verification of the broker margin reply)
Status: OWNER RUN
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.execution.openalgo_client import http_transport  # noqa: E402
from quant.execution.readonly_client import OpenAlgoReadOnly  # noqa: E402
from quant.risk.margin_entry import funded_share  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only MTF margin probe.")
    p.add_argument("--host", default=os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000"))
    p.add_argument("--symbol", required=True)
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--price", type=float, required=True, help="reference price of one share")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--max-leverage", type=float, default=5.0)
    args = p.parse_args()

    key = os.getenv("OPENALGO_API_KEY", "")
    if not key:
        print("OPENALGO_API_KEY is not set. Nothing was sent.")
        return 2
    ro = OpenAlgoReadOnly(args.host, http_transport)

    def show(title, body):
        status = body.get("status") if isinstance(body, dict) else None
        print(f"\n== {title}: status={status}")
        if isinstance(body, dict):
            print(f"   top-level keys: {sorted(body.keys())}")
            data = body.get("data")
            if isinstance(data, list):
                print(f"   rows: {len(data)}")
                if data and isinstance(data[0], dict):
                    print(f"   row field names: {sorted(data[0].keys())}")
                    products = sorted({str(r.get('product')) for r in data if isinstance(r, dict)})
                    print(f"   product values seen: {products}")
                    collat = [r for r in data if isinstance(r, dict) and float(r.get('collateralquantity') or 0) > 0]
                    print(f"   rows with collateralquantity > 0: {len(collat)}")
            elif isinstance(data, dict):
                print(f"   data keys: {sorted(data.keys())}")

    show("positionbook", ro.positionbook(key))
    show("holdings", http_transport(args.host.rstrip("/") + "/api/v1/holdings", {"apikey": key}))

    reply = ro.margin_for_order(key, args.symbol, args.exchange, "BUY", args.qty, "MTF",
                                pricetype="MARKET", price=args.price)
    show("margin reply (MTF, one order)", reply)
    full = args.qty * args.price
    funded, reason = funded_share(reply, full, args.max_leverage)
    print(f"\n   our order value: {full:.2f}")
    print(f"   verdict: {'PASS funded=%.0f%%' % (funded * 100) if funded is not None else 'REFUSED'} - {reason}")
    print("\nNothing was placed. Paste this output (no keys in it) back to the developer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
