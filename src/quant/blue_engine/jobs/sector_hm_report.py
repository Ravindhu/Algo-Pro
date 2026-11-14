"""
sector_hm_report.py - print (and persist) the sector Hilega-Milega score table.

WHY A SEPARATE COMMAND
----------------------
The score is TELEMETRY first and an allocation input second. Running it daily
without trading lets the ranking be judged on its own record before any capital
depends on it - and accumulating that daily history is what makes a
cross-sectional ML ranker trainable later (the sample grows by date, not by
ticker, because there are only ~15 sectors).

USAGE
-----
    python -m blue_engine.jobs.sector_hm_report
    python -m blue_engine.jobs.sector_hm_report --top 5
    python -m blue_engine.jobs.sector_hm_report --no-turn
    python -m blue_engine.jobs.sector_hm_report --json
    python -m blue_engine.jobs.sector_hm_report --as-of 2026-10-22

EXIT CODES
----------
    0  table produced
    1  no sector could be scored (a data problem, not a market condition)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import List, Optional

from ..sectors.sector_hm_score import SectorHMScorer


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sector Hilega-Milega score table (most discounted first).")
    parser.add_argument("--top", type=int, default=5,
                        help="How many discounted sectors to select.")
    parser.add_argument("--no-turn", action="store_true",
                        help="Rank on RSI alone instead of requiring the HM "
                             "recovery turn. Falling sectors are then WARNED.")
    parser.add_argument("--as-of", default=None,
                        help="Truncate the series at this date (YYYY-MM-DD).")
    parser.add_argument("--no-persist", action="store_true",
                        help="Do not append to the score history file.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output.")
    args = parser.parse_args(argv)

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    scorer = SectorHMScorer()
    table = scorer.score_all(as_of)
    selection = scorer.least_discounted(
        table, n=args.top, require_turn=not args.no_turn)

    if not args.no_persist:
        path = scorer.persist(table)
        table["persisted_to"] = str(path) if path else None

    if args.json:
        table["selection"] = selection
        print(json.dumps(table, indent=2))
    else:
        _print_table(table, selection, args.top)

    if not table["ranked"]:
        print("\n[FAIL] no sector index could be scored.", file=sys.stderr)
        return 1
    return 0


def _print_table(table, selection, top):
    c = table["counts"]
    lb = table["lookback"]
    print(f"SECTOR HM SCORE  as of {table['as_of']}")
    print(f"RSI({lb['rsi_period']}) vs EMA({lb['ema_period']}), "
          f"discount level = RSI {lb['discount_level']:.0f}")
    print(f"{c['scored']} sector indices scored; {c['below_level']} at/below "
          f"the level; {c['rising']} with RSI above its EMA\n")

    print(f"{'RANK':<5}{'SECTOR INDEX':<26}{'RSI':>6}{'EMA':>6}{'SCORE':>7}"
          f"{'1D/Below':>10}  SIGNAL")
    for i, r in enumerate(table["ranked"], 1):
        if r.get("hm_turn_buy"):
            sig = "HM BUY (recovering)"
        elif r.get("falling_knife"):
            sig = "falling knife"
        else:
            sig = "-"
        flag = "L" if r.get("below_level") else ""
        print(f"{i:<5}{r['name']:<26}{r['rsi']:>6.1f}{r['ema_rsi']:>6.1f}"
              f"{r['hm_score']:>7.2f}{flag:>10}  {sig}")

    print(f"\n--- LEAST DISCOUNTED x{top} ---")
    if selection["selected"]:
        for r in selection["selected"]:
            print(f"  {r['name']:<26} RSI {r['rsi']:>5.1f}  "
                  f"score {r['hm_score']:>6.2f}")
    else:
        print("  NONE qualify.")

    if selection["warnings"]:
        print("\nWARNINGS:")
        for w in selection["warnings"]:
            print(f"  - {w}")

    if table["unscored"]:
        print("\nUNSCORED:")
        for r in table["unscored"]:
            print(f"  {r['name']:<26} {r['reason']}")

    if table["not_available"]:
        print("\nNSE SECTORAL INDICES WITH NO USABLE DATA SOURCE:")
        for r in table["not_available"]:
            print(f"  {r['name']:<26} {r['reason']}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
