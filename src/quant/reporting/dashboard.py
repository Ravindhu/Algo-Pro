"""Dashboard data: EV, win rate, profit factor, risk use, rule compliance, and P&L.

P&L is always visible (decision 2026-11-04, overriding the earlier P&L-hidden design).
MAE and MFE are NOT shown here: they feed the optimizer score instead
(see Blue_engine/optimizer/objective.py).

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (profit factor delegated to Blue backtest.metrics; rest local)
Existing code to reuse or wrap (do not copy): Blue_engine/dashboard/app.py, Blue_engine/backtest/metrics.py
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

import numpy as np

from quant.core.blue_bridge import load


def profit_factor(gross_profit: float, gross_loss: float) -> Optional[float]:
    """Blue backtest.metrics.profit_factor: None when there are no losses."""
    return load("backtest.metrics").profit_factor(gross_profit, gross_loss)


def build_payload(trades: Sequence[dict], risk_used: float, rule_breaches: int) -> Dict[str, object]:
    """trades: dicts with ret (fraction) and pnl (money)."""
    if not trades:
        return {"trades": 0, "total_pnl": 0.0, "ev_pct_per_trade": None, "win_rate": None,
                "profit_factor": None, "risk_utilisation": risk_used,
                "rule_breaches": rule_breaches}
    ret = np.asarray([t["ret"] for t in trades], float)
    pnl = np.asarray([t["pnl"] for t in trades], float)
    wins, losses = pnl[pnl > 0].sum(), pnl[pnl < 0].sum()
    pf: Optional[float] = profit_factor(float(wins), float(losses))
    return {
        "trades": len(trades),
        "total_pnl": float(pnl.sum()),
        "ev_pct_per_trade": float(ret.mean() * 100),
        "win_rate": float((ret > 0).mean()),
        "profit_factor": pf,
        "risk_utilisation": risk_used,
        "rule_breaches": rule_breaches,
    }
