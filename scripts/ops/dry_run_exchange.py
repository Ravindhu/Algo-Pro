"""Slice 6: show the NSE/BSE decision for every screened symbol on live quotes. Writes nothing.

    python scripts/ops/dry_run_exchange.py                   # symbols from data/screened/latest.json
    python scripts/ops/dry_run_exchange.py --symbols RELIANCE,INFY

Read only: quotes and instrument lookups. No order, no file change.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.execution.exchange_dry_run import run  # noqa: E402
from quant.execution.openalgo_client import OpenAlgoClient, http_transport  # noqa: E402
from quant.execution.readonly_client import OpenAlgoReadOnly  # noqa: E402

SCREEN = ROOT / "data" / "screened" / "latest.json"


def _symbols(arg):
    if arg:
        return [s.strip().upper() for s in arg.split(",") if s.strip()]
    try:
        doc = json.loads(SCREEN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r["symbol"] for r in doc.get("results", []) if r.get("selected")]


def main() -> int:
    ap = argparse.ArgumentParser(description="Dry run of the NSE/BSE decision on live quotes (no orders).")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--base-url", default=os.environ.get("OPENALGO_URL", "http://127.0.0.1:5000"))
    args = ap.parse_args()
    api_key = OpenAlgoClient.api_key_from_env()
    if not api_key:
        print("[FAIL] OPENALGO_API_KEY is not set; no quote source", file=sys.stderr)
        return 1
    symbols = _symbols(args.symbols)
    if not symbols:
        print("[FAIL] no symbols: give --symbols or run refresh_screen.py first", file=sys.stderr)
        return 2
    reader = OpenAlgoReadOnly(args.base_url, http_transport)

    from quant.data.live_bars import LiveBars  # same lot logic the bot uses (fails to 1)
    lot_bars = LiveBars(reader, api_key)

    def lot(key, sym):
        return lot_bars._lot_size(sym)

    rows = run(symbols, reader, api_key, lot_reader=lot)
    print(f"{'symbol':<14}{'listing':<9}{'buy on':<9}{'buy at':>10}  {'exit on':<9}{'lot':>5}  note")
    for r in rows:
        note = r["entry_reason"] + ("  [BSE entry refused by gate]" if r["bse_entry_refused_by_gate"] else "")
        print(f"{r['symbol']:<14}{str(r['listing']):<9}{str(r['entry_exchange']):<9}"
              f"{str(r['buy_price']):>10}  {str(r['exit_exchange']):<9}{str(r['lot_size']):>5}  {note}")
    print("[dry-run] nothing placed, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
