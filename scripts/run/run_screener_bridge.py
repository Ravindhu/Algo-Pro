#!/usr/bin/env python
"""run_screener_bridge.py - launch the screener -> OCL -> POV bridge.

Reads symbols from a screener export (``filtered_summary.csv`` written by
``blue_engine.screener.scrape_saved_screen``) or from ``--symbols`` and runs
one OCL signal per symbol. Fires go through RiskGate + the POV slicer.

Examples
--------
    # symbols straight from the latest screener export
    python scripts/run/run_screener_bridge.py --csv "Temp/Staging Workspace/screener_exports/filtered_summary.csv"

    # explicit list, custom caps, report for the dashboard
    python scripts/run/run_screener_bridge.py --symbols INFY,TCS --max-active 1 \
        --report data/screener_bridge_report.json

Symbols carry their own exchange: a CSV row resolved to BSE runs on BSE
while the NSE names run on NSE, with no override. Use ``--exchange`` only
to force ONE exchange for every symbol.

Safety
------
* Kill switch (durable, from optim_policy) is checked before every fire.
* ``max_active_symbols`` bounds how many symbols are supervised at once.
* ``max_concurrent_executions`` (default 1) serialises POV executions so
  total exposure stays bounded.
* The OpenAlgo mode (LIVE/PAPER) is never changed here - it is controlled
  in the OpenAlgo UI (single source of truth).
"""
from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blue_engine.core.screener_bridge import ScreenerBridge  # noqa: E402


def read_symbols_from_csv(path: Path) -> tuple[list[tuple[str, str]], str]:
    """Return per-row ``[(tradable_symbol, exchange)]`` + the majority exchange.

    Prefers the ``tradable_symbol``/``exchange`` columns (written by the
    symbol resolver); falls back to ``symbol``/``_symbol``. Rows flagged
    ``symbol_resolved=false`` are skipped. The exchange is kept PER ROW so a
    screen mixing NSE names with BSE-only codes needs no override.
    """
    symbols: list[tuple[str, str]] = []
    exchanges: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("tradable_symbol") or row.get("_symbol")
                   or row.get("symbol") or "").strip().upper()
            if not sym or row.get("symbol_resolved", "True").lower() == "false":
                continue
            ex = (row.get("exchange") or "NSE").strip().upper()
            exchanges[ex] = exchanges.get(ex, 0) + 1
            if (sym, ex) not in seen:
                seen.add((sym, ex))
                symbols.append((sym, ex))
    exchange = max(exchanges, key=exchanges.get) if exchanges else "NSE"
    return symbols, exchange


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="screener export CSV")
    src.add_argument("--symbols", help="comma-separated symbol list")
    ap.add_argument("--exchange", default=None,
                    help="force one exchange for every symbol (default: the "
                         "exchange resolved for each row)")
    ap.add_argument("--max-active", type=int, default=5)
    ap.add_argument("--max-concurrent", type=int, default=1)
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    ap.add_argument("--strategy-params", default=None,
                    help="JSON dict merged into every OCL signal config")
    ap.add_argument("--pov-params", default=None,
                    help="JSON dict merged into every POV slicer config")
    ap.add_argument("--risk-config", default=None,
                    help="JSON dict of risk-gate overrides")
    ap.add_argument("--exit-config", default=None,
                    help="JSON dict for the exit engine (broker SL-M). "
                         "Omit ONLY for paper runs: a filled position is "
                         "otherwise left unprotected")
    ap.add_argument("--no-wait-for-exit", action="store_true",
                    help="stop supervising after a filled entry")
    ap.add_argument("--report", type=Path,
                    default=ROOT / "data" / "screener_bridge_report.json")
    args = ap.parse_args()

    if args.csv:
        if not args.csv.exists():
            print(f"CSV not found: {args.csv}")
            return 2
        symbols, csv_exchange = read_symbols_from_csv(args.csv)
    else:
        symbols, csv_exchange = [
            (s.strip().upper(), args.exchange or "NSE")
            for s in args.symbols.split(",") if s.strip()], "NSE"
    if args.exchange:
        # Explicit override: every symbol on that one exchange.
        symbols = [(s, args.exchange.upper()) for s, _ in symbols]
        csv_exchange = args.exchange.upper()
    if not symbols:
        print("No tradable symbols found; nothing to do.")
        return 1
    by_ex: dict[str, int] = {}
    for _, ex in symbols:
        by_ex[ex] = by_ex.get(ex, 0) + 1
    print(f"symbols: {', '.join(f'{s}/{e}' for s, e in symbols)}")
    print("by exchange: " + ", ".join(f"{e}={n}" for e, n in sorted(by_ex.items())))

    def _json(value):
        return json.loads(value) if value else None

    bridge = ScreenerBridge(
        exchange=args.exchange or csv_exchange,
        symbols=symbols,
        strategy_params=_json(args.strategy_params),
        pov_params=_json(args.pov_params),
        risk_config=_json(args.risk_config),
        exit_config=_json(args.exit_config),
        max_active_symbols=args.max_active,
        max_concurrent_executions=args.max_concurrent,
        poll_seconds=args.poll_seconds,
        wait_for_exit=not args.no_wait_for_exit,
    )

    def _on_term(signum, frame):        # noqa: ARG001
        bridge.log(f"[BRIDGE] signal {signum} -> graceful stop")
        bridge.stop()

    signal.signal(signal.SIGINT, _on_term)
    signal.signal(signal.SIGTERM, _on_term)

    if args.exit_config is None:
        print("WARNING: no --exit-config: filled positions will NOT get a "
              "broker-side stop. Use this for paper runs only.")
    try:
        bridge.start()
    finally:
        bridge.stop()
        bridge.save_report(args.report)
    # A CRITICAL protection failure must be visible to the scheduler/supervisor
    # that launched this run, not just buried in the report file.
    if getattr(bridge, "_protection_failed", False):
        print("CRITICAL: at least one filled position could not be protected; "
              "see the report for the broker errors")
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
