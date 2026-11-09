"""Research backtest: bars CSV -> QA gate -> momentum/low-vol composite -> top-N targets -> backtest.

Targets use only data up to each close. Fills happen at the next open.
Usage: python scripts/run_backtest.py --bars data/raw/bars.csv --top 3 --capital 1000000

Flow stage: Historical bars
Status: PHASE 2 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/backtest/engine.py
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quant.data.ingest import load_bars_csv  # noqa: E402
from quant.data.qa import check_bars  # noqa: E402
from quant.models.factors import composite  # noqa: E402
from quant.research.backtest import run_backtest  # noqa: E402
from quant.research.validation import sharpe  # noqa: E402


def build_targets(bars: pd.DataFrame, top: int, capital: float, lot: int = 1) -> pd.DataFrame:
    close = bars.pivot(index="date", columns="symbol", values="close")
    score = composite(close)
    rows = []
    for d, s in score.iterrows():
        s = s.dropna()
        if len(s) < top:
            continue
        for sym in s.nlargest(top).index:
            px = close.at[d, sym]
            qty = int(capital / top / px // lot * lot)
            rows.append({"date": d, "symbol": sym, "target_qty": qty})
        for sym in s.index.difference(s.nlargest(top).index):
            rows.append({"date": d, "symbol": sym, "target_qty": 0})
    return pd.DataFrame(rows)


def run(bars_path: str, top: int, capital: float) -> dict:
    bars = load_bars_csv(bars_path)
    qa = check_bars(bars)
    if not qa.passed:
        return {"qa_passed": False, "issues": qa.issues}
    targets = build_targets(bars, top, capital)
    res = run_backtest(bars, targets, capital=capital)
    ret = res.equity.pct_change().dropna()
    return {
        "qa_passed": True,
        "qa_warnings": [m for lvl, m in qa.issues if lvl == "WARNING"],
        "final_equity": round(float(res.equity.iloc[-1]), 2),
        "total_return": round(float(res.equity.iloc[-1] / capital - 1), 4),
        "sharpe_annualised": round(sharpe(ret), 3) if len(ret) > 1 else None,
        "trades": int(len(res.trades)),
        "total_cost": round(float(res.total_cost), 2),
        "missed_fills": res.missed_fills,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bars", required=True)
    p.add_argument("--top", type=int, default=3)
    p.add_argument("--capital", type=float, default=1_000_000.0)
    a = p.parse_args()
    print(json.dumps(run(a.bars, a.top, a.capital), indent=2, default=str))


if __name__ == "__main__":
    main()
